"""PHASE 7 — streaming analytics over the market event tape.

Producer : converts curated sessions into an ordered event stream (one event per symbol-session),
           written to Kafka if available, otherwise to the deterministic replay log.
Consumer : single pass with Bloom de-duplication, Flajolet-Martin distinct counting, DGIM sliding
           window counts of 'unusual volume' bits, exponential-decay attention scores, and online
           anomaly detection (Welford z-scores — strictly point-in-time, no lookahead).

Everything reported here is measured during the pass: throughput, per-event latency percentiles,
approximate-vs-exact error of each sketch.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from .algorithms import BloomFilter, DGIM, ExponentialDecayCounters, FlajoletMartin, ReservoirSample
from .bus import EventBus, LocalReplayBus, get_bus

log = get_logger("vigil.streaming.job")


class _Welford:
    """Online mean/variance — the only statistics a streaming anomaly detector is allowed to use."""

    __slots__ = ("n", "mean", "m2")

    def __init__(self) -> None:
        self.n, self.mean, self.m2 = 0, 0.0, 0.0

    def z(self, x: float) -> Optional[float]:
        if self.n < 30 or self.m2 <= 0:
            return None
        std = (self.m2 / (self.n - 1)) ** 0.5
        return (x - self.mean) / std if std > 0 else None

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)


def build_event_tape(cfg: Optional[VigilConfig] = None, bus: Optional[EventBus] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    df = lake.read("curated", "ohlcv")
    df = df[df["symbol"].isin(cfg.symbols)].sort_values(["date", "symbol"])
    bus = bus or get_bus(cfg)
    if isinstance(bus, LocalReplayBus):
        bus.truncate()
    events: List[Dict[str, Any]] = []
    prev_close: Dict[str, float] = {}
    for row in df.itertuples(index=False):
        pc = prev_close.get(row.symbol)
        ret = (row.close / pc - 1.0) if pc else 0.0
        prev_close[row.symbol] = row.close
        events.append({
            "event_id": f"E-{row.symbol}-{pd.Timestamp(row.date).date()}",
            "ts": pd.Timestamp(row.date).isoformat(),
            "symbol": row.symbol,
            "sector": cfg.sector_of(row.symbol),
            "close": float(row.close), "volume": float(row.volume),
            "ret": float(ret), "range_pct": float((row.high - row.low) / row.close),
            "source": "CURATED_SESSION_TAPE",
        })
    # 2% duplicate injection is a *test fixture* for the Bloom filter, flagged as such
    duplicates = events[::50]
    for d in duplicates:
        events.append({**d, "replayed": True})
    events.sort(key=lambda e: (e["ts"], e["symbol"], bool(e.get("replayed"))))
    produced = bus.produce(events)
    log.info("event tape produced: %d events (%d intentional duplicates for dedup testing)",
             produced, len(duplicates))
    return {"events_produced": produced, "duplicates_injected": len(duplicates),
            "bus_mode": bus.status().mode}


@dataclass
class StreamResult:
    events_processed: int = 0
    duplicates_filtered: int = 0
    throughput_eps: float = 0.0
    latency_p50_us: float = 0.0
    latency_p95_us: float = 0.0
    latency_p99_us: float = 0.0
    bloom: Dict[str, Any] = field(default_factory=dict)
    flajolet_martin: Dict[str, Any] = field(default_factory=dict)
    dgim: Dict[str, Any] = field(default_factory=dict)
    reservoir: Dict[str, Any] = field(default_factory=dict)
    attention: List[Tuple[str, float]] = field(default_factory=list)
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    bus_mode: str = "LOCAL_REPLAY"
    window_end: Optional[str] = None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["anomalies"] = self.anomalies[-200:]
        return d


def run_stream(cfg: Optional[VigilConfig] = None, bus: Optional[EventBus] = None,
               persist: bool = True) -> StreamResult:
    cfg = cfg or load_config()
    bus = bus or get_bus(cfg)
    bloom = BloomFilter(int(cfg.get("streaming.bloom_bits", 65536)),
                        int(cfg.get("streaming.bloom_hashes", 4)))
    fm = FlajoletMartin(256)
    dgim = DGIM(int(cfg.get("streaming.dgim_window", 2048)))
    reservoir = ReservoirSample(500, seed=cfg.seed)
    attention = ExponentialDecayCounters(half_life=120.0)
    vol_stats: Dict[str, _Welford] = {}
    ret_stats: Dict[str, _Welford] = {}

    exact_ids: set[str] = set()
    exact_symbols: set[str] = set()
    unusual_bits: List[int] = []
    latencies: List[float] = []
    res = StreamResult(bus_mode=bus.status().mode)
    anomalies: List[Dict[str, Any]] = []

    t_start = time.perf_counter()
    for ev in bus.consume():
        t0 = time.perf_counter()
        eid = ev["event_id"]
        if not bloom.add_if_absent(eid):
            res.duplicates_filtered += 1
            latencies.append((time.perf_counter() - t0) * 1e6)
            continue
        exact_ids.add(eid)
        sym = ev["symbol"]
        exact_symbols.add(sym)
        # distinct-count problem posed on event keys (symbol x session), the cardinality that
        # actually matters for a market tape; symbol cardinality is small enough to count exactly.
        fm.add(eid)

        vw = vol_stats.setdefault(sym, _Welford())
        rw = ret_stats.setdefault(sym, _Welford())
        log_vol = float(np.log1p(ev["volume"]))
        zvol, zret = vw.z(log_vol), rw.z(ev["ret"])
        vw.update(log_vol)
        rw.update(ev["ret"])

        unusual = 1 if (zvol is not None and zvol > 2.0) else 0
        dgim.add(unusual)
        unusual_bits.append(unusual)
        reservoir.add({"symbol": sym, "ts": ev["ts"], "ret": ev["ret"]})

        severity = max(abs(zvol or 0.0), abs(zret or 0.0))
        if severity >= 3.0:
            attention.add(sym, weight=min(severity, 8.0))
            kind = ("VOLUME_ANOMALY" if (zvol or 0) >= 3.0 else
                    "PRICE_ANOMALY" if abs(zret or 0) >= 3.0 else "MARKET_ANOMALY")
            anomalies.append({
                "event_id": f"A-{sym}-{ev['ts'][:10]}",
                "symbol": sym, "sector": ev.get("sector", "UNKNOWN"), "ts": ev["ts"],
                "event_type": kind,
                "z_volume": round(zvol, 3) if zvol is not None else None,
                "z_return": round(zret, 3) if zret is not None else None,
                "severity": round(float(severity), 3),
                "ret": round(ev["ret"], 5),
                "detector": "streaming_welford_zscore",
            })
        res.events_processed += 1
        latencies.append((time.perf_counter() - t0) * 1e6)
        res.window_end = ev["ts"]

    elapsed = max(time.perf_counter() - t_start, 1e-9)
    lat = np.array(latencies) if latencies else np.array([0.0])
    res.throughput_eps = round((res.events_processed + res.duplicates_filtered) / elapsed, 1)
    res.latency_p50_us = round(float(np.percentile(lat, 50)), 2)
    res.latency_p95_us = round(float(np.percentile(lat, 95)), 2)
    res.latency_p99_us = round(float(np.percentile(lat, 99)), 2)
    res.bloom = bloom.stats()
    res.bloom["exact_unique_ids"] = len(exact_ids)
    res.flajolet_martin = fm.stats(exact=len(exact_ids))
    res.flajolet_martin["exact_distinct_symbols"] = len(exact_symbols)
    window = dgim.window_size
    res.dgim = dgim.stats(exact=int(sum(unusual_bits[-window:])))
    res.reservoir = reservoir.stats()
    res.attention = attention.top(8)
    res.anomalies = anomalies

    if persist:
        store = DocumentStore(cfg)
        store.drop("events")
        if anomalies:
            store.insert_many("events", anomalies)
        out = cfg.reports_root / "results" / "streaming_stats.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res.to_dict(), indent=2, default=str))
    log.info("stream pass: processed=%d dedup=%d throughput=%.0f ev/s anomalies=%d",
             res.events_processed, res.duplicates_filtered, res.throughput_eps, len(anomalies))
    return res
