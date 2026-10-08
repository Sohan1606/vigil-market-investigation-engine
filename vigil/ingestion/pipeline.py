"""PHASE 3 — batch ingestion + data-quality triage + landing in the partitioned lake."""
from __future__ import annotations

import json
from typing import Optional

import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger, timed
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from .quality import DataQualityEngine
from .news import NewsIngestor
from .sources import MarketDataSource

log = get_logger("vigil.ingestion.pipeline")


def run_ingestion(cfg: Optional[VigilConfig] = None, allow_network: bool = True) -> dict:
    cfg = cfg or load_config()
    lake, store = DataLake(cfg), DocumentStore(cfg)
    source = MarketDataSource(cfg)

    with timed("ingest.batch"):
        raw = source.fetch_universe(allow_network=allow_network)
    source.write_provenance(cfg.processed_root / "provenance_ohlcv.json")

    valid, quarantine, report = DataQualityEngine().run(raw)
    lake.write(raw, "raw", "ohlcv", partition_cols=("symbol", "year"))
    lake.write(valid, "curated", "ohlcv", partition_cols=("symbol", "year"))
    if len(quarantine):
        lake.write(quarantine.drop(columns=["_status"]).rename(columns={"_reason": "quarantine_reason"}),
                   "curated", "ohlcv_quarantine", partition_cols=("symbol",))

    qpath = cfg.reports_root / "results" / "data_quality.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    news = NewsIngestor(cfg).fetch(allow_network=allow_network)
    store.drop("news")
    if news:
        store.insert_many("news", news)
    coverage = _direct_session_coverage(valid, news)

    summary = {
        "rows_raw": int(len(raw)),
        "rows_curated": int(len(valid)),
        "rows_quarantined": int(len(quarantine)),
        "symbols": int(valid["symbol"].nunique()),
        "date_min": str(valid["date"].min().date()),
        "date_max": str(valid["date"].max().date()),
        "quality_score": report.score,
        "news_documents": len(news),
        # DIRECT SESSION COVERAGE: share of (symbol, session) pairs that have at least one
        # headline published on that same session. This is NOT the share of modelling rows that
        # carry a news feature — the model uses a trailing 5-day window, reported separately as
        # ROLLING 5-DAY CONTEXT ROWS in reports/results/dataset_meta.json.
        "direct_session_coverage_pct": coverage,
        "coverage_definition": (
            "direct_session_coverage_pct = (symbol, session) pairs with a headline published on "
            "that session / all (symbol, session) pairs. The separate rolling 5-day context "
            "figure lives in dataset_meta.json as rolling_5d_context_rows_pct."),
        "docstore_backend": store.status().backend,
    }
    (cfg.reports_root / "results" / "ingestion_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("ingestion summary: %s", summary)
    return summary


def refresh_coverage_metadata(cfg: Optional[VigilConfig] = None) -> dict:
    """Recompute the coverage fields of ingestion_summary.json from published artefacts.

    Reads the curated lake and the news collection already in the document store — no network
    call, no re-fetch, no change to any measured value. It exists so the renamed, unambiguous
    coverage labels can be published without re-ingesting data that would no longer be identical.
    """
    cfg = cfg or load_config()
    from ..storage.lake import DataLake

    path = cfg.reports_root / "results" / "ingestion_summary.json"
    summary = json.loads(path.read_text()) if path.exists() else {}
    prices = DataLake(cfg).read("curated", "ohlcv", columns=["date", "symbol"])
    news = DocumentStore(cfg).find("news")
    summary["news_documents"] = len(news)
    summary["direct_session_coverage_pct"] = _direct_session_coverage(prices, news)
    summary.pop("news_coverage_pct", None)
    summary["coverage_definition"] = (
        "direct_session_coverage_pct = (symbol, session) pairs with a headline published on "
        "that session / all (symbol, session) pairs. The separate rolling 5-day context "
        "figure lives in dataset_meta.json as rolling_5d_context_rows_pct.")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("coverage metadata refreshed: %s", {k: summary[k] for k in
                                                 ("news_documents", "direct_session_coverage_pct")})
    return summary


def _direct_session_coverage(prices: pd.DataFrame, news: list) -> float:
    """Share of (symbol, session) pairs with a headline published on that same session.

    Deliberately the strictest of the two coverage numbers VIGIL publishes. The looser one
    (rows whose trailing 5-day window contains any headline) is computed in features/dataset.py
    and must always be labelled ROLLING 5-DAY CONTEXT ROWS, never "news coverage".
    """
    if not news:
        return 0.0
    ndf = pd.DataFrame(news)
    ndf["day"] = pd.to_datetime(ndf["published_at"], utc=True, format="mixed").dt.tz_localize(None).dt.normalize()
    have = set(zip(ndf["symbol"], ndf["day"]))
    pairs = set(zip(prices["symbol"], pd.to_datetime(prices["date"])))
    if not pairs:
        return 0.0
    return round(100.0 * len(have & pairs) / len(pairs), 4)
