"""Recursive summary-merge tests (v1.0 defect #7)."""
from __future__ import annotations

import json

from vigil.mlops.summary import deep_merge, merge_summary_file, sort_keys


def test_nested_dicts_merge_instead_of_replacing():
    base = {"models": {"horizon_1": "random_forest"}, "ingest": {"rows": 10}}
    incoming = {"models": {"horizon_5": "logistic"}}
    out = deep_merge(base, incoming)
    assert out["models"] == {"horizon_1": "random_forest", "horizon_5": "logistic"}
    assert out["ingest"] == {"rows": 10}
    assert base["models"] == {"horizon_1": "random_forest"}, "inputs must not be mutated"


def test_merge_is_recursive_at_depth():
    base = {"a": {"b": {"c": {"d": 1}}}}
    out = deep_merge(base, {"a": {"b": {"c": {"e": 2}}, "f": 3}})
    assert out["a"]["b"]["c"] == {"d": 1, "e": 2}
    assert out["a"]["f"] == 3


def test_scalar_overwrite_is_intentional_and_logged():
    log = []
    out = deep_merge({"models": {"horizon_1": "rf"}}, {"models": {"horizon_1": "xgb"}}, _log=log)
    assert out["models"]["horizon_1"] == "xgb"
    assert log == ["models.horizon_1"]


def test_stages_accumulate_across_invocations(tmp_path):
    path = tmp_path / "pipeline_summary.json"
    merge_summary_file(path, {"ingest": {"rows": 100}}, {"stages": ["ingest"]})
    merge_summary_file(path, {"models": {"horizon_1": "random_forest"}}, {"stages": ["models"]})
    merge_summary_file(path, {"models": {"horizon_5": "logistic"}}, {"stages": ["models"]})
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload["stages"]["ingest"]["rows"] == 100
    assert payload["stages"]["models"] == {"horizon_1": "random_forest", "horizon_5": "logistic"}


def test_repeated_execution_is_idempotent_and_deterministic(tmp_path):
    path = tmp_path / "s.json"
    summary = {"models": {"horizon_1": "rf"}, "backtest": {"horizon_1": {"MODEL": -16.2}}}
    merge_summary_file(path, summary, {})
    first = path.read_text(encoding='utf-8')
    merge_summary_file(path, summary, {})
    assert path.read_text(encoding='utf-8') == first, "re-running the same stage must not change the summary"


def test_output_key_order_is_sorted():
    out = sort_keys({"b": 1, "a": {"z": 1, "y": 2}})
    assert list(out) == ["a", "b"]
    assert list(out["a"]) == ["y", "z"]


def test_corrupt_summary_file_is_replaced_not_crashed(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    payload, _ = merge_summary_file(path, {"ingest": {"rows": 1}}, {})
    assert payload["stages"]["ingest"]["rows"] == 1
