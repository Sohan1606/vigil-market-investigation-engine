"""Decision-layer behaviour: the gate must be able to say NO ACTION, and costs must bite."""
from __future__ import annotations

from vigil.decision.costs import CostModel
from vigil.decision.gate import evaluate_gate


def _gate(**over):
    base = dict(probability=0.62, expected_move_pct=1.4, data_quality=96.0, model_agreement=0.8,
                uncertainty=0.25, calibration_ece=0.01, drift_state="HEALTHY", regime="BULL",
                regime_confidence=0.8, risk_score=0.3, stability_score=0.85, modality_coverage=1.0)
    base.update(over)
    return evaluate_gate(**base)


def test_a_strong_well_supported_forecast_can_pass_the_gate():
    g = _gate()
    assert g.verdict == "POSITIVE BIAS"
    assert g.net_edge_bps > 0
    assert g.trust_score > 50


def test_no_action_is_reachable_and_explained():
    g = _gate(probability=0.505, expected_move_pct=0.05)
    assert g.verdict == "NO ACTION"
    assert g.blocking_reasons, "NO ACTION must name what blocked it"
    assert g.reason


def test_bad_data_quality_blocks_regardless_of_confidence():
    g = _gate(data_quality=40.0)
    assert g.verdict == "NO ACTION"
    assert any("DATA" in r.upper() or "QUALITY" in r.upper() for r in g.blocking_reasons)


def test_degraded_models_block_action():
    g = _gate(drift_state="DEGRADED")
    assert g.verdict == "NO ACTION"


def test_disagreement_blocks_action():
    g = _gate(model_agreement=0.3)
    assert g.verdict == "NO ACTION"


def test_negative_direction_produces_negative_bias_not_an_error():
    g = _gate(probability=0.35)
    assert g.verdict in {"NEGATIVE BIAS", "NO ACTION"}
    assert g.direction == "DOWN"


def test_costs_are_charged_on_a_round_trip():
    cm = CostModel(commission_bps=3, slippage_bps=5, impact_bps=1)
    rt = cm.round_trip_bps
    assert rt == (3 + 5 + 1) * 2, "a round trip must charge entry and exit"
    assert cm.breakeven_edge_bps() == rt
    assert cm.cost_for_turnover(1.0) == (3 + 5 + 1) / 10_000.0


def test_a_tiny_edge_cannot_survive_costs():
    g = _gate(expected_move_pct=0.05, probability=0.53)
    assert g.net_edge_bps < g.expected_edge_bps
    assert g.verdict == "NO ACTION"


def test_cost_model_reads_the_project_config():
    from vigil.config import load_config
    cm = CostModel.from_config(load_config())
    assert cm.round_trip_bps > 0
