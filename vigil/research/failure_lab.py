"""PREDICTION FAILURE LAB + PREDICTION CEMETERY.

Every out-of-sample miss is categorised by *why* it plausibly failed, using only variables that
were observable at prediction time (regime, volatility state, model disagreement, data quality,
news availability, forecast instability). Counting errors is not analysis — attribution is.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake

log = get_logger("vigil.research.failure")

CATEGORIES = {
    "REGIME_SHIFT": "The market regime changed within the forecast window.",
    "VOLATILITY_SHOCK": "Realised volatility was in the top decile of its own history.",
    "MODEL_OVERCONFIDENCE": "High stated probability, wrong direction, models in agreement.",
    "MODEL_DISAGREEMENT": "Ensemble members disagreed; the signal was weak evidence.",
    "INSUFFICIENT_EVIDENCE": "Probability was within the abstention band — a coin flip.",
    "MISSING_MODALITY": "News coverage was unavailable at the information cutoff.",
    "DATA_QUALITY": "The symbol had quarantined rows in the recent window.",
    "FORECAST_INSTABILITY": "The forecast flipped direction in the preceding sessions.",
    "UNEXPLAINED": "No monitored condition explains this miss.",
}


def _attribute(row: pd.Series, band: float) -> List[str]:
    tags: List[str] = []
    if row.get("regime_changed"):
        tags.append("REGIME_SHIFT")
    if row.get("vol_decile") is not None and row["vol_decile"] >= 9:
        tags.append("VOLATILITY_SHOCK")
    conviction = abs(row["p"] - 0.5)
    if conviction < band:
        tags.append("INSUFFICIENT_EVIDENCE")
    if row.get("model_agreement", 1.0) < 0.6:
        tags.append("MODEL_DISAGREEMENT")
    elif conviction > 0.08:
        tags.append("MODEL_OVERCONFIDENCE")
    if not row.get("news_available", 0):
        tags.append("MISSING_MODALITY")
    if row.get("quarantine_rate", 0.0) > 0.05:
        tags.append("DATA_QUALITY")
    if row.get("flipped"):
        tags.append("FORECAST_INSTABILITY")
    return tags or ["UNEXPLAINED"]


def run_failure_lab(cfg: Optional[VigilConfig] = None, horizon: int = 1,
                    band: float = 0.055, cemetery_size: int = 40) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    store = DocumentStore(cfg)
    preds = lake.read("analytics", f"calibrated_predictions_h{horizon}").dropna(subset=["y_true"])
    preds["p"] = preds["p_cal"].fillna(preds["p_up"])
    feats = lake.read("features", "equity_features")[["date", "symbol", "vol_20", "sector"]]
    matrix = lake.read("features", "model_matrix")[["date", "symbol", "news_available"]]
    regimes = lake.read("analytics", "market_regime")[["date", "regime"]] \
        if lake.exists("analytics", "market_regime") else pd.DataFrame(columns=["date", "regime"])

    df = preds.merge(feats, on=["date", "symbol"], how="left").merge(matrix, on=["date", "symbol"], how="left")
    if not regimes.empty:
        df = df.merge(regimes, on="date", how="left", suffixes=("", "_r"))
        df["regime_changed"] = df.groupby("symbol")["regime"].transform(lambda s: s != s.shift(1))
    else:
        df["regime"], df["regime_changed"] = "UNKNOWN", False
    df["vol_decile"] = df.groupby("symbol")["vol_20"].transform(
        lambda s: s.rank(pct=True).mul(10).clip(upper=10).round())
    df["flipped"] = df.groupby("symbol")["p"].transform(
        lambda s: ((s >= 0.5).astype(int).diff().abs() > 0).rolling(3, min_periods=1).sum() > 1)
    qrep = json.loads((cfg.reports_root / "results" / "data_quality.json").read_text()) \
        if (cfg.reports_root / "results" / "data_quality.json").exists() else {"per_symbol": {}}
    df["quarantine_rate"] = df["symbol"].map(
        {k: (v.get("quarantined", 0) / max(v.get("rows", 1), 1))
         for k, v in qrep.get("per_symbol", {}).items()}).fillna(0.0)
    df["predicted_dir"] = np.where(df["p"] >= 0.5, 1, 0)
    df["correct"] = df["predicted_dir"] == df["y_true"].astype(int)

    misses = df[~df["correct"]].copy()
    misses["tags"] = misses.apply(lambda r: _attribute(r, band), axis=1)
    tag_counts: Dict[str, int] = {}
    for tags in misses["tags"]:
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    by_regime = (df.groupby("regime", observed=True)["correct"].agg(["mean", "count"])
                 .reset_index().rename(columns={"mean": "accuracy", "count": "n"}))
    by_regime["accuracy"] = by_regime["accuracy"].round(4)
    by_vol = (df.groupby("vol_decile", observed=True)["correct"].agg(["mean", "count"])
              .reset_index().rename(columns={"mean": "accuracy", "count": "n"}))
    by_vol["accuracy"] = by_vol["accuracy"].round(4)

    # ---- prediction cemetery: the most confident wrong calls ----
    misses["conviction"] = (misses["p"] - 0.5).abs()
    cem = misses.sort_values("conviction", ascending=False).head(cemetery_size)
    tombstones = []
    for r in cem.itertuples(index=False):
        tombstones.append({
            "forecast_id": f"F-{str(r.symbol).replace('.', '_')}-{pd.Timestamp(r.date).strftime('%Y%m%d')}-h{horizon}",
            "symbol": str(r.symbol), "as_of": str(pd.Timestamp(r.date).date()),
            "predicted": "UP" if r.p >= 0.5 else "DOWN",
            "stated_probability": round(float(r.p), 4),
            "actual": "UP" if r.y_true == 1 else "DOWN",
            "realised_return_pct": round(100 * float(r.fwd_ret), 3) if pd.notna(r.fwd_ret) else None,
            "regime": str(getattr(r, "regime", "UNKNOWN")),
            "model_agreement": round(float(r.model_agreement), 3) if pd.notna(r.model_agreement) else None,
            "news_available": bool(getattr(r, "news_available", 0)),
            "quarantine_rate": round(float(getattr(r, "quarantine_rate", 0.0)), 4),
            "failure_tags": list(r.tags),
            "assumption_that_failed": CATEGORIES[list(r.tags)[0]],
            "lesson": {
                "REGIME_SHIFT": "Condition the signal on regime; abstain through transitions.",
                "VOLATILITY_SHOCK": "Scale conviction down when volatility is in its top decile.",
                "MODEL_OVERCONFIDENCE": "Calibrate, then treat sharpness as a cost not a virtue.",
                "MODEL_DISAGREEMENT": "Disagreement is evidence of weakness — shrink the probability.",
                "INSUFFICIENT_EVIDENCE": "This forecast should never have been actionable.",
                "MISSING_MODALITY": "Reduce trust when a channel is dark instead of assuming neutral.",
                "DATA_QUALITY": "Quarantined inputs must propagate into the trust score.",
                "FORECAST_INSTABILITY": "An unstable trajectory invalidates a high-confidence reading.",
                "UNEXPLAINED": "Residual noise: daily direction is largely unpredictable.",
            }[list(r.tags)[0]],
        })
    store.drop("cemetery")
    if tombstones:
        store.insert_many("cemetery", tombstones)

    out = {
        "horizon": horizon,
        "n_predictions": int(len(df)),
        "n_misses": int(len(misses)),
        "miss_rate_pct": round(100 * float((~df["correct"]).mean()), 2),
        "failure_attribution": dict(sorted(tag_counts.items(), key=lambda kv: -kv[1])),
        "category_definitions": CATEGORIES,
        "accuracy_by_regime": by_regime.to_dict("records"),
        "accuracy_by_volatility_decile": by_vol.to_dict("records"),
        "accuracy_with_news_vs_without": {
            "with_news": round(float(df[df["news_available"] > 0]["correct"].mean()), 4)
            if (df["news_available"] > 0).any() else None,
            "without_news": round(float(df[df["news_available"] == 0]["correct"].mean()), 4),
            "n_with_news": int((df["news_available"] > 0).sum()),
        },
        "cemetery_size": len(tombstones),
        "note": "Attribution uses only variables observable at prediction time.",
    }
    (cfg.reports_root / "results" / f"failure_lab_h{horizon}.json").write_text(json.dumps(out, indent=2, default=str))
    log.info("failure lab: %d misses, top cause=%s", len(misses),
             next(iter(out["failure_attribution"]), None))
    return out
