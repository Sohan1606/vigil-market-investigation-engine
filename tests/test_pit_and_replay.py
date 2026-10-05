"""Point-in-time correctness of the serving path and of REPLAY."""
from __future__ import annotations

import pandas as pd
import pytest

from conftest import result  # noqa: E402


def test_information_cutoff_is_before_the_forecast_target(cfg):
    from vigil.features.dataset import build_dataset  # noqa: F401
    from vigil.decision.forecast_service import ForecastService
    svc = ForecastService(cfg)
    docs = svc.generate_current(horizon=1)
    assert docs, "no forecasts produced"
    for d in docs[:5]:
        # the cutoff is the close of the as-of session: same day, strictly before the target
        assert pd.Timestamp(d["information_cutoff"]).normalize() <= pd.Timestamp(d["as_of"]).normalize()
        assert d["horizon"] >= 1
        assert d["verdict"] in {"POSITIVE BIAS", "NEGATIVE BIAS", "NO ACTION"}


def test_replay_hides_everything_after_the_anchor(cfg):
    from vigil.research.replay import replay
    r = replay("RELIANCE.NS", "2024-06-14", horizon=5, cfg=cfg)
    lg = r["leak_guard"]
    assert lg["sessions_used_after_anchor"] == 0
    assert lg["sessions_after_anchor_in_dataset"] > 0, "fixture date must have a future in the dataset"
    assert lg["news_hidden_because_later_than_cutoff"] >= 0
    for item in r["visible_news"]:
        assert pd.Timestamp(item["published_at"]) <= pd.Timestamp(r["information_cutoff"])


def test_replay_outcome_is_measured_not_assumed(cfg):
    from vigil.research.replay import replay
    r = replay("RELIANCE.NS", "2024-06-14", horizon=5, cfg=cfg)
    o = r["outcome"]
    if not o["resolved"]:
        pytest.skip("anchor too recent to have resolved")
    assert o["realised_direction"] in {"UP", "DOWN"}
    assert isinstance(o["forecast_correct"], bool)
    assert o["decision_quality"]


def test_forecast_contract_carries_full_lineage(cfg):
    from vigil.decision.forecast_service import ForecastService
    d = ForecastService(cfg).generate_current(horizon=1)[0]
    for field in ("forecast_id", "model_version", "feature_version", "dataset_version",
                  "code_fingerprint", "evaluation_rule", "information_cutoff"):
        assert d.get(field), f"missing contract field {field}"
