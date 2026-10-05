"""Research-integrity tests: VIGIL must be allowed to report that it did not work."""
from __future__ import annotations

from conftest import result  # noqa: E402


def test_experiments_report_verdicts_including_failures():
    e = result("experiments_h1.json")
    verdicts = {x["experiment_id"]: x.get("conclusion", {}).get("verdict")
                for x in e["experiments"] if x.get("available")}
    assert verdicts, "no experiments available"
    assert set(verdicts.values()) <= {"SUPPORTED", "NOT SUPPORTED", "INCONCLUSIVE", "REFERENCE"}
    assert "NOT SUPPORTED" in verdicts.values(), (
        "every hypothesis supported is a red flag for result manipulation")


def test_backtest_reports_losses_honestly():
    b = result("backtest_h1.json")
    rets = {k: v.get("annualised_return_pct") for k, v in b["strategies"].items() if v.get("available")}
    assert rets
    assert any(v is not None and v < 0 for v in rets.values()), (
        "no losing strategy — results look curated")
    assert b.get("honest_note")


def test_calibration_reports_both_directions():
    c = result("calibration_h1.json")
    assert "before" in c and "after" in c
    assert c["verdict"], "calibration must state whether it helped or hurt"


def test_tournament_includes_a_baseline_to_beat():
    t = result("tournament_h1.json")
    ids = {m["model_id"] for m in t["leaderboard"]}
    assert any("baseline" in i or "majority" in i for i in ids), "no baseline in the tournament"


def test_failure_lab_attributes_misses_to_more_than_one_cause():
    f = result("failure_lab_h1.json")
    attribution = {k: v for k, v in f["failure_attribution"].items() if v > 0}
    assert len(attribution) >= 3, "single-bucket attribution indicates a tagging bug"
    assert f["n_misses"] > 0


def test_decision_quality_separates_prediction_from_policy_from_outcome():
    """v1.0: the three layers stay separate and no single score absorbs them."""
    q = result("decision_quality_h1.json")
    assert "good_decision_rate_pct" not in q, "the withdrawn inflated metric must stay withdrawn"
    assert {"forecast_accuracy_pct", "selective_accuracy_pct", "action_coverage_pct",
            "decision_utility_bps", "outcome_quality"} <= set(q)
    assert q["forecast_accuracy_pct"] != q["selective_accuracy_pct"], (
        "accuracy over all opportunities and over the acted subset must be measured separately")
    assert q["decision_utility_bps"] <= abs(q["mean_net_return_when_acted_bps"]), (
        "utility per opportunity cannot exceed the per-trade return it is scaled from")
