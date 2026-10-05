"""FORECAST STRESS — re-run the Decision Gate under adverse but plausible conditions.

Scenarios perturb the *inputs to the gate*, not the outcome: higher volatility, hostile sentiment,
a model dropping out, the news channel disappearing, delayed data, and a broad market shock.
The output shows whether the verdict survives, which is a far more useful statement than a single
point forecast.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from ..config import VigilConfig, load_config
from .costs import CostModel
from .gate import evaluate_gate

SCENARIOS = [
    {"id": "VOLATILITY_X2", "label": "Volatility doubles",
     "description": "Realised volatility doubles, widening the expected move and the risk score."},
    {"id": "NEGATIVE_SENTIMENT", "label": "Sentiment turns hostile",
     "description": "Evidence conflict rises; probability is shrunk toward the mean."},
    {"id": "SECTOR_REVERSAL", "label": "Sector reverses",
     "description": "Sector context flips, reducing agreement and conviction."},
    {"id": "MARKET_SHOCK", "label": "Broad market shock",
     "description": "Regime confidence collapses and risk rises sharply."},
    {"id": "MODEL_REMOVED", "label": "Strongest model unavailable",
     "description": "The highest-weighted model is dropped and the ensemble re-weighted."},
    {"id": "NEWS_MISSING", "label": "News modality lost",
     "description": "Headline coverage disappears; modality coverage falls."},
    {"id": "DELAYED_DATA", "label": "Data arrives late",
     "description": "Inputs are stale: data quality drops and uncertainty rises."},
]


def stress_forecast(forecast: Dict, cfg: Optional[VigilConfig] = None) -> Dict:
    cfg = cfg or load_config()
    costs = CostModel.from_config(cfg)
    base = {
        "probability": float(forecast["probability_up"]),
        "expected_move_pct": float(forecast["expected_move_pct"]) / 100.0,
        "data_quality": float(forecast["data_quality"]["score"]),
        "model_agreement": float(forecast["model_agreement"]),
        "uncertainty": float(forecast["uncertainty"]["uncertainty"]),
        "calibration_ece": 0.02,
        "drift_state": "HEALTHY",
        "regime": forecast["regime"]["state"],
        "regime_confidence": float(forecast["regime"]["confidence"]),
        "risk_score": float(forecast["risk"]["risk_score"]),
        "stability_score": float(forecast["stability"].get("stability_score", 0.5)),
        "modality_coverage": float(forecast["modality"]["coverage"]),
    }
    members = {k: v for k, v in forecast["member_probabilities"].items() if v is not None}
    weights = forecast.get("member_weights", {})

    results: List[Dict] = []
    for sc in SCENARIOS:
        p = dict(base)
        if sc["id"] == "VOLATILITY_X2":
            p["expected_move_pct"] *= 2.0
            p["risk_score"] = min(100.0, p["risk_score"] * 1.6)
            p["uncertainty"] = min(1.0, p["uncertainty"] + 0.12)
        elif sc["id"] == "NEGATIVE_SENTIMENT":
            p["probability"] = 0.5 + (p["probability"] - 0.5) * 0.6
            p["uncertainty"] = min(1.0, p["uncertainty"] + 0.1)
        elif sc["id"] == "SECTOR_REVERSAL":
            p["model_agreement"] = max(0.0, p["model_agreement"] - 0.25)
            p["probability"] = 0.5 + (p["probability"] - 0.5) * 0.7
        elif sc["id"] == "MARKET_SHOCK":
            p["regime"], p["regime_confidence"] = "TRANSITION", min(p["regime_confidence"], 0.4)
            p["risk_score"] = min(100.0, p["risk_score"] + 25)
            p["expected_move_pct"] *= 1.5
        elif sc["id"] == "MODEL_REMOVED":
            if len(members) > 1:
                strongest = max(members, key=lambda m: weights.get(m, 0))
                kept = {k: v for k, v in members.items() if k != strongest}
                w = {k: weights.get(k, 1 / len(kept)) for k in kept}
                total = sum(w.values()) or 1.0
                p["probability"] = float(sum(kept[k] * w[k] for k in kept) / total)
                dirs = [1 if v >= 0.5 else 0 for v in kept.values()]
                p["model_agreement"] = float(max(np.mean(dirs), 1 - np.mean(dirs)))
                p["uncertainty"] = min(1.0, p["uncertainty"] + 0.08)
        elif sc["id"] == "NEWS_MISSING":
            p["modality_coverage"] = max(0.0, p["modality_coverage"] - 0.2)
            p["uncertainty"] = min(1.0, p["uncertainty"] + 0.05)
        elif sc["id"] == "DELAYED_DATA":
            p["data_quality"] = max(0.0, p["data_quality"] - 18)
            p["uncertainty"] = min(1.0, p["uncertainty"] + 0.07)
        gate = evaluate_gate(cfg=cfg, cost_model=costs, **p)
        results.append({
            "scenario_id": sc["id"], "label": sc["label"], "description": sc["description"],
            "verdict": gate.verdict, "probability": gate.probability,
            "trust_score": gate.trust_score, "net_edge_bps": gate.net_edge_bps,
            "blocking_reasons": gate.blocking_reasons,
            "verdict_changed": gate.verdict != forecast["verdict"],
        })
    survived = sum(1 for r in results if not r["verdict_changed"])
    return {
        "forecast_id": forecast["forecast_id"],
        "base_verdict": forecast["verdict"],
        "scenarios": results,
        "robustness_pct": round(100 * survived / len(results), 1),
        "interpretation": ("The verdict is robust to the stress set." if survived == len(results)
                           else f"{len(results) - survived} of {len(results)} scenarios flip the verdict — "
                                f"treat the conclusion as condition-dependent."),
    }
