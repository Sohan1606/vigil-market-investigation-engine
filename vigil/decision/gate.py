"""THE DECISION GATE — where a forecast becomes (or fails to become) a verdict.

Inputs are nine independent checks. A forecast is only promoted to POSITIVE/NEGATIVE BIAS when the
expected edge survives costs AND every reliability check clears its floor. Otherwise the verdict is
NO ACTION — which VIGIL treats as a legitimate, frequently correct answer rather than a failure.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np

from ..config import VigilConfig, load_config
from .costs import CostModel

POSITIVE, NEGATIVE, NO_ACTION = "POSITIVE BIAS", "NEGATIVE BIAS", "NO ACTION"


@dataclass
class GateCheck:
    name: str
    value: float
    threshold: float
    passed: bool
    weight: float
    explanation: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class GateResult:
    verdict: str
    direction: str
    probability: float
    expected_edge_bps: float
    cost_bps: float
    net_edge_bps: float
    trust_score: float
    checks: List[GateCheck] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


def evaluate_gate(
    probability: float,
    expected_move_pct: float,
    data_quality: float,
    model_agreement: float,
    uncertainty: float,
    calibration_ece: float,
    drift_state: str,
    regime: str,
    regime_confidence: float,
    risk_score: float,
    stability_score: float,
    modality_coverage: float,
    cfg: Optional[VigilConfig] = None,
    cost_model: Optional[CostModel] = None,
) -> GateResult:
    cfg = cfg or load_config()
    costs = cost_model or CostModel.from_config(cfg)

    direction = "UP" if probability >= 0.5 else "DOWN"
    conviction = abs(probability - 0.5)
    # Expected edge: directional conviction applied to the expected absolute move, in bps.
    expected_edge_bps = float(2 * conviction * abs(expected_move_pct) * 10_000)
    cost_bps = costs.round_trip_bps
    net_edge_bps = expected_edge_bps - cost_bps

    th = {
        "data_quality": float(cfg.get("decision.min_data_quality", 85.0)),
        "edge": float(cfg.get("decision.min_edge_bps", 12.0)),
        "agreement": float(cfg.get("decision.min_model_agreement", 0.55)),
        "uncertainty": float(cfg.get("decision.max_uncertainty", 0.62)),
        "abstain_band": float(cfg.get("decision.abstain_band", 0.055)),
    }

    checks: List[GateCheck] = [
        GateCheck("DATA QUALITY", round(data_quality, 1), th["data_quality"],
                  data_quality >= th["data_quality"], 0.15,
                  "Share of inputs that passed the data contract at the information cutoff."),
        GateCheck("FORECAST CONVICTION", round(conviction, 4), th["abstain_band"],
                  conviction >= th["abstain_band"], 0.20,
                  "Distance of the calibrated probability from a coin flip."),
        GateCheck("MODEL AGREEMENT", round(model_agreement, 3), th["agreement"],
                  model_agreement >= th["agreement"], 0.15,
                  "Fraction of ensemble members pointing the same way."),
        GateCheck("UNCERTAINTY", round(uncertainty, 3), th["uncertainty"],
                  uncertainty <= th["uncertainty"], 0.15,
                  "Combined sharpness deficit, member spread and disagreement (lower is better)."),
        GateCheck("CALIBRATION", round(calibration_ece, 4), 0.06, calibration_ece <= 0.06, 0.08,
                  "Expected calibration error of the live probability model."),
        GateCheck("MODEL HEALTH", float(0 if drift_state in ("HEALTHY", "WATCH") else 1), 0.0,
                  drift_state in ("HEALTHY", "WATCH"), 0.10,
                  f"Drift monitor reports {drift_state}."),
        GateCheck("MARKET STABILITY", round(100 * regime_confidence, 1), 45.0,
                  regime_confidence >= 0.45 and regime != "TRANSITION", 0.07,
                  f"Regime {regime} held with {100*regime_confidence:.0f}% confidence."),
        GateCheck("RISK", round(risk_score, 1), 75.0, risk_score <= 75.0, 0.05,
                  "Position-level volatility, beta and recent drawdown."),
        GateCheck("COST ADVANTAGE", round(net_edge_bps, 1), th["edge"], net_edge_bps >= th["edge"], 0.05,
                  f"Expected edge {expected_edge_bps:.0f} bps vs round-trip cost {cost_bps:.0f} bps."),
    ]
    # forecast stability and modality coverage modulate trust but are reported separately
    trust = float(np.clip(
        sum(c.weight * (1.0 if c.passed else 0.0) for c in checks) * 100
        * (0.85 + 0.15 * np.clip(stability_score, 0, 1))
        * (0.9 + 0.1 * np.clip(modality_coverage, 0, 1)), 0, 100))

    blocking = [c.name for c in checks if not c.passed]
    if not blocking:
        verdict = POSITIVE if direction == "UP" else NEGATIVE
        reason = (f"Edge of {net_edge_bps:.0f} bps net of costs, with {model_agreement*100:.0f}% model "
                  f"agreement and acceptable uncertainty in a {regime.replace('_', ' ').lower()} market.")
    else:
        verdict = NO_ACTION
        primary = blocking[0]
        detail = {
            "COST ADVANTAGE": f"the {expected_edge_bps:.0f} bps expected edge does not clear the "
                              f"{cost_bps:.0f} bps round-trip cost",
            "FORECAST CONVICTION": "the forecast is too close to a coin flip",
            "MODEL AGREEMENT": "the models disagree about direction",
            "UNCERTAINTY": "uncertainty is too high for the size of the edge",
            "DATA QUALITY": "the input data did not satisfy the data contract",
            "CALIBRATION": "the probability model is not sufficiently calibrated right now",
            "MODEL HEALTH": "a model in the live ensemble is degraded",
            "MARKET STABILITY": "the market regime is unresolved",
            "RISK": "position risk is too high relative to the edge",
        }.get(primary, "a reliability check failed")
        reason = f"Forecast exists, but {detail}."
    return GateResult(verdict=verdict, direction=direction, probability=round(float(probability), 4),
                      expected_edge_bps=round(expected_edge_bps, 1), cost_bps=round(cost_bps, 1),
                      net_edge_bps=round(net_edge_bps, 1), trust_score=round(trust, 1),
                      checks=checks, blocking_reasons=blocking, reason=reason)
