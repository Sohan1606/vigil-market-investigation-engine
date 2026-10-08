"""PHASE 13 — cost-aware backtesting and decision-quality accounting.

Strategies compared on identical out-of-sample windows:
  BUY_AND_HOLD        : equal-weight long the universe (the benchmark any strategy must beat)
  ALWAYS_LONG_SIGNAL  : trade every directional signal (no abstention, no gate)
  MODEL               : trade when the calibrated probability leaves a fixed band
  ADAPTIVE_GATE       : trade only when the full Decision Gate clears (abstention allowed)
  ADAPTIVE_GATE_T1    : the same gate executed one session late (implementation-lag sensitivity)

EXECUTION CONVENTION (one convention, used everywhere and documented identically in
docs/SCIENTIFIC_METHOD.md, README.md and vigil/research/experiments.py)
---------------------------------------------------------------------------------------------
  * features and the calibrated probability for session t use information up to the close of t;
  * the position is OPENED at the close of session t;
  * the position is CLOSED at the close of session t+h, where h is the forecast horizon;
  * the realised return is exactly the label, fwd_ret_h[t] = close[t+h] / close[t] - 1;
  * every position pays the full round trip (entry + exit):
    cost = turnover x (commission_bps + slippage_bps + impact_bps), turnover = 2 x exposure;
  * positions are equal-weighted across the symbols traded in a session; no leverage, no
    compounding of intra-horizon signals, no financing, no taxes, no borrow constraint.

The convention is optimistic in one specific, disclosed way: it assumes the session-t closing
price is obtainable for a signal computed from that same close. ADAPTIVE_GATE_T1 quantifies that
optimism by delaying execution one session (open at close t+1, exit at close t+1+h), which is
achievable in practice. Both numbers are published.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake
from .costs import CostModel
from .risk import performance_summary

log = get_logger("vigil.decision.backtest")


def _equity_curve(daily: pd.Series) -> List[Dict]:
    curve = (1 + daily.fillna(0)).cumprod()
    return [{"date": str(pd.Timestamp(d).date()), "equity": round(float(v), 5)}
            for d, v in curve.items()]


def run_backtest(cfg: Optional[VigilConfig] = None, horizon: int = 1,
                 band: float = 0.055, gate_min_agreement: float = 0.55,
                 gate_max_uncertainty: float = 0.62) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    costs = CostModel.from_config(cfg)
    preds = lake.read("analytics", f"calibrated_predictions_h{horizon}").dropna(subset=["fwd_ret"])
    preds = preds.sort_values(["date", "symbol"]).copy()
    preds["p"] = preds["p_cal"].fillna(preds["p_up"])
    feats = lake.read("features", "equity_features")[["date", "symbol", "vol_20"]]
    preds = preds.merge(feats, on=["date", "symbol"], how="left")
    preds["sigma_daily"] = preds["vol_20"] / np.sqrt(252)
    # Expected absolute move used for the cost test (volatility-scaled, no future information)
    preds["expected_move"] = preds["sigma_daily"].fillna(preds["sigma_daily"].median())
    preds["edge_bps"] = 2 * (preds["p"] - 0.5).abs() * preds["expected_move"] * 10_000
    preds["uncertainty"] = 1 - (preds["model_agreement"].fillna(0.5))
    # Implementation-lag sensitivity: the same decision executed one session later. Shifting the
    # realised return per symbol is the post-hoc evaluation of a delayed execution; it changes no
    # feature, probability or gate input.
    preds["fwd_ret_t1"] = preds.sort_values(["symbol", "date"]).groupby("symbol", observed=True)["fwd_ret"].shift(-1)

    strategies: Dict[str, pd.Series] = {}
    positions: Dict[str, pd.Series] = {}

    # equal-weight buy & hold of the same universe
    bh = preds.groupby("date")["fwd_ret"].mean()
    strategies["BUY_AND_HOLD"] = bh
    positions["BUY_AND_HOLD"] = pd.Series(1.0, index=bh.index)

    def simulate(name: str, signal, return_col: str = "fwd_ret") -> None:
        frame = preds.assign(signal=np.asarray(signal))
        frame = frame[(frame["signal"] != 0) & frame[return_col].notna()]
        if frame.empty:
            strategies[name] = pd.Series(dtype=float)
            positions[name] = pd.Series(dtype=float)
            return
        gross = frame.groupby("date").apply(
            lambda g: float(np.average(g["signal"] * g[return_col])), include_groups=False)
        exposure = frame.groupby("date")["signal"].apply(lambda s: float(np.abs(s).mean()))
        # turnover: every session the position set is re-established for a 1-session horizon
        turnover = exposure * 2.0
        net = gross - turnover.reindex(gross.index).fillna(0) * \
            (costs.commission_bps + costs.slippage_bps + costs.impact_bps) / 10_000
        strategies[name] = net.reindex(bh.index).fillna(0.0)
        positions[name] = exposure.reindex(bh.index).fillna(0.0)

    simulate("ALWAYS_LONG_SIGNAL", np.where(preds["p"] >= 0.5, 1.0, -1.0))
    simulate("MODEL", np.where(preds["p"] >= 0.5 + band, 1.0,
                               np.where(preds["p"] <= 0.5 - band, -1.0, 0.0)))
    gate_ok = ((preds["p"] - 0.5).abs() >= band) & \
              (preds["model_agreement"].fillna(0) >= gate_min_agreement) & \
              (preds["uncertainty"] <= gate_max_uncertainty) & \
              (preds["edge_bps"] >= costs.round_trip_bps + float(cfg.get("decision.min_edge_bps", 12.0)))
    gate_signal = np.where(gate_ok & (preds["p"] >= 0.5), 1.0,
                           np.where(gate_ok & (preds["p"] < 0.5), -1.0, 0.0))
    simulate("ADAPTIVE_GATE", gate_signal)
    simulate("ADAPTIVE_GATE_T1", gate_signal, return_col="fwd_ret_t1")

    results = {}
    for name, series in strategies.items():
        if series.empty:
            results[name] = {"available": False, "reason": "strategy never traded"}
            continue
        summary = performance_summary(series, rf=0.065, confidence=float(cfg.get("risk.var_confidence", 0.95)))
        active = positions[name]
        summary.update({
            "available": True,
            "mean_exposure": round(float(active.mean()), 4),
            "sessions_traded": int((active > 0).sum()),
            "abstention_rate_pct": round(100 * float((active == 0).mean()), 2),
            "equity_curve": _equity_curve(series)[::5],
        })
        results[name] = summary

    trades = int(gate_ok.sum())
    out = {
        "horizon": horizon,
        "window": {"start": str(preds["date"].min().date()), "end": str(preds["date"].max().date())},
        "costs": costs.to_dict(),
        "strategies": results,
        "execution_convention": {
            "signal_information_cutoff": "close of session t",
            "entry": "close of session t",
            "exit": f"close of session t+{horizon}",
            "realised_return": f"fwd_ret_{horizon}[t] = close[t+{horizon}] / close[t] - 1",
            "cost_per_position": "round trip = 2 x (commission_bps + slippage_bps + impact_bps) "
                                 f"= {costs.round_trip_bps:.1f} bps",
            "position_sizing": "equal weight across the symbols traded in a session, no leverage",
            "sensitivity": "ADAPTIVE_GATE_T1 is the identical gate executed one session later "
                           "(entry at close t+1), quantifying the optimism of same-close execution",
            "excluded": ["taxes", "financing", "borrow availability for shorts",
                         "intraday price paths", "market impact beyond the fixed impact_bps"],
        },
        "signals_evaluated": int(len(preds)),
        "gate_trades": trades,
        "gate_abstention_pct": round(100 * (1 - trades / max(len(preds), 1)), 2),
        "honest_note": (
            "Returns are gross of taxes and financing, assume entry at the session-t close and "
            "exit at the session t+h close, and ignore borrow constraints on short positions. The same-close execution assumption is optimistic — ADAPTIVE_GATE_T1 shows the same policy executed one session later. The universe is 14 large-cap NSE "
            "names over a single historical window — treat all figures as a research result, not "
            "an investable track record."),
    }
    (cfg.reports_root / "results" / f"backtest_h{horizon}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("backtest: %s", {k: (v.get("annualised_return_pct"), v.get("sharpe"))
                              for k, v in results.items() if v.get("available")})
    return out
