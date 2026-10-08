"""Probability calibration with strictly historical fitting.

The calibrator for session t is fit on outcomes that were already observable before t
(expanding window with a purge gap of the forecast horizon). ECE/Brier before and after are
measured on the same out-of-sample rows, so an improvement claim is always verifiable.
"""
from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..models.metrics import classification_metrics, expected_calibration_error, reliability_curve
from ..storage.lake import DataLake

log = get_logger("vigil.reliability.calibration")


def rolling_calibrate(df: pd.DataFrame, horizon: int = 1, min_history: int = 1500,
                      refit_every: int = 60) -> pd.DataFrame:
    """Adds `p_cal` to a frame of [date, p_up, y_true] using only past outcomes."""
    data = df.sort_values("date").reset_index(drop=True).copy()
    data["p_cal"] = np.nan
    sessions = data["date"].drop_duplicates().sort_values().to_list()
    calibrator: Optional[IsotonicRegression] = None
    last_fit_idx = -10 ** 9
    for i, day in enumerate(sessions):
        observable = data[(data["date"] <= day - pd.Timedelta(days=horizon + 1)) & data["y_true"].notna()]
        if len(observable) >= min_history and (i - last_fit_idx) >= refit_every:
            calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
            calibrator.fit(observable["p_up"].to_numpy(), observable["y_true"].to_numpy())
            last_fit_idx = i
        mask = data["date"] == day
        if calibrator is not None:
            data.loc[mask, "p_cal"] = calibrator.predict(data.loc[mask, "p_up"].to_numpy())
        else:
            data.loc[mask, "p_cal"] = data.loc[mask, "p_up"]
    return data


def run_calibration(cfg: Optional[VigilConfig] = None, horizon: int = 1) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    ens = lake.read("analytics", f"ensemble_predictions_h{horizon}")
    cal = rolling_calibrate(ens[["date", "symbol", "p_up", "y_true"]].copy(), horizon=horizon)
    merged = ens.merge(cal[["date", "symbol", "p_cal"]], on=["date", "symbol"], how="left")
    lake.write(merged, "analytics", f"calibrated_predictions_h{horizon}", partition_cols=("symbol",))

    scored = merged.dropna(subset=["y_true", "p_cal"])
    before = classification_metrics(scored.rename(columns={"p_up": "p_up"})[["p_up", "y_true"]])
    after = classification_metrics(scored.assign(p_up=scored["p_cal"])[["p_up", "y_true"]])
    summary = {
        "horizon": horizon,
        "method": "rolling isotonic regression, expanding window, purge gap = horizon + 1 session",
        "rows_scored": int(len(scored)),
        "before": {"brier": before["brier"], "ece": before["ece"], "log_loss": before["log_loss"],
                   "accuracy": before["accuracy"]},
        "after": {"brier": after["brier"], "ece": after["ece"], "log_loss": after["log_loss"],
                  "accuracy": after["accuracy"]},
        "ece_improvement": round(before["ece"] - after["ece"], 5),
        "brier_improvement": round(before["brier"] - after["brier"], 5),
        "reliability_before": before["reliability"],
        "reliability_after": after["reliability"],
        "verdict": ("calibration improved probability quality" if after["brier"] < before["brier"]
                    else "calibration did NOT improve probability quality on this dataset"),
    }
    (cfg.reports_root / "results" / f"calibration_h{horizon}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("calibration: ECE %.4f -> %.4f | Brier %.5f -> %.5f",
             before["ece"], after["ece"], before["brier"], after["brier"])
    return summary
