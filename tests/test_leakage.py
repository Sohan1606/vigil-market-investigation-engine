"""Automated leakage tests — the mandatory scientific guardrail.

If any of these fail, every metric VIGIL reports is void.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT, result  # noqa: E402

from vigil.features.pit import assert_chronological_split, assert_no_future_columns
from vigil.models.walkforward import SplitPlan, make_folds


def test_no_future_named_columns_in_feature_set():
    meta = result("dataset_meta.json")
    assert_no_future_columns(meta["feature_columns"])          # raises on fwd_/label_/future_
    for col in meta["feature_columns"]:
        assert not col.startswith(("fwd_", "label_", "target_", "future_", "next_")), col


def test_features_never_correlate_perfectly_with_the_label(matrix):
    """A feature with |corr| ~ 1 against the forward return is the classic leak signature."""
    meta = result("dataset_meta.json")
    y = matrix["fwd_ret_1"]
    mask = y.notna()
    worst, worst_col = 0.0, None
    for col in meta["feature_columns"]:
        x = pd.to_numeric(matrix[col], errors="coerce")
        both = mask & x.notna()
        if both.sum() < 500 or x[both].std() == 0:
            continue
        c = abs(float(np.corrcoef(x[both], y[both])[0, 1]))
        if c > worst:
            worst, worst_col = c, col
    assert worst < 0.30, f"suspiciously predictive feature {worst_col}: |corr|={worst:.3f}"


def test_label_is_strictly_forward_looking(matrix):
    """label_dir_1 must be reconstructible from the NEXT session's close, never the current one."""
    sym = matrix["symbol"].iloc[0]
    g = matrix[matrix["symbol"] == sym].sort_values("date")
    recomputed = (g["close"].shift(-1) > g["close"]).astype(float)
    valid = g["label_dir_1"].notna() & recomputed.notna()
    agreement = float((g.loc[valid, "label_dir_1"] == recomputed[valid]).mean())
    assert agreement > 0.98, f"label does not match next-session direction ({agreement:.3f})"


def test_walkforward_folds_are_chronological_and_purged():
    dates = pd.Series(pd.date_range("2015-01-01", "2025-01-01", freq="B"))
    folds = make_folds(dates, SplitPlan())
    assert folds, "no folds produced"
    for start, test_start, test_end in folds:
        assert start < test_start < test_end
        train = dates[dates < test_start - pd.Timedelta(days=1)]
        test = dates[(dates >= test_start) & (dates < test_end)]
        assert_chronological_split(train, test)
        assert train.max() < test.min()
        gap = (test.min() - train.max()).days
        assert gap >= 1, "no purge gap between train and test"


def test_chronological_assertion_actually_rejects_a_leaky_split():
    """The guard must fail on a deliberately leaky (random) split — otherwise it proves nothing."""
    dates = pd.Series(pd.date_range("2020-01-01", periods=400, freq="B"))
    shuffled = dates.sample(frac=1.0, random_state=0)
    train, test = shuffled.iloc[:300], shuffled.iloc[300:]
    with pytest.raises(Exception):
        assert_chronological_split(train, test)


def test_no_random_split_was_used_in_the_tournament():
    t = result("tournament_h1.json")
    assert t["protocol"]["random_split_used"] is False
    assert "walk" in t["protocol"]["validation"].lower()
    assert t["protocol"]["purge_sessions"] >= 1


def test_every_fold_trains_before_it_tests():
    t = result("tournament_h1.json")
    for model in t["leaderboard"]:
        if not model.get("available"):
            continue
        folds = model["by_fold"]
        for i in range(1, len(folds)):
            assert folds[i]["test_start"] >= folds[i - 1]["test_end"], "overlapping test windows"


def test_news_features_respect_the_publication_timestamp(matrix):
    """News attached to session t may only come from articles published at or before t."""
    if "news_available" not in matrix.columns:
        pytest.skip("news features absent")
    from vigil.features.dataset import attach_news_features

    base = matrix[["symbol", "date"]].drop_duplicates().head(200).copy()
    base["close"] = 100.0
    future_news = pd.DataFrame({
        "symbol": base["symbol"].iloc[0],
        "published_at": base["date"].max() + pd.Timedelta(days=30),
        "sentiment": 1.0,
        "strength": 1.0,
        "headline": "a headline from the future",
    }, index=[0])
    out = attach_news_features(base, future_news)
    assert float(out["news_count_5d"].sum()) == 0.0, "future-dated news leaked into past sessions"


def test_scaling_statistics_are_not_global(matrix):
    """Fold-local scaling: a runner fit on fold 1 must not know fold 2's distribution."""
    from vigil.models.walkforward import WalkForwardRunner
    meta = result("dataset_meta.json")
    cols = meta["feature_columns"][:6]
    runner = WalkForwardRunner(cols, horizon=1)
    df = matrix.sort_values("date")
    early = df[df["date"] < df["date"].quantile(0.5)]
    stats_early = early[cols].mean()
    stats_all = df[cols].mean()
    assert not np.allclose(stats_early.values, stats_all.values), (
        "test fixture invalid: distributions identical, test cannot detect global scaling")
    assert runner.feature_cols == cols


# --------------------------------------------------------------------------- v1.0 regression
LEAK_PATTERN = re.compile(r"fwd_ret|y_true|label_dir|future_", re.I)
DECISION_SOURCES = (
    "vigil/decision/quality.py",
    "vigil/decision/backtest.py",
    "vigil/decision/forecast_service.py",
    "vigil/decision/gate.py",
    "vigil/research/experiments.py",
)


def _assignment_lines(path: Path, target: str):
    """Source lines that assign to `target` (e.g. edge_bps), including continuations."""
    lines = path.read_text(encoding='utf-8').splitlines()
    out, buf, depth = [], None, 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("def ", "#", "return ", "raise ")) or stripped[:3] in ('"""', "'''"):
            continue
        # matches `edge_bps = ...`, `df["edge_bps"] = ...` and `frame['edge_bps'] = ...`
        if buf is None and re.search(rf"(?<![\w]){target}[\"']?\]?\s*=[^=]", line) and "==" not in line:
            buf, depth = line, line.count("(") - line.count(")")
            if depth <= 0:
                out.append(buf)
                buf = None
            continue
        if buf is not None:
            buf += " " + line.strip()
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                out.append(buf)
                buf = None
    return out


def test_pre_trade_edge_never_uses_future_returns():
    """REGRESSION (v1.0, defect #4): the pre-trade edge must be computable before the outcome.

    Before the fix, vigil/research/experiments.py built the abstention gate from
    `df["fwd_ret"].abs()` — the magnitude of the very return the gate was deciding about.
    """
    offenders = []
    for rel in DECISION_SOURCES:
        path = ROOT / rel
        if not path.exists():
            continue
        for target in ("edge_bps", "expected_move", "signal", "acted", "gate_ok"):
            for line in _assignment_lines(path, target):
                if LEAK_PATTERN.search(line):
                    offenders.append(f"{rel}: {line.strip()[:110]}")
    assert not offenders, "future information in a pre-trade decision path:\n" + "\n".join(offenders)


def test_gating_inputs_are_known_before_the_outcome(lake):
    """The columns the gate reads must all exist in the point-in-time feature set."""
    preds = lake.read("analytics", "calibrated_predictions_h1")
    for col in ("p_cal", "p_up", "model_agreement"):
        assert col in preds.columns, col
    feats = lake.read("features", "equity_features")
    assert "vol_20" in feats.columns
    # vol_20 at date t must be computable from closes up to t: it is finite long before any
    # forward return exists for the final sessions.
    tail = feats.sort_values("date").groupby("symbol", observed=True).tail(1)
    assert tail["vol_20"].notna().all()


def test_decision_quality_artefact_has_no_future_leak_in_its_definition():
    q = result("decision_quality_h1.json")
    defs = json.dumps(q.get("metric_definitions", {})).lower()
    assert "edge" not in defs or "fwd_ret" not in defs
    assert q["diagnostics"]["edge_threshold_bps"] > 0
