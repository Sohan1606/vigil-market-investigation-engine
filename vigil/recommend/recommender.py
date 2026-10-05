"""Risk-aware RESEARCH recommendation engine (not investment advice).

Transparent scoring: every candidate's score decomposes into named components with weights the
user can see. Preferences (risk, sector, volatility, horizon) reshape the weights, never the data.
A candidate whose Decision Gate verdict is NO ACTION is listed as 'WATCH ONLY' rather than hidden,
because the reason it is not actionable is itself the useful output.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore

log = get_logger("vigil.recommend")

RISK_PROFILES = {
    "CONSERVATIVE": {"vol_penalty": 1.0, "trust_weight": 0.45, "edge_weight": 0.20, "risk_weight": 0.35},
    "BALANCED": {"vol_penalty": 0.6, "trust_weight": 0.35, "edge_weight": 0.35, "risk_weight": 0.30},
    "AGGRESSIVE": {"vol_penalty": 0.25, "trust_weight": 0.25, "edge_weight": 0.55, "risk_weight": 0.20},
}


def recommend(risk_profile: str = "BALANCED", sectors: Optional[List[str]] = None,
              max_volatility_pct: Optional[float] = None, horizon: int = 1,
              limit: int = 6, cfg: Optional[VigilConfig] = None) -> Dict:
    cfg = cfg or load_config()
    store = DocumentStore(cfg)
    profile = RISK_PROFILES.get(risk_profile.upper(), RISK_PROFILES["BALANCED"])
    forecasts = [f for f in store.find("forecasts") if f.get("horizon") == horizon]
    if not forecasts:
        return {"available": False, "reason": "no forecasts available — run the pipeline first"}

    rows = []
    for f in forecasts:
        sector = f.get("sector", "UNKNOWN")
        vol = float(f.get("risk", {}).get("annualised_vol_pct", 0.0))
        if sectors and sector not in sectors:
            continue
        if max_volatility_pct is not None and vol > max_volatility_pct:
            continue
        trust = float(f["gate"]["trust_score"]) / 100.0
        edge = float(f["gate"]["net_edge_bps"])
        risk_score = float(f["risk"]["risk_score"]) / 100.0
        conviction = abs(float(f["probability_up"]) - 0.5) * 2
        edge_norm = float(np.clip((edge + 40) / 80, 0, 1))
        score = 100 * (profile["trust_weight"] * trust +
                       profile["edge_weight"] * edge_norm +
                       profile["risk_weight"] * (1 - risk_score) -
                       profile["vol_penalty"] * 0.15 * min(vol / 45.0, 1.0))
        rows.append({
            "symbol": f["symbol"], "name": f.get("name"), "sector": sector,
            "verdict": f["verdict"],
            "status": "CANDIDATE" if f["verdict"] != "NO ACTION" else "WATCH ONLY",
            "score": round(float(score), 2),
            "components": {
                "forecast_trust": round(100 * trust, 1),
                "net_edge_bps": round(edge, 1),
                "risk_score": round(100 * risk_score, 1),
                "annualised_vol_pct": round(vol, 1),
                "conviction": round(conviction, 3),
            },
            "weights_used": profile,
            "why": (f"Trust {100*trust:.0f}/100, net edge {edge:+.0f} bps after costs, "
                    f"risk {100*risk_score:.0f}/100, volatility {vol:.0f}% annualised. "
                    + ("Gate cleared." if f["verdict"] != "NO ACTION"
                       else f"Gate blocked on {', '.join(f['gate']['blocking_reasons'][:2])}.")),
            "forecast_id": f["forecast_id"],
        })
    rows.sort(key=lambda r: -r["score"])
    out = {
        "available": True,
        "risk_profile": risk_profile.upper(),
        "filters": {"sectors": sectors, "max_volatility_pct": max_volatility_pct, "horizon": horizon},
        "scoring": "score = w_trust·trust + w_edge·normalised_net_edge + w_risk·(1 − risk) − vol penalty",
        "candidates": rows[:limit],
        "watch_only": [r for r in rows if r["status"] == "WATCH ONLY"][:limit],
        "disclaimer": "Research ranking for study purposes. Not investment advice. "
                      "No guarantee of future returns.",
    }
    return out
