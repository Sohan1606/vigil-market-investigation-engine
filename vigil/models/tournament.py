"""PHASE 10 — the model tournament.

Runs every available model family through the identical walk-forward protocol, scores them with
identical metrics, measures real fit/inference times, breaks performance down by market regime,
and writes out-of-sample predictions for the ensemble, calibration, backtest and failure lab.
"""
from __future__ import annotations

import json
import platform
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..features.dataset import NEWS_FEATURES, PRICE_FEATURES
from ..logging_utils import get_logger, timed
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from .metrics import classification_metrics, metrics_by_group
from .walkforward import SplitPlan, WalkForwardRunner
from .zoo import model_specs

log = get_logger("vigil.models.tournament")


def default_feature_columns(df: pd.DataFrame) -> List[str]:
    return [c for c in PRICE_FEATURES + NEWS_FEATURES if c in df.columns]


def run_tournament(cfg: Optional[VigilConfig] = None, horizon: int = 1,
                   data: Optional[pd.DataFrame] = None, persist: bool = True) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    df = data if data is not None else lake.read("features", "model_matrix")
    feature_cols = default_feature_columns(df)
    plan = SplitPlan(train_years=int(cfg.get("models.walk_forward.train_years", 3)),
                     test_months=int(cfg.get("models.walk_forward.test_months", 12)),
                     min_train_rows=int(cfg.get("models.walk_forward.min_train_rows", 400)),
                     purge_days=1)
    runner = WalkForwardRunner(feature_cols, horizon=horizon, plan=plan, seed=cfg.seed)

    regimes = lake.read("analytics", "market_regime")[["date", "regime"]] \
        if lake.exists("analytics", "market_regime") else pd.DataFrame(columns=["date", "regime"])

    leaderboard: List[Dict] = []
    all_preds: List[pd.DataFrame] = []
    specs = model_specs(cfg.seed)
    for spec in specs:
        with timed("model.walkforward", model=spec.model_id):
            run = runner.run(df, spec)
        if not run.available:
            leaderboard.append({"model_id": spec.model_id, "name": spec.name, "family": spec.family,
                                "available": False, "failures": run.failures,
                                "rationale": spec.rationale})
            continue
        preds = run.predictions
        m = classification_metrics(preds)
        if not regimes.empty:
            merged = preds.merge(regimes, on="date", how="left")
            by_regime = metrics_by_group(merged.dropna(subset=["regime"]),
                                         merged.dropna(subset=["regime"])["regime"], "regime")
        else:
            by_regime = []
        entry = {
            "model_id": spec.model_id, "name": spec.name, "family": spec.family,
            "available": True, "rationale": spec.rationale, "params": spec.params,
            "sequence_model": spec.sequence_model,
            "metrics": m,
            "by_regime": by_regime,
            "by_fold": [{"fold": f.fold, "test_start": f.test_start, "test_end": f.test_end,
                         "n_train": f.n_train, "n_test": f.n_test, "fit_seconds": f.fit_seconds,
                         "inference_ms_per_1k": f.inference_ms_per_1k} for f in run.folds],
            "mean_fit_seconds": round(float(np.mean([f.fit_seconds for f in run.folds])), 3),
            "mean_inference_ms_per_1k": round(float(np.mean([f.inference_ms_per_1k for f in run.folds])), 3),
            "failures": run.failures,
        }
        leaderboard.append(entry)
        all_preds.append(preds)

    preds_all = pd.concat(all_preds, ignore_index=True) if all_preds else pd.DataFrame()
    ranked = sorted([e for e in leaderboard if e.get("available")],
                    key=lambda e: (e["metrics"]["brier"], -(e["metrics"]["roc_auc"] or 0)))
    winner = ranked[0]["model_id"] if ranked else None
    baseline = next((e for e in leaderboard if e["model_id"] == "baseline_momentum" and e.get("available")), None)

    result = {
        "horizon": horizon,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_version": cfg.feature_version,
        "n_features": len(feature_cols),
        "feature_columns": feature_cols,
        "protocol": {
            "validation": "walk-forward rolling origin",
            "train_years": plan.train_years, "test_months": plan.test_months,
            "purge_sessions": horizon + plan.purge_days,
            "scaling": "median-impute + z-score fitted on the training fold only",
            "random_split_used": False,
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpu_count": int(__import__("os").cpu_count() or 1)},
        "leaderboard": leaderboard,
        "ranked_by": "Brier score (probability quality), tie-break ROC-AUC",
        "winner": winner,
        "baseline_brier": baseline["metrics"]["brier"] if baseline else None,
        "honest_note": (
            "Daily equity direction is close to a coin flip. Small accuracy differences between "
            "models are mostly noise; probability quality and calibration are the primary criteria."),
    }

    if persist:
        if not preds_all.empty:
            preds_out = preds_all.copy()
            preds_out["symbol"] = preds_out["symbol"].astype(str)
            lake.write(preds_out, "analytics", f"oos_predictions_h{horizon}",
                       partition_cols=("model_id",))
        path = cfg.reports_root / "results" / f"tournament_h{horizon}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, default=str))
        store = DocumentStore(cfg)
        store.drop("models")
        for entry in leaderboard:
            store.insert("models", {
                "model_id": entry["model_id"],
                "model_version": f"{entry['model_id']}@{cfg.feature_version}",
                "family": entry.get("family"),
                "available": entry.get("available", False),
                "horizon": horizon,
                "metrics": entry.get("metrics", {}),
                "trained_at": result["generated_at"],
                "rationale": entry.get("rationale", ""),
            })
    log.info("tournament complete: winner=%s", winner)
    return result
