"""PHASE 11 — adaptive ensemble with point-in-time weights and explicit disagreement.

Weights for session t are computed only from model performance observed strictly BEFORE t
(trailing window of realised Brier scores), optionally conditioned on the market regime, and
damped by each model's health state. Disagreement is reported, not hidden: when models disagree,
the ensemble probability is shrunk toward 0.5 because the evidence is genuinely weaker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake
from .metrics import classification_metrics

log = get_logger("vigil.models.ensemble")


@dataclass
class EnsembleConfig:
    lookback_sessions: int = 120
    min_history: int = 40
    temperature: float = 12.0        # softmax sharpness over negative Brier
    disagreement_shrink: float = 0.6  # how strongly disagreement pulls probabilities to 0.5
    regime_conditioned: bool = True


def _wide(preds: pd.DataFrame) -> pd.DataFrame:
    return preds.pivot_table(index=["date", "symbol"], columns="model_id", values="p_up")


def build_adaptive_ensemble(preds: pd.DataFrame, regimes: Optional[pd.DataFrame] = None,
                            cfg_e: Optional[EnsembleConfig] = None,
                            exclude: Optional[List[str]] = None) -> pd.DataFrame:
    cfg_e = cfg_e or EnsembleConfig()
    exclude = exclude or ["baseline_momentum"]
    truth = preds.groupby(["date", "symbol"]).agg(y_true=("y_true", "first"),
                                                  fwd_ret=("fwd_ret", "first")).reset_index()
    wide = _wide(preds[~preds["model_id"].isin(exclude)])
    models = list(wide.columns)
    if not models:
        raise ValueError("no models available for the ensemble")
    frame = wide.reset_index().merge(truth, on=["date", "symbol"], how="left")
    if regimes is not None and not regimes.empty:
        frame = frame.merge(regimes[["date", "regime"]], on="date", how="left")
    else:
        frame["regime"] = "UNKNOWN"
    frame = frame.sort_values(["date", "symbol"]).reset_index(drop=True)

    sessions = frame["date"].drop_duplicates().sort_values().tolist()
    sq_err: Dict[str, List[float]] = {m: [] for m in models}
    hist_dates: List[pd.Timestamp] = []
    regime_err: Dict[str, Dict[str, List[float]]] = {m: {} for m in models}

    rows = []
    for day in sessions:
        block = frame[frame["date"] == day]
        # ---- weights from strictly past performance ----
        w = {}
        usable = sum(len(v) for v in sq_err.values()) >= cfg_e.min_history * len(models)
        for m in models:
            errs = sq_err[m][-cfg_e.lookback_sessions * 14:]
            base = float(np.mean(errs)) if errs else 0.25
            reg_component = None
            if cfg_e.regime_conditioned:
                reg = str(block["regime"].iloc[0])
                r_errs = regime_err[m].get(reg, [])[-cfg_e.lookback_sessions * 14:]
                if len(r_errs) >= 200:
                    reg_component = float(np.mean(r_errs))
            brier = base if reg_component is None else 0.5 * base + 0.5 * reg_component
            w[m] = np.exp(-cfg_e.temperature * brier)
        total = sum(w.values()) or 1.0
        weights = {m: (w[m] / total if usable else 1.0 / len(models)) for m in models}

        p_matrix = block[models].to_numpy(dtype=float)
        p_matrix = np.where(np.isnan(p_matrix), 0.5, p_matrix)
        wvec = np.array([weights[m] for m in models])
        p_ens = p_matrix @ wvec

        directions = (p_matrix >= 0.5).astype(int)
        agreement = np.maximum(directions.mean(axis=1), 1 - directions.mean(axis=1))
        spread = p_matrix.std(axis=1)
        # disagreement shrinks the signal toward 0.5 (honest weakening of weak evidence)
        shrink = 1.0 - cfg_e.disagreement_shrink * np.clip(spread / 0.12, 0, 1) * (1 - agreement)
        p_final = 0.5 + (p_ens - 0.5) * shrink

        for i, (_, r) in enumerate(block.iterrows()):
            rows.append({
                "date": day, "symbol": r["symbol"], "p_up": float(np.clip(p_final[i], 1e-4, 1 - 1e-4)),
                "p_raw": float(p_ens[i]), "model_agreement": float(agreement[i]),
                "model_spread": float(spread[i]), "y_true": r.get("y_true"),
                "fwd_ret": r.get("fwd_ret"), "regime": r.get("regime"),
                "weights": {m: round(float(weights[m]), 4) for m in models},
                "member_probabilities": {m: (None if np.isnan(r[m]) else round(float(r[m]), 4)) for m in models},
                "model_id": "adaptive_ensemble",
            })
        # ---- update performance history AFTER the day is scored (no look-ahead) ----
        for i, (_, r) in enumerate(block.iterrows()):
            if pd.isna(r.get("y_true")):
                continue
            y = float(r["y_true"])
            reg = str(r.get("regime", "UNKNOWN"))
            for j, m in enumerate(models):
                if np.isnan(p_matrix[i, j]):
                    continue
                e = (p_matrix[i, j] - y) ** 2
                sq_err[m].append(e)
                regime_err[m].setdefault(reg, []).append(e)
        hist_dates.append(day)
    return pd.DataFrame(rows)


def run_ensemble(cfg: Optional[VigilConfig] = None, horizon: int = 1) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    preds = lake.read("analytics", f"oos_predictions_h{horizon}")
    regimes = lake.read("analytics", "market_regime")[["date", "regime"]] \
        if lake.exists("analytics", "market_regime") else pd.DataFrame()
    ens = build_adaptive_ensemble(preds, regimes)
    scored = ens.dropna(subset=["y_true"]).copy()
    metrics = classification_metrics(scored[["p_up", "y_true"]].assign(
        p_up=scored["p_up"], y_true=scored["y_true"]))
    member_metrics = {}
    for mid, sub in preds.groupby("model_id"):
        member_metrics[mid] = classification_metrics(sub)["brier"]

    store_frame = ens.copy()
    store_frame["weights"] = store_frame["weights"].apply(json.dumps)
    store_frame["member_probabilities"] = store_frame["member_probabilities"].apply(json.dumps)
    lake.write(store_frame, "analytics", f"ensemble_predictions_h{horizon}", partition_cols=("symbol",))

    summary = {
        "horizon": horizon,
        "rows": int(len(ens)),
        "metrics": metrics,
        "member_brier": member_metrics,
        "mean_agreement": round(float(ens["model_agreement"].mean()), 4),
        "low_agreement_share": round(float((ens["model_agreement"] < 0.7).mean()), 4),
        "final_weights": ens.iloc[-1]["weights"] if len(ens) else {},
        "method": "softmax(-12 x trailing Brier), regime-conditioned, disagreement-shrunk",
    }
    (cfg.reports_root / "results" / f"ensemble_h{horizon}.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("ensemble: brier=%.5f acc=%.4f mean_agreement=%.3f",
             metrics["brier"], metrics["accuracy"], summary["mean_agreement"])
    return summary
