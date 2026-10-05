"""Execution-semantics tests (v1.0 defect #5).

One convention, stated once and obeyed everywhere: the signal uses information up to the close of
session t, the position is opened at that close and closed at the close of session t+h, and pays
a full round trip. These tests check the code, the artefact and the documentation agree.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT, result

from vigil.decision.costs import CostModel


def test_label_matches_the_documented_return_definition(matrix):
    """fwd_ret_1[t] must equal close[t+1]/close[t]-1 for every symbol."""
    sub = matrix.sort_values(["symbol", "date"]).groupby("symbol", observed=True).head(50)
    for _sym, g in sub.groupby("symbol", observed=True):
        expected = g["close"].shift(-1) / g["close"] - 1.0
        mask = expected.notna() & g["fwd_ret_1"].notna()
        assert np.allclose(expected[mask], g["fwd_ret_1"][mask], atol=1e-10)


def test_backtest_publishes_its_execution_convention():
    bt = result("backtest_h1.json")
    conv = bt["execution_convention"]
    assert conv["entry"] == "close of session t"
    assert conv["exit"] == "close of session t+1"
    assert "fwd_ret_1[t] = close[t+1] / close[t] - 1" in conv["realised_return"]
    assert "round trip" in conv["cost_per_position"]
    assert conv["excluded"], "the convention must name what it ignores"


def test_cost_in_the_artefact_equals_the_configured_cost(cfg):
    bt = result("backtest_h1.json")
    costs = CostModel.from_config(cfg)
    assert bt["costs"]["round_trip_bps"] == pytest.approx(costs.round_trip_bps)
    assert f"{costs.round_trip_bps:.1f} bps" in bt["execution_convention"]["cost_per_position"]


def test_delayed_execution_sensitivity_is_published():
    bt = result("backtest_h1.json")
    assert "ADAPTIVE_GATE_T1" in bt["strategies"], "the implementation-lag variant must be reported"
    gate = bt["strategies"]["ADAPTIVE_GATE"]
    lagged = bt["strategies"]["ADAPTIVE_GATE_T1"]
    assert gate["available"] and lagged["available"]
    assert gate["sessions_traded"] == lagged["sessions_traded"]


def test_honest_note_matches_the_convention():
    bt = result("backtest_h1.json")
    note = bt["honest_note"]
    assert "entry at the session-t close" in note
    assert "optimistic" in note
    assert "next session close" not in note, "the stale, contradictory wording must be gone"


def test_documentation_states_the_same_convention():
    text = (ROOT / "docs" / "SCIENTIFIC_METHOD.md").read_text(encoding='utf-8')
    assert "close of session t" in text
    assert re.search(r"round trip", text, re.I)
    assert "ADAPTIVE_GATE_T1" in text


def test_experiments_use_the_same_cost_convention(cfg):
    ex = result("experiments_h1.json")
    costs = CostModel.from_config(cfg)
    arm = next(e for e in ex["experiments"] if e["experiment_id"] == "A_PRICE_ONLY")
    fin = arm["financial_metrics"]
    implied = fin["gross_return_bps_per_trade"] - fin["net_return_bps_per_trade"]
    assert implied == pytest.approx(costs.round_trip_bps, abs=0.05)
