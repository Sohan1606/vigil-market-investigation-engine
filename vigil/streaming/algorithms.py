"""Data-stream algorithms from the Big Data Analytics syllabus — real implementations.

BloomFilter        : membership test with one-sided error, used for event de-duplication.
FlajoletMartin     : probabilistic distinct counting (PCSA with stochastic averaging).
DGIM               : O(log^2 N) counting of 1-bits in a sliding window of the last N events.
ReservoirSample    : uniform sample of an unbounded stream (used for drift monitoring).
ExponentialDecay   : decaying window counters per key (used for 'attention' scoring).

All of them are exercised by the live stream job and surfaced in the DATA OBSERVATORY with
*measured* error figures against exact counts.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ----------------------------------------------------------------------------- Bloom filter
class BloomFilter:
    def __init__(self, n_bits: int = 1 << 16, n_hashes: int = 4) -> None:
        if n_bits <= 0 or n_hashes <= 0:
            raise ValueError("bloom filter requires positive size and hash count")
        self.n_bits = int(n_bits)
        self.n_hashes = int(n_hashes)
        self.bits = bytearray((self.n_bits + 7) // 8)
        self.inserted = 0

    def _positions(self, item: str) -> Iterable[int]:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1
        for i in range(self.n_hashes):
            yield (h1 + i * h2) % self.n_bits

    def add(self, item: str) -> None:
        for pos in self._positions(item):
            self.bits[pos >> 3] |= 1 << (pos & 7)
        self.inserted += 1

    def __contains__(self, item: str) -> bool:
        return all(self.bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(item))

    def add_if_absent(self, item: str) -> bool:
        """Returns True if the item was (probably) new — the de-duplication primitive."""
        if item in self:
            return False
        self.add(item)
        return True

    @property
    def fill_ratio(self) -> float:
        set_bits = sum(bin(b).count("1") for b in self.bits)
        return set_bits / self.n_bits

    @property
    def expected_fp_rate(self) -> float:
        k, m, n = self.n_hashes, self.n_bits, max(self.inserted, 1)
        return (1 - math.exp(-k * n / m)) ** k

    def stats(self) -> Dict[str, Any]:
        return {"algorithm": "BloomFilter", "bits": self.n_bits, "hashes": self.n_hashes,
                "inserted": self.inserted, "fill_ratio": round(self.fill_ratio, 5),
                "expected_fp_rate": round(self.expected_fp_rate, 6)}


# ----------------------------------------------------------------------------- Flajolet–Martin
class FlajoletMartin:
    """PCSA / FM sketch with stochastic averaging over `n_registers` buckets."""

    PHI = 0.77351  # FM bias-correction constant

    def __init__(self, n_registers: int = 64) -> None:
        if n_registers & (n_registers - 1):
            raise ValueError("n_registers must be a power of two")
        self.n_registers = n_registers
        self.reg_bits = n_registers.bit_length() - 1
        self.registers = [0] * n_registers
        self.seen = 0

    @staticmethod
    def _rho(value: int) -> int:
        if value == 0:
            return 64
        return (value & -value).bit_length() - 1  # index of least-significant 1-bit

    def add(self, item: str) -> None:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "big")
        idx = h & (self.n_registers - 1)
        rest = h >> self.reg_bits
        self.registers[idx] |= 1 << self._rho(rest)
        self.seen += 1

    def estimate(self) -> float:
        total = 0.0
        for reg in self.registers:
            r = 0
            while reg & (1 << r):
                r += 1
            total += r
        avg_r = total / self.n_registers
        return (2 ** avg_r) / self.PHI * self.n_registers

    def stats(self, exact: Optional[int] = None) -> Dict[str, Any]:
        est = self.estimate()
        out = {"algorithm": "Flajolet-Martin", "registers": self.n_registers,
               "items_seen": self.seen, "estimated_distinct": round(est, 1)}
        if exact is not None:
            out["exact_distinct"] = int(exact)
            out["relative_error_pct"] = round(100.0 * abs(est - exact) / max(exact, 1), 2)
        return out


# ----------------------------------------------------------------------------- DGIM
@dataclass
class _Bucket:
    timestamp: int
    size: int


class DGIM:
    """Datar–Gionis–Indyk–Motwani sliding-window 1-bit counter (error <= 50%, typically far less)."""

    def __init__(self, window_size: int, bucket_capacity: int = 2) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        self.window_size = window_size
        self.bucket_capacity = bucket_capacity
        self.buckets: List[_Bucket] = []
        self.t = -1

    def add(self, bit: int) -> None:
        self.t += 1
        # expire buckets outside the window
        while self.buckets and self.buckets[0].timestamp <= self.t - self.window_size:
            self.buckets.pop(0)
        if not bit:
            return
        self.buckets.append(_Bucket(self.t, 1))
        # merge from the newest end while more than `capacity` buckets share a size
        i = len(self.buckets) - 1
        sizes: Dict[int, List[int]] = {}
        for idx, b in enumerate(self.buckets):
            sizes.setdefault(b.size, []).append(idx)
        for size in sorted(sizes):
            while True:
                idxs = [i for i, b in enumerate(self.buckets) if b.size == size]
                if len(idxs) <= self.bucket_capacity:
                    break
                a, b = idxs[0], idxs[1]          # merge the two oldest of this size
                merged = _Bucket(self.buckets[b].timestamp, self.buckets[a].size + self.buckets[b].size)
                self.buckets = [bk for k, bk in enumerate(self.buckets) if k not in (a, b)]
                self.buckets.insert(a, merged)
                self.buckets.sort(key=lambda bk: bk.timestamp)

    def count(self, k: Optional[int] = None) -> int:
        """Approximate number of 1-bits in the last k (default: window) events."""
        k = min(k or self.window_size, self.window_size)
        lower_bound_ts = self.t - k + 1
        total, last_size = 0, 0
        for b in self.buckets:
            if b.timestamp >= lower_bound_ts:
                total += b.size
                last_size = b.size
        if last_size:
            total -= last_size // 2          # DGIM halves the oldest (partially-covered) bucket
        return int(total)

    def stats(self, exact: Optional[int] = None) -> Dict[str, Any]:
        out = {"algorithm": "DGIM", "window": self.window_size, "buckets": len(self.buckets),
               "events_seen": self.t + 1, "estimated_ones": self.count()}
        if exact is not None:
            out["exact_ones"] = int(exact)
            out["relative_error_pct"] = round(100.0 * abs(out["estimated_ones"] - exact) / max(exact, 1), 2)
            out["memory_buckets_vs_window"] = f"{len(self.buckets)} buckets vs {self.window_size} bits"
        return out


# ----------------------------------------------------------------------------- Reservoir sample
class ReservoirSample:
    def __init__(self, k: int = 500, seed: int = 7) -> None:
        self.k = k
        self.items: List[Any] = []
        self.n = 0
        self._rng = random.Random(seed)

    def add(self, item: Any) -> None:
        self.n += 1
        if len(self.items) < self.k:
            self.items.append(item)
        else:
            j = self._rng.randint(0, self.n - 1)
            if j < self.k:
                self.items[j] = item

    def stats(self) -> Dict[str, Any]:
        return {"algorithm": "ReservoirSampling", "capacity": self.k, "stream_length": self.n,
                "sample_size": len(self.items)}


# ----------------------------------------------------------------------------- Decaying counters
@dataclass
class ExponentialDecayCounters:
    half_life: float = 50.0
    counters: Dict[str, float] = field(default_factory=dict)
    last_t: Dict[str, int] = field(default_factory=dict)
    t: int = 0

    def add(self, key: str, weight: float = 1.0) -> None:
        self.t += 1
        decay_rate = math.log(2) / self.half_life
        prev_t = self.last_t.get(key, self.t)
        decayed = self.counters.get(key, 0.0) * math.exp(-decay_rate * (self.t - prev_t))
        self.counters[key] = decayed + weight
        self.last_t[key] = self.t

    def top(self, n: int = 5) -> List[Tuple[str, float]]:
        return sorted(((k, round(v, 3)) for k, v in self.counters.items()), key=lambda kv: -kv[1])[:n]
