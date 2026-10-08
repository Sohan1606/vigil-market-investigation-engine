"""MapReduce jobs over the HDFS-style lake. Mappers are module-level (picklable) functions."""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake
from .framework import KV, MapReduceJob

log = get_logger("vigil.mapreduce.jobs")


# --------------------------------------------------------------------------------------
# JOB 1 — symbol/year OHLCV aggregate profile (classic numeric aggregation)
# --------------------------------------------------------------------------------------
def map_symbol_year(rec: Dict[str, Any]) -> Iterable[KV]:
    key = (str(rec.get("symbol")), int(rec.get("year", pd.Timestamp(rec["date"]).year)))
    yield key, (1, float(rec["close"]), float(rec["volume"]), float(rec["high"]), float(rec["low"]))


def combine_symbol_year(key: Any, values: List[Tuple]) -> Iterable[KV]:
    n = sum(v[0] for v in values)
    close_sum = sum(v[1] for v in values)
    vol_sum = sum(v[2] for v in values)
    hi = max(v[3] for v in values)
    lo = min(v[4] for v in values)
    yield key, (n, close_sum, vol_sum, hi, lo)


def reduce_symbol_year(key: Any, values: List[Tuple]) -> Iterable[KV]:
    n = sum(v[0] for v in values)
    close_sum = sum(v[1] for v in values)
    vol_sum = sum(v[2] for v in values)
    hi = max(v[3] for v in values)
    lo = min(v[4] for v in values)
    symbol, year = key
    yield f"{symbol}|{year}", {
        "symbol": symbol, "year": int(year), "sessions": int(n),
        "avg_close": round(close_sum / max(n, 1), 4),
        "total_volume": float(vol_sum),
        "year_high": round(hi, 4), "year_low": round(lo, 4),
        "range_pct": round(100.0 * (hi - lo) / max(lo, 1e-9), 2),
    }


# --------------------------------------------------------------------------------------
# JOB 2 — market breadth: advancers / decliners per trading session (cross-symbol reduction)
# --------------------------------------------------------------------------------------
def map_breadth(rec: Dict[str, Any]) -> Iterable[KV]:
    try:
        o, c = float(rec["open"]), float(rec["close"])
    except (TypeError, ValueError):
        return
    if o <= 0:
        return
    day = str(pd.Timestamp(rec["date"]).date())
    direction = 1 if c > o else (-1 if c < o else 0)
    yield day, (direction, (c - o) / o, float(rec["volume"]))


def reduce_breadth(key: Any, values: List[Tuple]) -> Iterable[KV]:
    adv = sum(1 for v in values if v[0] > 0)
    dec = sum(1 for v in values if v[0] < 0)
    unch = sum(1 for v in values if v[0] == 0)
    total = max(adv + dec + unch, 1)
    yield key, {
        "date": key, "advancers": adv, "decliners": dec, "unchanged": unch,
        "breadth_ratio": round(adv / total, 4),
        "net_breadth": round((adv - dec) / total, 4),
        "mean_session_return": round(sum(v[1] for v in values) / total, 6),
        "session_volume": float(sum(v[2] for v in values)),
    }


# --------------------------------------------------------------------------------------
# JOB 3 — inverted index: news term -> symbols/frequency (classic MapReduce text workload)
# --------------------------------------------------------------------------------------
STOPWORDS = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "is", "at", "as", "by",
             "with", "from", "share", "shares", "stock", "nse", "bse", "india", "its", "after",
             "this", "that", "be", "it", "news", "update", "live", "price"}


def map_news_terms(rec: Dict[str, Any]) -> Iterable[KV]:
    headline = str(rec.get("headline", ""))
    symbol = str(rec.get("symbol"))
    seen = set()
    for tok in headline.lower().replace("'", " ").split():
        tok = "".join(ch for ch in tok if ch.isalpha())
        if len(tok) < 4 or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        yield tok, symbol


def reduce_news_terms(key: Any, values: List[str]) -> Iterable[KV]:
    counts: Dict[str, int] = {}
    for sym in values:
        counts[sym] = counts.get(sym, 0) + 1
    if sum(counts.values()) < 3:
        return
    yield key, {"term": key, "documents": int(sum(counts.values())),
                "symbols": len(counts), "postings": json.dumps(counts)}


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------
def run_mapreduce_suite(cfg: VigilConfig | None = None) -> Dict[str, Any]:
    """Run the MapReduce suite on the engine selected by MAPREDUCE_ENGINE (local | hadoop).

    `hadoop` submits hadoop/*.py through Hadoop Streaming and fails loudly when no cluster is
    reachable; `local` (default) runs the in-repo engine. The engine that actually produced the
    numbers is recorded in the result so no surface can mislabel it.
    """
    from .hadoop_runner import mapreduce_status, resolve_engine, run_hadoop_suite

    cfg = cfg or load_config()
    status = mapreduce_status(cfg)
    if resolve_engine() == "hadoop":
        if not status["hadoop_available"]:
            raise RuntimeError(
                "MAPREDUCE_ENGINE=hadoop requested but no usable Hadoop client is present: "
                f"{status['hadoop_detail']}. VIGIL will not fabricate a Hadoop run — "
                "use MAPREDUCE_ENGINE=local (default) or provision a cluster (hadoop/README.md).")
        hadoop_out = run_hadoop_suite(cfg)
        hadoop_out["engine_status"] = status
        return hadoop_out

    lake = DataLake(cfg)
    if lake.is_hdfs:
        raise RuntimeError(
            "The local MapReduce engine reads POSIX Parquet blocks as input splits and cannot "
            "run against STORAGE_MODE=hdfs. Use MAPREDUCE_ENGINE=hadoop with the HDFS lake, "
            "or STORAGE_MODE=local with the local engine.")
    results: Dict[str, Any] = {"engine": status["engine"], "engine_status": status, "jobs": []}

    curated = lake.dataset_path("curated", "ohlcv")

    job1 = MapReduceJob("symbol_year_profile", "vigil.mapreduce.jobs:map_symbol_year",
                        reduce_symbol_year, combiner=combine_symbol_year,
                        columns=["date", "symbol", "close", "volume", "high", "low"])
    df1, s1 = job1.run(curated)
    lake.write(df1.drop(columns=["key"]), "analytics", "mr_symbol_year_profile",
               partition_cols=("symbol",))
    results["jobs"].append(s1.to_dict())

    job2 = MapReduceJob("market_breadth", "vigil.mapreduce.jobs:map_breadth", reduce_breadth,
                        columns=["date", "symbol", "open", "close", "volume"])
    df2, s2 = job2.run(curated)
    df2 = df2.drop(columns=["key"]).sort_values("date")
    df2["date"] = pd.to_datetime(df2["date"])
    lake.write(df2, "analytics", "mr_market_breadth", partition_cols=("year",))
    results["jobs"].append(s2.to_dict())

    # news term index — materialise docstore news as a lake dataset first
    from ..storage.docstore import DocumentStore

    news = DocumentStore(cfg).find("news")
    if news:
        ndf = pd.DataFrame(news)[["news_id", "symbol", "headline", "published_at"]]
        ndf["date"] = pd.to_datetime(ndf["published_at"], utc=True, format="mixed").dt.tz_localize(None).dt.normalize()
        lake.write(ndf, "curated", "news", partition_cols=("symbol",))
        job3 = MapReduceJob("news_inverted_index", "vigil.mapreduce.jobs:map_news_terms",
                            reduce_news_terms, columns=["headline", "symbol"])
        df3, s3 = job3.run(lake.dataset_path("curated", "news"))
        if len(df3):
            lake.write(df3.drop(columns=["key"]).assign(symbol="_GLOBAL_"), "analytics",
                       "mr_news_inverted_index", partition_cols=("symbol",))
        results["jobs"].append(s3.to_dict())
    else:
        log.warning("no news documents — inverted index job skipped (modality unavailable)")

    out = cfg.reports_root / "results" / "mapreduce_stats.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results
