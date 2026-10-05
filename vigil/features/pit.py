"""Point-in-time correctness utilities and leakage detectors.

Two independent guards:
  1. `assert_no_future_columns` — static guard: no label/forward column may enter a model matrix.
  2. `truncation_invariance` — empirical guard: recompute features on data truncated at T and check
     that every feature value at t <= T is bit-identical to the full-history computation. If any
     feature peeked into the future, the values would differ. This is the test that actually
     catches look-ahead bias, and it is executed in tests/test_temporal_integrity.py.
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd

FORBIDDEN_PREFIXES = ("fwd_", "label_", "future_", "target_", "outcome_")


class LeakageError(AssertionError):
    pass


def assert_no_future_columns(columns: Iterable[str]) -> None:
    bad = [c for c in columns if c.startswith(FORBIDDEN_PREFIXES)]
    if bad:
        raise LeakageError(f"future-information columns present in model matrix: {bad}")


def truncation_invariance(builder: Callable[[pd.DataFrame], pd.DataFrame],
                          raw: pd.DataFrame,
                          cutoff: pd.Timestamp,
                          feature_cols: Sequence[str],
                          tolerance: float = 1e-9) -> Tuple[bool, List[str]]:
    """Returns (is_point_in_time, offending_columns)."""
    full = builder(raw)
    truncated = builder(raw[raw["date"] <= cutoff].copy())
    merged = full.merge(truncated, on=["symbol", "date"], suffixes=("_full", "_trunc"))
    merged = merged[merged["date"] <= cutoff]
    offenders: List[str] = []
    for col in feature_cols:
        a, b = merged.get(f"{col}_full"), merged.get(f"{col}_trunc")
        if a is None or b is None:
            continue
        mask = a.notna() | b.notna()
        diff = (a.fillna(0) - b.fillna(0)).abs()
        scale = a.abs().fillna(0).clip(lower=1.0)
        if (diff[mask] / scale[mask] > tolerance).any():
            offenders.append(col)
    return (len(offenders) == 0), offenders


def assert_chronological_split(train_dates: pd.Series, test_dates: pd.Series) -> None:
    if len(train_dates) == 0 or len(test_dates) == 0:
        raise LeakageError("empty split")
    if pd.Timestamp(train_dates.max()) >= pd.Timestamp(test_dates.min()):
        raise LeakageError(
            f"non-chronological split: train ends {train_dates.max()} but test starts {test_dates.min()}")


def information_cutoff(as_of: pd.Timestamp) -> pd.Timestamp:
    """Information cutoff for a forecast made after session close on `as_of`."""
    return pd.Timestamp(as_of).normalize() + pd.Timedelta(hours=23, minutes=59)


def news_available_at(news: pd.DataFrame, symbol: str, cutoff: pd.Timestamp,
                      lookback_days: int = 5) -> pd.DataFrame:
    """Strictly point-in-time news slice: only headlines published at or before the cutoff."""
    if news.empty:
        return news
    sub = news[(news["symbol"] == symbol) &
               (news["published_at"] <= cutoff) &
               (news["published_at"] >= cutoff - pd.Timedelta(days=lookback_days))]
    return sub.sort_values("published_at")
