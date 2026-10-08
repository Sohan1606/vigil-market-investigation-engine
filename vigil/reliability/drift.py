"""Concept drift and model health.

Feature drift  : Population Stability Index between a reference window and the current window.
Performance drift: trailing Brier/accuracy vs the model's own validation baseline.
Prediction drift : share of extreme probabilities and direction-flip rate.
Health state   : HEALTHY / WATCH / DEGRADED / UNRELIABLE, with the reason recorded.
A DEGRADED or UNRELIABLE model is excluded from the production ensemble by the decision layer.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake

log = get_logger("vigil.reliability.drift")

HEALTH_ORDER = {"HEALTHY": 0, "WATCH": 1, "DEGRADED": 2, "UNRELIABLE": 3}


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    ref = reference[np.isfinite(reference)]
    cur = current[np.isfinite(current)]
    if len(ref) < 50 or len(cur) < 20:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    r, _ = np.histogram(ref, bins=edges)
    c, _ = np.histogram(cur, bins=edges)
    r = np.clip(r / max(r.sum(), 1), 1e-4, None)
    c = np.clip(c / max(c.sum(), 1), 1e-4, None)
    return float(np.sum((c - r) * np.log(c / r)))


def feature_drift(features: pd.DataFrame, feature_cols: List[str], window: int = 60) -> List[Dict]:
    features = features.sort_values("date")
    cutoff = features["date"].max() - pd.Timedelta(days=window)
    ref = features[features["date"] <= cutoff]
    cur = features[features["date"] > cutoff]
    out = []
    for col in feature_cols:
        if col not in features.columns:
            continue
        value = psi(ref[col].to_numpy(dtype=float), cur[col].to_numpy(dtype=float))
        if not np.isfinite(value):
            continue
        level = "LOW" if value < 0.1 else "MODERATE" if value < 0.25 else "HIGH"
        out.append({"feature": col, "psi": round(value, 4), "level": level})
    return sorted(out, key=lambda d: -d["psi"])


def performance_drift(preds: pd.DataFrame, window_sessions: int = 120) -> Dict:
    data = preds.dropna(subset=["y_true"]).sort_values("date")
    if data.empty:
        return {"available": False}
    sessions = data["date"].drop_duplicates().sort_values()
    recent_cut = sessions.iloc[-min(window_sessions, len(sessions))]
    recent = data[data["date"] >= recent_cut]
    baseline = data[data["date"] < recent_cut]
    if baseline.empty or recent.empty:
        return {"available": False}
    b_recent = float(np.mean((recent["p_up"] - recent["y_true"]) ** 2))
    b_base = float(np.mean((baseline["p_up"] - baseline["y_true"]) ** 2))
    acc_recent = float(((recent["p_up"] >= 0.5).astype(int) == recent["y_true"]).mean())
    acc_base = float(((baseline["p_up"] >= 0.5).astype(int) == baseline["y_true"]).mean())
    return {"available": True, "recent_brier": round(b_recent, 5), "baseline_brier": round(b_base, 5),
            "brier_degradation": round(b_recent - b_base, 5),
            "recent_accuracy": round(acc_recent, 4), "baseline_accuracy": round(acc_base, 4),
            "accuracy_degradation": round(acc_base - acc_recent, 4),
            "n_recent": int(len(recent))}


def health_state(perf: Dict, drift_rows: List[Dict], stability: Optional[float] = None) -> Dict:
    reasons: List[str] = []
    state = "HEALTHY"

    def escalate(new: str, reason: str) -> None:
        nonlocal state
        reasons.append(reason)
        if HEALTH_ORDER[new] > HEALTH_ORDER[state]:
            state = new

    high_drift = [d for d in drift_rows if d["level"] == "HIGH"]
    moderate = [d for d in drift_rows if d["level"] == "MODERATE"]
    perf_degraded = perf.get("available") and perf.get("brier_degradation", 0) > 0.005
    if len(high_drift) >= 3 and perf_degraded:
        escalate("DEGRADED", f"{len(high_drift)} features with PSI > 0.25 AND measured performance decay")
    elif len(high_drift) >= 3:
        escalate("WATCH", f"{len(high_drift)} features with PSI > 0.25 but no performance decay yet")
    elif high_drift:
        escalate("WATCH", f"{len(high_drift)} feature(s) with high PSI ({high_drift[0]['feature']})")
    elif len(moderate) >= 5:
        escalate("WATCH", f"{len(moderate)} features with moderate PSI")
    if perf.get("available"):
        if perf["brier_degradation"] > 0.02:
            escalate("DEGRADED", f"Brier degraded by {perf['brier_degradation']:.4f} vs baseline")
        elif perf["brier_degradation"] > 0.008:
            escalate("WATCH", f"Brier degraded by {perf['brier_degradation']:.4f}")
        if perf["recent_accuracy"] < 0.45:
            escalate("UNRELIABLE", f"recent accuracy {perf['recent_accuracy']:.3f} below 45%")
    if stability is not None and stability < 0.35:
        escalate("WATCH", f"forecast stability score {stability:.2f}")
    if not reasons:
        reasons.append("no drift or degradation detected in the monitored window")
    return {"state": state, "reasons": reasons}


def run_drift_monitor(cfg: Optional[VigilConfig] = None, horizon: int = 1) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    matrix = lake.read("features", "model_matrix")
    from ..models.tournament import default_feature_columns

    cols = default_feature_columns(matrix)[:24]
    drift_rows = feature_drift(matrix, cols, window=90)
    report: Dict[str, Dict] = {"features": drift_rows, "models": {}}
    preds = lake.read("analytics", f"oos_predictions_h{horizon}")
    for mid, sub in preds.groupby("model_id", observed=True):
        perf = performance_drift(sub)
        report["models"][mid] = {"performance": perf, "health": health_state(perf, drift_rows)}
    ens = lake.read("analytics", f"calibrated_predictions_h{horizon}")
    ens_perf = performance_drift(ens)
    report["models"]["adaptive_ensemble"] = {"performance": ens_perf,
                                             "health": health_state(ens_perf, drift_rows)}
    report["summary"] = {
        "high_psi_features": [d["feature"] for d in drift_rows if d["level"] == "HIGH"][:8],
        "worst_psi": drift_rows[0] if drift_rows else None,
        "unhealthy_models": [m for m, v in report["models"].items()
                             if v["health"]["state"] in ("DEGRADED", "UNRELIABLE")],
        "window": "last 90 calendar days vs all prior history",
    }
    (cfg.reports_root / "results" / f"drift_h{horizon}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("drift: %d high-PSI features, unhealthy models=%s",
             len(report["summary"]["high_psi_features"]), report["summary"]["unhealthy_models"])
    return report
