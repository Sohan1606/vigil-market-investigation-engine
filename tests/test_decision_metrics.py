"""Decision-metric tests (v1.0 defect #3): abstention must never score well.

The removed `good_decision_rate_pct` counted every below-threshold abstention as a good decision,
so a system that never acted scored ~100%. These tests pin the replacement definitions.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from conftest import result

from vigil.decision.quality import CATEGORIES, METRIC_DEFINITIONS, classify


def test_the_discredited_metric_is_gone_everywhere():
    q = result("decision_quality_h1.json")
    blob = json.dumps(q)
    assert "good_decision_rate_pct" not in blob.replace(q.get("removed_metric_note", ""), "")
    assert "GOOD_ABSTENTION" not in blob


def test_published_metric_set_is_complete_and_defined():
    q = result("decision_quality_h1.json")
    for key in ("forecast_accuracy_pct", "selective_accuracy_pct", "action_coverage_pct",
                "abstention_rate_pct", "mean_net_return_when_acted_bps", "decision_utility_bps",
                "downside_avoidance_bps", "outcome_quality"):
        assert key in q, key
    assert len(q["metric_definitions"]) >= 8
    assert set(METRIC_DEFINITIONS) >= {"decision_utility_bps", "downside_avoidance_bps"}


def test_decision_utility_is_zero_when_the_system_never_acts():
    """The defining property of the replacement metric: inaction earns nothing."""
    coverage, net = 0.0, -20.0
    assert coverage * net == 0.0
    q = result("decision_quality_h1.json")
    expected = q["action_coverage_pct"] / 100 * q["mean_net_return_when_acted_bps"]
    assert q["decision_utility_bps"] == pytest.approx(expected, abs=0.05)


def test_coverage_and_abstention_are_complementary():
    q = result("decision_quality_h1.json")
    assert q["action_coverage_pct"] + q["abstention_rate_pct"] == pytest.approx(100.0, abs=0.01)


def test_selective_accuracy_is_measured_on_the_acted_subset_only():
    q = result("decision_quality_h1.json")
    trades = q["outcome_quality"]["trades"]
    assert trades == q["categories"]["ACTED_WITH_EDGE_CORRECT"] + \
        q["categories"]["ACTED_WITH_EDGE_INCORRECT"] + q["categories"]["ACTED_WITHOUT_EDGE"]
    hit = q["categories"]["ACTED_WITH_EDGE_CORRECT"] / max(trades, 1) * 100
    assert q["selective_accuracy_pct"] == pytest.approx(hit, abs=0.6)


def test_categories_are_descriptive_not_verdicts():
    for name in CATEGORIES:
        assert not name.startswith("GOOD") and not name.startswith("BAD"), name
    row = pd.Series({"acted": False, "correct": False, "edge_bps": 1.0})
    assert classify(row, 30.0) == "ABSTAINED_BELOW_EDGE"
    row = pd.Series({"acted": False, "correct": True, "edge_bps": 99.0})
    assert classify(row, 30.0) == "ABSTAINED_DESPITE_EDGE"
    row = pd.Series({"acted": True, "correct": True, "edge_bps": 99.0})
    assert classify(row, 30.0) == "ACTED_WITH_EDGE_CORRECT"
    row = pd.Series({"acted": True, "correct": True, "edge_bps": 1.0})
    assert classify(row, 30.0) == "ACTED_WITHOUT_EDGE"


def test_policy_adherence_is_quarantined_as_a_diagnostic():
    q = result("decision_quality_h1.json")
    assert "policy_adherence_pct" in q["diagnostics"]
    assert "not a measure of decision quality" in q["diagnostics"]["caveat"].lower()
    assert "policy_adherence_pct" not in {k for k in q if k != "diagnostics"}
