"""Uncertainty quantification.

Three distinct quantities, never conflated (see docs/ml-methodology.md):
  DATA RELIABILITY  — is the input trustworthy?            (data quality engine)
  FORECAST CONFIDENCE — how sharp and how stable is p?     (this module)
  DECISION TRUST    — should we act?                        (decision gate)

Implemented here:
  * split-conformal prediction intervals for the forward return, with MEASURED coverage,
  * ensemble-dispersion uncertainty for the direction probability,
  * forecast stability over a trailing sequence of forecasts for the same symbol.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake

log = get_logger("vigil.reliability.uncertainty")


@dataclass
class ConformalBand:
    lower: float
    upper: float
    alpha: float
    scale: float


def conformal_return_interval(residuals: np.ndarray, sigma_now: float, sigma_hist: np.ndarray,
                              alpha: float = 0.2) -> ConformalBand:
    """Split-conformal interval around a zero-drift forecast, scaled by current volatility.

    Nonconformity score: |r_t| / sigma_t (volatility-normalised). The quantile is taken from past
    scores only; the band is then rescaled by the current sigma.
    """
    sigma_hist = np.where(sigma_hist <= 0, np.nan, sigma_hist)
    scores = np.abs(residuals) / sigma_hist
    scores = scores[np.isfinite(scores)]
    if len(scores) < 50 or not np.isfinite(sigma_now) or sigma_now <= 0:
        return ConformalBand(float("nan"), float("nan"), alpha, float("nan"))
    q = float(np.quantile(scores, 1 - alpha))
    return ConformalBand(-q * sigma_now, q * sigma_now, alpha, q)


def measure_coverage(df: pd.DataFrame, alpha: float = 0.2, min_history: int = 500) -> Dict:
    """Walk-forward coverage test of the conformal band — the honesty check on our own intervals."""
    data = df.dropna(subset=["fwd_ret", "sigma"]).sort_values("date").reset_index(drop=True)
    covered, widths, n = [], [], 0
    residual_hist: List[float] = []
    sigma_hist: List[float] = []
    for row in data.itertuples(index=False):
        if len(residual_hist) >= min_history:
            band = conformal_return_interval(np.array(residual_hist), row.sigma, np.array(sigma_hist), alpha)
            if np.isfinite(band.lower):
                covered.append(bool(band.lower <= row.fwd_ret <= band.upper))
                widths.append(band.upper - band.lower)
                n += 1
        residual_hist.append(float(row.fwd_ret))
        sigma_hist.append(float(row.sigma))
    if n == 0:
        return {"available": False, "reason": "insufficient history"}
    return {
        "available": True, "target_coverage": round(1 - alpha, 3),
        "empirical_coverage": round(float(np.mean(covered)), 4),
        "mean_width_pct": round(100 * float(np.mean(widths)), 3),
        "n_evaluated": n,
        "verdict": "intervals are well calibrated" if abs(np.mean(covered) - (1 - alpha)) < 0.05
                   else "intervals are miscalibrated on this dataset (reported, not hidden)",
    }


def probability_uncertainty(member_probs: Dict[str, Optional[float]], agreement: float) -> Dict:
    vals = [v for v in member_probs.values() if v is not None]
    if not vals:
        return {"spread": None, "uncertainty": 1.0, "label": "UNKNOWN"}
    spread = float(np.std(vals))
    # 0 = sharp & unanimous, 1 = maximally uncertain
    sharpness = 1.0 - min(abs(float(np.mean(vals)) - 0.5) / 0.15, 1.0)
    u = float(np.clip(0.5 * sharpness + 0.3 * min(spread / 0.10, 1.0) + 0.2 * (1 - agreement), 0, 1))
    label = "LOW" if u < 0.4 else "MEDIUM" if u < 0.68 else "HIGH"
    return {"spread": round(spread, 4), "uncertainty": round(u, 3), "label": label,
            "components": {"sharpness_deficit": round(sharpness, 3),
                           "member_spread": round(min(spread / 0.10, 1.0), 3),
                           "disagreement": round(1 - agreement, 3)}}


def forecast_stability(prob_series: List[float]) -> Dict:
    """Stability of the recent forecast trajectory for one symbol."""
    if len(prob_series) < 3:
        return {"available": False, "label": "UNKNOWN"}
    arr = np.asarray(prob_series, dtype=float)
    flips = int(np.sum(np.diff((arr >= 0.5).astype(int)) != 0))
    vol = float(np.std(np.diff(arr)))
    score = float(np.clip(1.0 - (flips / max(len(arr) - 1, 1)) - min(vol / 0.08, 1.0) * 0.5, 0, 1))
    return {"available": True, "direction_flips": flips, "step_volatility": round(vol, 4),
            "stability_score": round(score, 3),
            "label": "STABLE" if score > 0.66 else "DRIFTING" if score > 0.4 else "UNSTABLE",
            "trajectory": [round(float(x), 4) for x in arr[-8:]]}


def run_uncertainty(cfg: Optional[VigilConfig] = None, horizon: int = 1) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    ens = lake.read("analytics", f"calibrated_predictions_h{horizon}")
    feats = lake.read("features", "equity_features")[["date", "symbol", "vol_20"]]
    merged = ens.merge(feats, on=["date", "symbol"], how="left")
    merged["sigma"] = merged["vol_20"] / np.sqrt(252) * np.sqrt(horizon)
    coverage = measure_coverage(merged[["date", "symbol", "fwd_ret", "sigma"]].copy(), alpha=0.2)
    stability_sample = {}
    for sym, g in merged.sort_values("date").groupby("symbol"):
        stability_sample[sym] = forecast_stability(g["p_up"].tail(10).tolist())
    summary = {
        "horizon": horizon,
        "conformal": coverage,
        "method": "volatility-normalised split conformal (alpha=0.2), walk-forward coverage test",
        "stability_overview": {
            "stable": sum(1 for v in stability_sample.values() if v.get("label") == "STABLE"),
            "drifting": sum(1 for v in stability_sample.values() if v.get("label") == "DRIFTING"),
            "unstable": sum(1 for v in stability_sample.values() if v.get("label") == "UNSTABLE"),
        },
    }
    (cfg.reports_root / "results" / f"uncertainty_h{horizon}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("conformal coverage: %s", coverage)
    return summary
