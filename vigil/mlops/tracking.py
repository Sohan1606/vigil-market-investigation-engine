"""Lightweight experiment tracking + model registry + data lineage.

MLflow is intentionally not required: the project must run offline with no server. The tracking
store writes immutable JSON run records (parameters, metrics, artefacts, seed, versions, git commit
when available) under reports/mlruns, which is enough to reproduce any artefact in the repo.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import REPO_ROOT, VigilConfig, load_config
from ..logging_utils import get_logger
from .fingerprints import artefact_fingerprints, file_digest, tree_fingerprint

log = get_logger("vigil.mlops.tracking")


def git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


class Tracker:
    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.root = self.cfg.reports_root / "mlruns"
        self.root.mkdir(parents=True, exist_ok=True)

    def log_run(self, name: str, params: Dict[str, Any], metrics: Dict[str, Any],
                artifacts: Optional[List[str]] = None, tags: Optional[Dict[str, str]] = None) -> Dict:
        run = {
            "run_id": f"R-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": self.cfg.seed,
            "feature_version": self.cfg.feature_version,
            # Content hashes: the config's canonical bytes and the bytes of every module in the
            # `vigil` package. Both change if and only if the content changes.
            "config_fingerprint": hashlib.blake2b(
                json.dumps(self.cfg.raw, sort_keys=True, default=str).encode(), digest_size=8).hexdigest(),
            "code_fingerprint": tree_fingerprint(Path(REPO_ROOT) / "vigil", ("*.py",)),
            "git_commit": git_commit(),
            "environment": {"python": platform.python_version(), "platform": platform.platform()},
            "params": params,
            "metrics": metrics,
            "artifacts": artifacts or [],
            # Content digest of each artefact, so a run record can prove which bytes it produced.
            "artifact_fingerprints": artefact_fingerprints(
                [Path(REPO_ROOT) / a for a in (artifacts or [])], root=Path(REPO_ROOT)),
            "tags": tags or {},
        }
        (self.root / f"{run['run_id']}.json").write_text(json.dumps(run, indent=2, default=str))
        log.info("tracked run %s (%s)", run["run_id"], name)
        return run

    def runs(self, limit: int = 50) -> List[Dict]:
        files = sorted(self.root.glob("R-*.json"), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                out.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
        return out


def dataset_fingerprint(lake, zone: str, dataset: str) -> Optional[str]:
    """Content fingerprint of one lake dataset: hashes the bytes of its parquet blocks."""
    try:
        if not lake.exists(zone, dataset):
            return None
        if getattr(lake, "is_hdfs", False):
            return None  # remote blocks are fingerprinted by the cluster-side manifest
        return tree_fingerprint(lake.dataset_path(zone, dataset), ("*.parquet",))
    except Exception as exc:  # pragma: no cover - diagnostics must never break the surface
        log.warning("dataset fingerprint failed for %s/%s: %s", zone, dataset, exc)
        return None


def lineage(cfg: Optional[VigilConfig] = None) -> Dict:
    """RAW → CLEANED → FEATURES → MODEL → FORECAST → DECISION → OUTCOME, with real counts.

    Each stage carries a CONTENT fingerprint (BLAKE2b over the bytes of the parquet blocks or the
    result JSON), not a name/size hash, so the manifest is reproducible and actually detects
    change. See vigil/mlops/fingerprints.py.
    """
    cfg = cfg or load_config()
    from ..storage.docstore import DocumentStore
    from ..storage.lake import DataLake

    lake, store = DataLake(cfg), DocumentStore(cfg)
    inv = {f"{i['zone']}/{i['dataset']}": i for i in lake.inventory()}
    results = cfg.reports_root / "results"

    def load(name: str) -> Dict:
        p = results / name
        return json.loads(p.read_text()) if p.exists() else {}

    def result_fp(name: str) -> Optional[str]:
        p = results / name
        return file_digest(p) if p.exists() else None

    quality = load("data_quality.json")
    features = load("feature_build.json")
    tournament = load("tournament_h1.json")
    stages = [
        {"stage": "RAW DATA", "artifact": "lake://raw/ohlcv",
         "rows": inv.get("raw/ohlcv", {}).get("rows"), "blocks": inv.get("raw/ohlcv", {}).get("blocks"),
         "detail": "Yahoo Finance OHLCV for the configured NSE universe + indices"},
        {"stage": "CLEANED DATA", "artifact": "lake://curated/ohlcv",
         "rows": inv.get("curated/ohlcv", {}).get("rows"),
         "detail": f"data contract applied — score {quality.get('score')}, "
                   f"{quality.get('rows_quarantined')} rows quarantined, "
                   f"{quality.get('rows_rejected')} rejected"},
        {"stage": "FEATURE SET", "artifact": "lake://features/model_matrix",
         "rows": inv.get("features/model_matrix", {}).get("rows"),
         "detail": f"{features.get('feature_count')} engineered features via {features.get('engine')} "
                   f"({features.get('feature_version')})"},
        {"stage": "MODEL", "artifact": "docstore://models",
         "rows": store.count("models"),
         "detail": f"walk-forward tournament, winner={tournament.get('winner')}"},
        {"stage": "FORECAST", "artifact": "docstore://forecasts",
         "rows": store.count("forecasts"), "detail": "forecast contracts with input snapshots"},
        {"stage": "DECISION", "artifact": "docstore://decisions",
         "rows": store.count("decisions"), "detail": "Decision Gate verdicts with blocking reasons"},
        {"stage": "OUTCOME", "artifact": "docstore://cemetery + analytics/oos_predictions",
         "rows": store.count("cemetery"),
         "detail": "resolved predictions, failure attribution and lessons"},
    ]
    fingerprints = {
        "raw/ohlcv": dataset_fingerprint(lake, "raw", "ohlcv"),
        "curated/ohlcv": dataset_fingerprint(lake, "curated", "ohlcv"),
        "features/model_matrix": dataset_fingerprint(lake, "features", "model_matrix"),
        "data_quality.json": result_fp("data_quality.json"),
        "feature_build.json": result_fp("feature_build.json"),
        "tournament_h1.json": result_fp("tournament_h1.json"),
    }
    for stage, key in (("RAW DATA", "raw/ohlcv"), ("CLEANED DATA", "curated/ohlcv"),
                       ("FEATURE SET", "features/model_matrix")):
        for s_ in stages:
            if s_["stage"] == stage:
                s_["content_fingerprint"] = fingerprints.get(key)
    return {"stages": stages,
            "code_fingerprint": tree_fingerprint(Path(REPO_ROOT) / "vigil", ("*.py",)),
            "content_fingerprints": fingerprints,
            "fingerprint_method": "BLAKE2b-64 over file CONTENT (never name or size); manifests "
                                  "are sorted by repo-relative path, so they are deterministic "
                                  "across machines.",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
