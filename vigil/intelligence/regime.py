"""Market regime engine — probabilistic states with walk-forward (point-in-time) refits.

Method: a Gaussian Mixture over market-level state features (benchmark trend, realised volatility,
breadth, dispersion). The mixture is refit only on data strictly *before* each refit date, and the
fitted model labels the following period. Component -> regime naming is derived from each
component's own mean vector, not hand-assigned.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake

log = get_logger("vigil.intelligence.regime")

REGIMES = ["BULL", "BEAR", "SIDEWAYS", "HIGH_VOLATILITY"]
STATE_FEATURES = ["bench_trend_20", "bench_vol_20", "market_breadth_20", "dispersion_20"]


def market_state_frame(cfg: Optional[VigilConfig] = None) -> pd.DataFrame:
    """Market-level daily state (strictly backward-looking rolling statistics)."""
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    feats = lake.read("features", "equity_features")
    bench = lake.read("curated", "ohlcv")
    bench = bench[bench["symbol"] == cfg.benchmark].sort_values("date").set_index("date")
    state = pd.DataFrame(index=bench.index)
    logret = np.log(bench["close"]).diff()
    state["bench_ret_1"] = bench["close"].pct_change()
    state["bench_trend_20"] = bench["close"].pct_change(20)
    state["bench_vol_20"] = logret.rolling(20).std() * np.sqrt(252)
    state["bench_vol_60"] = logret.rolling(60).std() * np.sqrt(252)
    breadth = feats.groupby("date")["ret_1"].apply(lambda s: float((s > 0).mean()))
    disp = feats.groupby("date")["ret_1"].std()
    state["market_breadth"] = breadth.reindex(state.index)
    state["market_breadth_20"] = state["market_breadth"].rolling(20).mean()
    state["dispersion_20"] = disp.reindex(state.index).rolling(20).mean()
    state["drawdown"] = bench["close"] / bench["close"].cummax() - 1.0
    return state.dropna(subset=STATE_FEATURES).reset_index()


@dataclass
class RegimeAssignment:
    date: pd.Timestamp
    regime: str
    probabilities: Dict[str, float]
    confidence: float


class RegimeEngine:
    def __init__(self, cfg: Optional[VigilConfig] = None, n_components: int = 4) -> None:
        self.cfg = cfg or load_config()
        self.n_components = n_components
        self.model: Optional[GaussianMixture] = None
        self.scaler: Optional[StandardScaler] = None
        self.mapping: Dict[int, str] = {}

    # -------------------- fitting --------------------
    def fit(self, state: pd.DataFrame) -> "RegimeEngine":
        X = state[STATE_FEATURES].to_numpy()
        self.scaler = StandardScaler().fit(X)
        self.model = GaussianMixture(n_components=self.n_components, covariance_type="full",
                                     random_state=self.cfg.seed, n_init=4).fit(self.scaler.transform(X))
        self.mapping = self._name_components()
        return self

    def _name_components(self) -> Dict[int, str]:
        """Name components from their own centroids (trend / volatility quadrants)."""
        assert self.model is not None and self.scaler is not None
        centers = self.scaler.inverse_transform(self.model.means_)
        trend, vol = centers[:, 0], centers[:, 1]
        order_vol = np.argsort(vol)
        mapping: Dict[int, str] = {}
        high_vol_idx = int(order_vol[-1])
        mapping[high_vol_idx] = "HIGH_VOLATILITY"
        remaining = [i for i in range(len(centers)) if i != high_vol_idx]
        remaining.sort(key=lambda i: trend[i])
        if remaining:
            # a component is only BEAR/BULL if its own centroid trend has the matching sign
            mapping[remaining[0]] = "BEAR" if trend[remaining[0]] < 0 else "SIDEWAYS"
        if len(remaining) > 1:
            mapping[remaining[-1]] = "BULL" if trend[remaining[-1]] > 0 else "SIDEWAYS"
        for i in remaining[1:-1]:
            mapping[i] = "SIDEWAYS"
        return mapping

    # -------------------- inference --------------------
    def predict(self, state: pd.DataFrame) -> pd.DataFrame:
        assert self.model is not None and self.scaler is not None
        X = self.scaler.transform(state[STATE_FEATURES].to_numpy())
        proba = self.model.predict_proba(X)
        regime_proba = np.zeros((len(state), len(REGIMES)))
        for comp, name in self.mapping.items():
            regime_proba[:, REGIMES.index(name)] += proba[:, comp]
        out = pd.DataFrame(regime_proba, columns=[f"p_{r.lower()}" for r in REGIMES])
        out.insert(0, "date", state["date"].values)
        out["regime"] = [REGIMES[i] for i in regime_proba.argmax(axis=1)]
        out["regime_confidence"] = regime_proba.max(axis=1)
        # TRANSITION state: no component dominates
        out.loc[out["regime_confidence"] < 0.55, "regime"] = "TRANSITION"
        return out

    # -------------------- walk-forward history --------------------
    def walk_forward_history(self, state: pd.DataFrame, initial_years: int = 3,
                             refit_months: int = 6) -> pd.DataFrame:
        state = state.sort_values("date").reset_index(drop=True)
        start = state["date"].min() + pd.DateOffset(years=initial_years)
        cuts = pd.date_range(start, state["date"].max(), freq=f"{refit_months}MS")
        if len(cuts) == 0:
            cuts = pd.DatetimeIndex([start])
        frames: List[pd.DataFrame] = []
        for i, cut in enumerate(cuts):
            train = state[state["date"] < cut]
            end = cuts[i + 1] if i + 1 < len(cuts) else state["date"].max() + pd.Timedelta(days=1)
            apply_to = state[(state["date"] >= cut) & (state["date"] < end)]
            if len(train) < 250 or apply_to.empty:
                continue
            self.fit(train)
            block = self.predict(apply_to)
            block["fit_cutoff"] = cut
            frames.append(block)
        if not frames:
            raise RuntimeError("insufficient history for walk-forward regime estimation")
        hist = pd.concat(frames, ignore_index=True)
        # final model = fitted on everything available (used for today's inference only)
        self.fit(state)
        log.info("regime history: %d sessions, %d refits, distribution=%s",
                 len(hist), len(frames), hist["regime"].value_counts().to_dict())
        return hist


def run_regime_engine(cfg: Optional[VigilConfig] = None) -> Tuple[pd.DataFrame, Dict]:
    cfg = cfg or load_config()
    state = market_state_frame(cfg)
    engine = RegimeEngine(cfg)
    hist = engine.walk_forward_history(state)
    merged = state.merge(hist, on="date", how="inner")
    lake = DataLake(cfg)
    lake.write(merged.assign(symbol="_MARKET_"), "analytics", "market_regime",
               partition_cols=("year",))
    transitions = merged[merged["regime"] != merged["regime"].shift(1)][["date", "regime"]]
    meta = {
        "sessions": int(len(merged)),
        "distribution": {k: int(v) for k, v in merged["regime"].value_counts().items()},
        "transitions": int(len(transitions)),
        "current_regime": str(merged.iloc[-1]["regime"]),
        "current_probabilities": {r: round(float(merged.iloc[-1][f"p_{r.lower()}"]), 4) for r in REGIMES},
        "method": "GaussianMixture(4) on market state features, walk-forward refit every 6 months",
        "last_transitions": [{"date": str(d.date()), "regime": r}
                             for d, r in transitions.tail(8).itertuples(index=False)],
    }
    (cfg.reports_root / "results" / "regime.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return merged, meta
