"""Non-repetition audit, enforced in code.

VIGIL's product rule is 'one concept = one canonical home'. These tests fail if a concept that
belongs to one space starts appearing in another, or if generic dashboard navigation creeps in.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWS = (ROOT / "apps" / "web" / "js" / "views.js").read_text(encoding='utf-8')
APP_JS = (ROOT / "apps" / "web" / "js" / "app.js").read_text(encoding='utf-8')

# the source is organised as one exported function per space
SECTIONS = {}
for m in re.finditer(r"/\* =+ ([A-Z ()]+) \*/", VIEWS):
    SECTIONS[m.group(1).strip()] = m.end()
ORDER = list(SECTIONS.items())


def body(name: str) -> str:
    keys = [k for k, _ in ORDER]
    i = keys.index(name)
    start = ORDER[i][1]
    end = ORDER[i + 1][1] if i + 1 < len(ORDER) else len(VIEWS)
    return VIEWS[start:end]


def test_every_space_exists_exactly_once():
    assert set(SECTIONS) >= {"PULSE", "INVESTIGATE", "FORECAST", "DECISION GATE", "REPLAY",
                             "RESEARCH LAB", "DATA OBSERVATORY"}


def test_technical_indicators_live_only_in_forecast():
    for space in ["PULSE", "INVESTIGATE", "DECISION GATE", "RESEARCH LAB", "DATA OBSERVATORY"]:
        assert "rsi_14" not in body(space), f"technical indicator duplicated into {space}"


def test_risk_uncertainty_drift_and_calibration_live_in_the_decision_gate_or_the_lab():
    for token in ["uncertainty.components", "var_95_pct", "drift?.health"]:
        assert token not in body("PULSE") and token not in body("INVESTIGATE")


def test_big_data_internals_live_only_in_the_observatory():
    for token in ["bloom", "flajolet_martin", "dgim", "mapreduce", "lineage.stages"]:
        for space in ["PULSE", "INVESTIGATE", "FORECAST", "DECISION GATE", "REPLAY"]:
            assert token not in body(space).lower(), f"{token} duplicated into {space}"


def test_experiments_and_backtests_live_only_in_the_research_lab():
    for token in ["equity_curve", "experiment_id", "sharpe"]:
        for space in ["PULSE", "INVESTIGATE", "FORECAST", "DECISION GATE"]:
            assert token not in body(space), f"{token} duplicated into {space}"


def test_navigation_uses_vigil_language_not_generic_dashboard_labels():
    banned = ["Dashboard", "Analytics", "Charts", "Settings", "Overview Dashboard", "Prediction Screen"]
    nav = APP_JS[APP_JS.index("const SPACES"):APP_JS.index("const main")]
    for word in banned:
        assert word not in nav, f"generic navigation label '{word}' present"
    for space in ["PULSE", "INVESTIGATE", "FORECAST", "DECISION GATE", "REPLAY",
                  "RESEARCH LAB", "DATA OBSERVATORY"]:
        assert space in nav


def test_every_space_declares_exactly_one_question():
    nav = APP_JS[APP_JS.index("const SPACES"):APP_JS.index("const main")]
    questions = re.findall(r"question: '([^']+)'", nav)
    assert len(questions) == len(set(questions)), "two spaces claim the same question"


def test_no_guaranteed_return_or_certainty_language_in_the_ui():
    banned = ["guaranteed", "sure shot", "will definitely", "risk-free", "profit guaranteed",
              "news caused", "caused the"]
    for path in (ROOT / "apps" / "web").rglob("*"):
        if path.suffix not in {".js", ".html"}:
            continue
        text = path.read_text(encoding='utf-8').lower()
        for phrase in banned:
            assert phrase not in text, f"{phrase!r} found in {path.name}"
