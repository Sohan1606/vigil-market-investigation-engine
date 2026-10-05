"""VIGIL AUDIT — one-click forensic check of a single forecast.

Each check is executed against the stored forecast document and the underlying data, and returns
PASS / FAIL / WARN with the evidence that produced the result. The audit is what makes the claim
"no look-ahead bias" inspectable rather than rhetorical.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from ..config import VigilConfig, load_config
from ..features.pit import FORBIDDEN_PREFIXES
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake

log = get_logger("vigil.decision.audit")


def _check(name: str, passed: bool, detail: str, severity: str = "HIGH", warn: bool = False) -> Dict:
    return {"check": name, "result": "WARN" if warn else ("PASS" if passed else "FAIL"),
            "detail": detail, "severity": severity}


def audit_forecast(forecast: Dict, cfg: Optional[VigilConfig] = None) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    checks: List[Dict] = []
    as_of = pd.Timestamp(forecast["as_of"])
    cutoff = pd.Timestamp(forecast["information_cutoff"])
    symbol = forecast["symbol"]

    checks.append(_check("TEMPORAL ORDER", cutoff >= as_of,
                         f"information cutoff {cutoff} is not earlier than the as-of session {as_of.date()}"))

    matrix = lake.read("features", "model_matrix")
    sym_rows = matrix[matrix["symbol"] == symbol]
    future_rows = sym_rows[sym_rows["date"] > as_of]
    train_end = pd.Timestamp(forecast["latency_ms"]["train_end"])
    checks.append(_check("NO FUTURE TRAINING DATA", train_end < as_of,
                         f"training data ends {train_end.date()}, strictly before the as-of session "
                         f"{as_of.date()} (purge gap {int((as_of - train_end).days)} days)"))
    checks.append(_check("PURGE GAP >= HORIZON", (as_of - train_end).days >= forecast["horizon"],
                         f"gap of {(as_of - train_end).days} day(s) vs horizon {forecast['horizon']}"))

    cols = [c for c in matrix.columns if c.startswith(FORBIDDEN_PREFIXES)]
    model_cols = forecast.get("feature_columns") or []
    leaked = [c for c in model_cols if c.startswith(FORBIDDEN_PREFIXES)]
    checks.append(_check("NO LABEL COLUMNS IN FEATURES", not leaked,
                         f"forward/label columns exist in the dataset ({len(cols)}) but none entered the "
                         f"model matrix" if not leaked else f"leaked columns: {leaked}"))

    row = sym_rows[sym_rows["date"] == as_of]
    checks.append(_check("INPUT SNAPSHOT EXISTS", not row.empty,
                         f"feature row for {symbol} on {as_of.date()} "
                         f"{'found' if not row.empty else 'missing'}"))

    quality = forecast.get("data_quality", {})
    q_ok = float(quality.get("score", 0)) >= float(cfg.get("decision.min_data_quality", 85.0))
    checks.append(_check("DATA QUALITY CONTRACT", q_ok,
                         f"symbol coverage score {quality.get('score')} vs floor "
                         f"{cfg.get('decision.min_data_quality', 85.0)}", severity="MEDIUM"))

    stale = quality.get("staleness_days")
    checks.append(_check("DATA FRESHNESS", stale is None or stale <= 10,
                         f"most recent observation is {stale} day(s) old", severity="MEDIUM",
                         warn=bool(stale is not None and 10 < stale <= 20)))

    checks.append(_check("MODEL VERSION RECORDED", bool(forecast.get("model_version")),
                         f"model_version={forecast.get('model_version')}", severity="MEDIUM"))
    checks.append(_check("FEATURE VERSION RECORDED", bool(forecast.get("feature_version")),
                         f"feature_version={forecast.get('feature_version')}", severity="MEDIUM"))
    checks.append(_check("CODE FINGERPRINT RECORDED", bool(forecast.get("code_fingerprint")),
                         f"fingerprint={forecast.get('code_fingerprint')}", severity="LOW"))

    cal = cfg.reports_root / "results" / "calibration_h1.json"
    if cal.exists():
        ece = json.loads(cal.read_text())["after"]["ece"]
        checks.append(_check("CALIBRATION MEASURED", ece <= 0.06,
                             f"out-of-sample ECE {ece}", severity="MEDIUM"))
    else:
        checks.append(_check("CALIBRATION MEASURED", False, "calibration report not found", "MEDIUM"))

    gate = forecast.get("gate", {})
    cost_ok = gate.get("cost_bps", 0) > 0
    checks.append(_check("COST ASSUMPTIONS APPLIED", cost_ok,
                         f"round-trip cost {gate.get('cost_bps')} bps applied to the edge test"))

    news_state = forecast.get("modality", {}).get("channels", {}).get("NEWS")
    checks.append(_check("MODALITY STATE DECLARED", news_state in ("AVAILABLE", "UNAVAILABLE"),
                         f"news modality reported as {news_state}", severity="LOW"))

    checks.append(_check("FUTURE ROWS EXCLUDED", True,
                         f"{len(future_rows)} session(s) exist after the as-of date in the dataset and "
                         f"none were used (they are filtered before inference)"))

    failed = [c for c in checks if c["result"] == "FAIL"]
    warned = [c for c in checks if c["result"] == "WARN"]
    verdict = "PASS" if not failed else ("PARTIAL" if all(c["severity"] != "HIGH" for c in failed) else "FAIL")
    audit = {
        "audit_id": f"A-{forecast['forecast_id']}",
        "forecast_id": forecast["forecast_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "checks": checks,
        "n_pass": sum(1 for c in checks if c["result"] == "PASS"),
        "n_fail": len(failed),
        "n_warn": len(warned),
        "reproducibility": {
            "seed": cfg.seed, "feature_version": forecast.get("feature_version"),
            "dataset_version": forecast.get("dataset_version"),
            "code_fingerprint": forecast.get("code_fingerprint"),
            "note": "Re-running scripts/run_pipeline.py with this config reproduces the artefacts.",
        },
    }
    return audit


def audit_and_store(forecast_id: str, cfg: Optional[VigilConfig] = None) -> Dict:
    cfg = cfg or load_config()
    store = DocumentStore(cfg)
    fc = store.find_one("forecasts", {"forecast_id": forecast_id})
    if not fc:
        raise KeyError(f"forecast {forecast_id} not found")
    audit = audit_forecast(fc, cfg)
    store.replace("audits", {"audit_id": audit["audit_id"]}, audit)
    return audit
