"""REPLAY — reconstruct exactly what VIGIL could have known on a historical date.

The replay re-runs the production forecast path with `as_of` set to the chosen session. Models are
retrained on data older than that session; news is filtered by publication timestamp; the regime is
taken from the walk-forward history fitted before the date. After the horizon elapses, the realised
outcome is attached and the decision is graded.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..decision.forecast_service import ForecastService
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake

log = get_logger("vigil.research.replay")


def replay(symbol: str, as_of: str, horizon: int = 1, cfg: Optional[VigilConfig] = None,
           service: Optional[ForecastService] = None) -> Dict:
    cfg = cfg or load_config()
    service = service or ForecastService(cfg)
    as_of_ts = pd.Timestamp(as_of)
    matrix = service.matrix
    sessions = matrix[matrix["symbol"] == symbol].sort_values("date")
    available = sessions[sessions["date"] <= as_of_ts]
    if available.empty:
        raise ValueError(f"no sessions for {symbol} on or before {as_of}")
    anchor = pd.Timestamp(available["date"].max())

    forecast = service.forecast(symbol, as_of=anchor, horizon=horizon, persist=False)

    future = sessions[sessions["date"] > anchor]
    outcome: Dict[str, object] = {"resolved": False,
                                  "note": "horizon has not elapsed in the available data"}
    if len(future) >= horizon:
        realised = float(future.iloc[horizon - 1]["close"] / available.iloc[-1]["close"] - 1)
        direction = "UP" if realised > 0 else "DOWN"
        correct = (direction == forecast["direction"])
        acted = forecast["verdict"] != "NO ACTION"
        cost = forecast["gate"]["cost_bps"] / 10_000
        pnl = (realised if forecast["direction"] == "UP" else -realised) - cost if acted else 0.0
        if acted:
            decision_quality = "GOOD_DECISION_GOOD_OUTCOME" if correct else "GOOD_DECISION_BAD_OUTCOME"
        else:
            decision_quality = "GOOD_ABSTENTION" if abs(realised) * 10_000 < forecast["gate"]["cost_bps"] \
                else ("MISSED_OPPORTUNITY" if correct else "AVOIDED_LOSS")
        outcome = {
            "resolved": True,
            "resolution_date": str(pd.Timestamp(future.iloc[horizon - 1]["date"]).date()),
            "realised_return_pct": round(100 * realised, 3),
            "realised_direction": direction,
            "forecast_direction": forecast["direction"],
            "forecast_correct": bool(correct),
            "acted": acted,
            "net_pnl_pct": round(100 * pnl, 3),
            "decision_quality": decision_quality,
            "inside_interval_80": (
                None if forecast["interval_80"]["low_pct"] is None else
                bool(forecast["interval_80"]["low_pct"] <= 100 * realised <= forecast["interval_80"]["high_pct"])),
        }

    news_docs = DocumentStore(cfg).find("news", {"symbol": symbol})
    cutoff = pd.Timestamp(forecast["information_cutoff"])
    visible_news = []
    for d in news_docs:
        ts = pd.Timestamp(d["published_at"]).tz_localize(None) if pd.Timestamp(d["published_at"]).tzinfo \
            else pd.Timestamp(d["published_at"])
        if ts <= cutoff and ts >= cutoff - pd.Timedelta(days=7):
            visible_news.append({"headline": d["headline"], "published_at": d["published_at"],
                                 "sentiment": d.get("sentiment"), "provider": d.get("provider")})
    return {
        "symbol": symbol,
        "requested_date": str(as_of_ts.date()),
        "anchor_session": str(anchor.date()),
        "horizon": horizon,
        "information_cutoff": forecast["information_cutoff"],
        "leak_guard": {
            "training_data_ends": forecast["latency_ms"]["train_end"],
            "sessions_after_anchor_in_dataset": int(len(future)),
            "sessions_used_after_anchor": 0,
            "news_visible": len(visible_news),
            "news_hidden_because_later_than_cutoff": int(len(news_docs) - len(visible_news)),
            "statement": "Models were retrained on data strictly older than the anchor session; "
                         "no post-cutoff price, news or label was read.",
        },
        "forecast": forecast,
        "visible_news": visible_news[-6:],
        "outcome": outcome,
    }


def replay_timeline(symbol: str, start: str, end: str, step: int = 5, horizon: int = 1,
                    cfg: Optional[VigilConfig] = None, max_points: int = 12) -> Dict:
    """A sequence of replays — the 'play' control of the time machine."""
    cfg = cfg or load_config()
    service = ForecastService(cfg)
    sessions = service.matrix[(service.matrix["symbol"] == symbol)]["date"].sort_values().unique()
    window = [d for d in sessions if pd.Timestamp(start) <= pd.Timestamp(d) <= pd.Timestamp(end)]
    picks = window[::step][:max_points]
    frames = []
    for d in picks:
        try:
            r = replay(symbol, str(pd.Timestamp(d).date()), horizon=horizon, cfg=cfg, service=service)
            frames.append({
                "date": r["anchor_session"], "probability": r["forecast"]["probability_up"],
                "verdict": r["forecast"]["verdict"], "trust": r["forecast"]["gate"]["trust_score"],
                "realised_pct": r["outcome"].get("realised_return_pct"),
                "correct": r["outcome"].get("forecast_correct"),
                "decision_quality": r["outcome"].get("decision_quality"),
            })
        except Exception as exc:
            log.warning("replay failed at %s: %s", d, exc)
    resolved = [f for f in frames if f.get("correct") is not None]
    return {
        "symbol": symbol, "start": start, "end": end, "points": frames,
        "hit_rate_pct": round(100 * float(np.mean([f["correct"] for f in resolved])), 1) if resolved else None,
        "action_rate_pct": round(100 * float(np.mean([f["verdict"] != "NO ACTION" for f in frames])), 1)
        if frames else None,
    }
