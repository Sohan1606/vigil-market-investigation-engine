"""Market memory — a pattern library with explicit detection logic and measured historical behaviour.

Every statistic in this module is computed from the curated history. Forward-behaviour numbers are
DESCRIPTIVE (what happened after past occurrences), never a promise about the future; the UI labels
them as such.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from .regime import market_state_frame

log = get_logger("vigil.intelligence.patterns")


def _detectors() -> Dict[str, Dict[str, object]]:
    return {
        "VOLATILITY_EXPANSION": {
            "definition": "20-session realised volatility rises above 1.4x its 60-session level.",
            "logic": "vol_20 / vol_60 > 1.4",
            "fn": lambda s: (s["bench_vol_20"] / s["bench_vol_60"]) > 1.4,
        },
        "BROAD_SELLOFF": {
            "definition": "Benchmark falls while fewer than 25% of the universe advances.",
            "logic": "bench_ret_1 < -0.8% and market_breadth < 0.25",
            "fn": lambda s: (s["bench_ret_1"] < -0.008) & (s["market_breadth"] < 0.25),
        },
        "MOMENTUM_ACCELERATION": {
            "definition": "Positive 20-session trend with broad participation and contained volatility.",
            "logic": "bench_trend_20 > 3% and market_breadth_20 > 0.55 and vol_20 < vol_60",
            "fn": lambda s: (s["bench_trend_20"] > 0.03) & (s["market_breadth_20"] > 0.55) &
                            (s["bench_vol_20"] < s["bench_vol_60"]),
        },
        "RECOVERY": {
            "definition": "Drawdown improves by more than 3 points over 10 sessions from a deep level.",
            "logic": "drawdown < -5% 10 sessions ago and drawdown improves > 3 points",
            "fn": lambda s: (s["drawdown"].shift(10) < -0.05) & ((s["drawdown"] - s["drawdown"].shift(10)) > 0.03),
        },
        "DISPERSION_SHOCK": {
            "definition": "Cross-sectional dispersion jumps above its 95th historical percentile.",
            "logic": "dispersion_20 > expanding 95th percentile",
            "fn": lambda s: s["dispersion_20"] > s["dispersion_20"].expanding(250).quantile(0.95),
        },
        "BREADTH_DIVERGENCE": {
            "definition": "Benchmark advances while market breadth stays weak — narrow leadership.",
            "logic": "bench_trend_20 > 1% and market_breadth_20 < 0.45",
            "fn": lambda s: (s["bench_trend_20"] > 0.01) & (s["market_breadth_20"] < 0.45),
        },
    }


def build_pattern_library(cfg: Optional[VigilConfig] = None, horizon: int = 5) -> List[Dict]:
    cfg = cfg or load_config()
    state = market_state_frame(cfg).sort_values("date").reset_index(drop=True)
    bench_close = state.set_index("date")["bench_ret_1"].add(1).cumprod()
    fwd = bench_close.shift(-horizon) / bench_close - 1.0
    state["fwd_bench_ret"] = fwd.values

    library: List[Dict] = []
    for name, spec in _detectors().items():
        mask = spec["fn"](state).fillna(False)  # type: ignore[operator]
        occ = state[mask]
        fwd_vals = occ["fwd_bench_ret"].dropna()
        base = state["fwd_bench_ret"].dropna()
        entry = {
            "pattern_id": name,
            "definition": spec["definition"],
            "detection_logic": spec["logic"],
            "occurrences": int(len(occ)),
            "frequency_pct": round(100.0 * len(occ) / max(len(state), 1), 2),
            "first_seen": str(occ["date"].min().date()) if len(occ) else None,
            "last_seen": str(occ["date"].max().date()) if len(occ) else None,
            "forward_horizon_sessions": horizon,
            "historical_forward_mean_pct": round(100 * float(fwd_vals.mean()), 3) if len(fwd_vals) else None,
            "historical_forward_median_pct": round(100 * float(fwd_vals.median()), 3) if len(fwd_vals) else None,
            "historical_up_rate_pct": round(100 * float((fwd_vals > 0).mean()), 2) if len(fwd_vals) else None,
            "baseline_up_rate_pct": round(100 * float((base > 0).mean()), 2),
            "sample_warning": "small sample (<30 occurrences) — treat as anecdotal"
                              if len(fwd_vals) < 30 else None,
            "currently_active": bool(mask.iloc[-1]) if len(mask) else False,
            "disclaimer": "Descriptive statistics of past occurrences. Not a forecast.",
        }
        library.append(entry)

    store = DocumentStore(cfg)
    store.drop("patterns")
    store.insert_many("patterns", library)
    (cfg.reports_root / "results" / "patterns.json").write_text(json.dumps(library, indent=2), encoding="utf-8")
    log.info("pattern library: %d patterns, %d currently active",
             len(library), sum(1 for p in library if p["currently_active"]))
    return library
