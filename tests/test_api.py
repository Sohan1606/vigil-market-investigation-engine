"""API contract tests via FastAPI's TestClient (no network, no running server required)."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vigil.api.app import app  # noqa: E402

client = TestClient(app)


def _ok(path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
    return r.json()


def test_health_reports_components_and_mode():
    d = _ok("/api/health")
    assert d["vigil_health"] in {"HEALTHY", "WATCH", "DEGRADED"}
    assert d["components"] and all({"component", "state", "detail"} <= set(c) for c in d["components"])
    assert "REPLAY" in d["data_mode"].upper() or "LIVE" in d["data_mode"].upper()


def test_pulse_has_market_state_and_field():
    d = _ok("/api/pulse")
    assert d["market_state"]
    assert 0 <= d["market_pressure"] <= 100
    assert d["field"]["nodes"]


def test_universe_and_forecast_contract():
    uni = _ok("/api/universe")
    sym = uni["instruments"][0]["symbol"]
    f = _ok(f"/api/forecast/{sym}?horizon=1")
    assert f["verdict"] in {"POSITIVE BIAS", "NEGATIVE BIAS", "NO ACTION"}
    assert 0 <= f["probability_up"] <= 1
    assert f["information_cutoff"][:10] <= f["as_of"][:10]
    assert f["evidence"]["items"]


def test_decision_endpoint_exposes_every_gate_check():
    uni = _ok("/api/universe")
    sym = uni["instruments"][0]["symbol"]
    d = _ok(f"/api/decision/{sym}")
    names = {c["name"] for c in d["gate"]["checks"]}
    assert len(names) >= 5
    assert all({"passed", "value", "threshold", "explanation"} <= set(c) for c in d["gate"]["checks"])
    assert d["uncertainty"]["label"] in {"LOW", "MEDIUM", "HIGH"}


def test_cases_and_case_detail_carry_an_evidence_ledger():
    cases = _ok("/api/cases")
    assert cases["count"] >= 1
    cid = cases["cases"][0]["case_id"]
    c = _ok(f"/api/cases/{cid}")
    assert {"supporting", "contradicting", "unavailable"} <= set(c["evidence"])
    assert c["causal_language_note"]


def test_research_endpoints_are_served_from_measured_artefacts():
    o = _ok("/api/research/overview")
    assert o["tournament"]["leaderboard"]
    assert o["experiments"]["experiments"]
    assert _ok("/api/research/cemetery")["tombstones"] is not None


def test_observatory_describes_the_full_architecture():
    d = _ok("/api/observatory")
    assert len(d["architecture"]) >= 8
    assert d["streaming"]["bloom"]["bits"] > 0
    assert d["lake"]


def test_search_finds_an_instrument():
    d = _ok("/api/search?q=reliance")
    assert d["results"], "global search returned nothing for a known symbol"
    assert all({"kind", "label", "route"} <= set(r) for r in d["results"])


def test_assistant_refuses_to_answer_outside_its_evidence():
    r = client.post("/api/assistant", json={"question": "what will the nifty close at next friday"})
    assert r.status_code == 200
    body = r.json()
    answer = body["answer"].lower()
    assert answer
    # it must decline and say what it can answer from, instead of inventing a level
    assert "only from" in answer or "cannot" in answer or "do not" in answer
    assert not re.search(r"\b(nifty|close|target)\b[^.]*\b\d{4,}\b", answer), "invented a price level"


def test_unknown_symbol_returns_an_explained_error_not_a_stack_trace():
    r = client.get("/api/forecast/NOT_A_SYMBOL.NS")
    assert r.status_code in (404, 422, 500)
    body = r.json()
    text = str(body).lower()
    assert "not" in text or "unknown" in text
    assert "traceback" not in text


def test_exports_produce_real_files():
    r = client.get("/api/export/experiments.json")
    assert r.status_code == 200 and r.json()
    r = client.get("/api/export/data-quality.csv")
    assert r.status_code == 200 and "," in r.text
