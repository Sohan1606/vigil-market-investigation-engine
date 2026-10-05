"""Assembles the modelling matrix: engineered features + timestamp-aware news features + labels.

News features are joined with a strict `published_at <= session_close(t)` rule, and a
`news_available` flag records the modality state for every row (used by the Decision Gate and by
Research Lab experiment C).
"""
from __future__ import annotations

import json

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger, timed
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from ..spark_jobs.features_job import FEATURE_COLUMNS
from .labels import add_labels
from .pit import assert_no_future_columns

log = get_logger("vigil.features.dataset")

NEWS_FEATURES = ["news_count_5d", "news_sentiment_5d", "news_sentiment_strength", "news_available"]
PRICE_FEATURES = [c for c in FEATURE_COLUMNS]


def _news_frame(cfg: VigilConfig) -> pd.DataFrame:
    docs = DocumentStore(cfg).find("news")
    if not docs:
        return pd.DataFrame(columns=["symbol", "published_at", "sentiment", "strength"])
    df = pd.DataFrame(docs)
    df["published_at"] = pd.to_datetime(df["published_at"], utc=True, format="mixed").dt.tz_localize(None)
    for col, default in (("sentiment", 0.0), ("strength", 0.0)):
        if col not in df.columns:
            df[col] = default
    return df[["symbol", "published_at", "sentiment", "strength", "headline"]].sort_values("published_at")


def attach_news_features(features: pd.DataFrame, news: pd.DataFrame, window_days: int = 5) -> pd.DataFrame:
    """Timestamp-aware rolling news aggregation.

    Only headlines published at or before the session close enter the window, so the feature is
    computable in real time. "Timestamp-aware" is a statement about information availability, not
    about causation: a headline inside the window is temporally associated with the session, never
    established as its cause.
    """
    out = features.copy()
    for col in NEWS_FEATURES:
        out[col] = 0.0
    if news.empty:
        return out
    cutoffs = out["date"] + pd.Timedelta(hours=23, minutes=59)
    out["_cutoff"] = cutoffs
    pieces = []
    for sym, g in out.groupby("symbol", sort=False):
        nsub = news[news["symbol"] == sym]
        g = g.sort_values("_cutoff").copy()
        if nsub.empty:
            pieces.append(g)
            continue
        pub = nsub["published_at"].values.astype("datetime64[ns]")
        sent = nsub["sentiment"].to_numpy(dtype=float)
        strg = nsub["strength"].to_numpy(dtype=float)
        hi = np.searchsorted(pub, g["_cutoff"].values.astype("datetime64[ns]"), side="right")
        lo = np.searchsorted(pub, (g["_cutoff"] - pd.Timedelta(days=window_days)).values.astype("datetime64[ns]"),
                             side="left")
        csum_s = np.concatenate([[0.0], np.cumsum(sent)])
        csum_t = np.concatenate([[0.0], np.cumsum(strg)])
        count = hi - lo
        g["news_count_5d"] = count
        with np.errstate(invalid="ignore", divide="ignore"):
            g["news_sentiment_5d"] = np.where(count > 0, (csum_s[hi] - csum_s[lo]) / np.maximum(count, 1), 0.0)
            g["news_sentiment_strength"] = np.where(count > 0, (csum_t[hi] - csum_t[lo]) / np.maximum(count, 1), 0.0)
        g["news_available"] = (count > 0).astype(float)
        pieces.append(g)
    out = pd.concat(pieces, ignore_index=True).drop(columns=["_cutoff"])
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_dataset(cfg: Optional[VigilConfig] = None, persist: bool = True) -> Tuple[pd.DataFrame, Dict]:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    with timed("dataset.build"):
        feats = lake.read("features", "equity_features")
        news = _news_frame(cfg)
        feats = attach_news_features(feats, news)
        horizons = list(cfg.get("features.horizons", [1, 3, 5, 20]))
        data = add_labels(feats, horizons)

    feature_cols = [c for c in PRICE_FEATURES + NEWS_FEATURES if c in data.columns]
    assert_no_future_columns(feature_cols)
    meta = {
        "rows": int(len(data)),
        "feature_columns": feature_cols,
        "n_features": len(feature_cols),
        "horizons": horizons,
        # ROLLING 5-DAY CONTEXT ROWS: modelling rows whose trailing 5-session window contains at
        # least one headline. Strictly larger than the DIRECT SESSION COVERAGE reported in
        # reports/results/ingestion_summary.json — the two are never interchangeable.
        "news_window_days": 5,
        "rolling_5d_context_rows": int(data["news_available"].sum()),
        "rolling_5d_context_rows_pct": round(100.0 * float(data["news_available"].mean()), 3),
        "coverage_definitions": {
            "rolling_5d_context_rows_pct": "modelling rows with >=1 headline in the trailing "
                                           "5-session window (this file)",
            "direct_session_coverage_pct": "(symbol, session) pairs with a headline published on "
                                           "that same session (reports/results/ingestion_summary.json)",
        },
        "feature_version": cfg.feature_version,
        "date_min": str(data["date"].min().date()),
        "date_max": str(data["date"].max().date()),
    }
    if persist:
        lake.write(data, "features", "model_matrix", partition_cols=("symbol", "year"))
        (cfg.reports_root / "results" / "dataset_meta.json").write_text(
            pd.Series(meta).to_json(indent=2))
    log.info("model matrix: rows=%d features=%d rolling_5d_context_rows=%.2f%%",
             meta["rows"], meta["n_features"], meta["rolling_5d_context_rows_pct"])
    return data, meta


def dataset_meta_from_matrix(cfg: Optional[VigilConfig] = None, persist: bool = True) -> Dict:
    """Recompute dataset_meta.json from the PUBLISHED model matrix, without rebuilding it.

    Used when only the reporting metadata needs refreshing (for example after the news-coverage
    labels were separated into DIRECT SESSION COVERAGE and ROLLING 5-DAY CONTEXT ROWS). It never
    changes a feature value, so the published models stay consistent with the matrix they were
    trained on.
    """
    cfg = cfg or load_config()
    data = DataLake(cfg).read("features", "model_matrix")
    feature_cols = [c for c in PRICE_FEATURES + NEWS_FEATURES if c in data.columns]
    meta = {
        "rows": int(len(data)),
        "feature_columns": feature_cols,
        "n_features": len(feature_cols),
        "horizons": list(cfg.get("features.horizons", [1, 3, 5, 20])),
        "news_window_days": 5,
        "rolling_5d_context_rows": int(data["news_available"].sum()),
        "rolling_5d_context_rows_pct": round(100.0 * float(data["news_available"].mean()), 3),
        "coverage_definitions": {
            "rolling_5d_context_rows_pct": "modelling rows with >=1 headline in the trailing "
                                           "5-session window (this file)",
            "direct_session_coverage_pct": "(symbol, session) pairs with a headline published on "
                                           "that same session (reports/results/ingestion_summary.json)",
        },
        "feature_version": cfg.feature_version,
        "date_min": str(data["date"].min().date()),
        "date_max": str(data["date"].max().date()),
        "source": "recomputed from lake://features/model_matrix (no rebuild, no retraining)",
    }
    if persist:
        (cfg.reports_root / "results" / "dataset_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def feature_groups(columns: List[str]) -> Dict[str, List[str]]:
    """Feature sets used by the Research Lab experiments (A..E)."""
    price = [c for c in columns if c in PRICE_FEATURES and not c.startswith(("bench_", "sector_", "market_", "excess_", "beta_", "corr_"))]
    context = [c for c in columns if c.startswith(("bench_", "sector_", "market_", "excess_", "beta_", "corr_"))]
    news = [c for c in columns if c in NEWS_FEATURES]
    return {"price": price, "context": context, "news": news}
