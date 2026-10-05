"""Label construction. Labels are the ONLY place where forward-looking shifts are allowed."""
from __future__ import annotations

from typing import Iterable, List

import numpy as np
import pandas as pd


def add_labels(df: pd.DataFrame, horizons: Iterable[int]) -> pd.DataFrame:
    """Forward return + direction label per horizon.

    `fwd_ret_h` at row t = close[t+h]/close[t] - 1  (known only at t+h).
    `label_dir_h` = 1 if fwd_ret_h > 0 else 0.
    `label_known_at_h` = the date on which this label becomes observable — used by the evaluator
    and by the Replay engine so a forecast is never scored before its outcome exists.
    """
    out = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("date").copy()
        for h in horizons:
            fwd = g["close"].shift(-h) / g["close"] - 1.0
            g[f"fwd_ret_{h}"] = fwd
            g[f"label_dir_{h}"] = np.where(fwd.isna(), np.nan, (fwd > 0).astype(float))
            g[f"label_known_at_{h}"] = g["date"].shift(-h)
        out.append(g)
    return pd.concat(out, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)


def label_columns(horizons: Iterable[int]) -> List[str]:
    cols: List[str] = []
    for h in horizons:
        cols += [f"fwd_ret_{h}", f"label_dir_{h}", f"label_known_at_{h}"]
    return cols
