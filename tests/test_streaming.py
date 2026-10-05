"""Stream-algorithm correctness: the sketches must be right, and their error must be measured."""
from __future__ import annotations

import math
import random

from vigil.streaming.algorithms import (BloomFilter, DGIM, ExponentialDecayCounters,
                                        FlajoletMartin, ReservoirSample)


def test_bloom_filter_has_no_false_negatives_and_a_small_false_positive_rate():
    bf = BloomFilter(n_bits=1 << 20, n_hashes=7)
    inserted = [f"evt-{i}" for i in range(30_000)]
    for key in inserted:
        bf.add(key)
    assert all(k in bf for k in inserted), "false negative — impossible for a Bloom filter"
    probes = [f"absent-{i}" for i in range(20_000)]
    fp = sum(k in bf for k in probes) / len(probes)
    assert fp < 0.01, f"measured false-positive rate {fp:.5f} too high for the configured size"
    assert bf.expected_fp_rate < 0.01


def test_bloom_dedup_primitive_accepts_each_key_once():
    bf = BloomFilter(n_bits=1 << 18, n_hashes=5)
    keys = [f"k-{i % 1000}" for i in range(5000)]
    accepted = sum(bf.add_if_absent(k) for k in keys)
    assert accepted <= 1000, "de-duplication let a repeat through"
    assert accepted >= 990, "de-duplication rejected too many genuinely new keys"


def test_flajolet_martin_estimates_large_cardinality_within_tolerance():
    fm = FlajoletMartin(n_registers=1024)
    exact = 40_000
    for i in range(exact):
        fm.add(f"key-{i}")
    est = fm.estimate()
    err = abs(est - exact) / exact
    assert err < 0.35, f"FM relative error {err:.3f} outside the documented tolerance"
    # the sketch must use dramatically less memory than exact counting
    assert fm.n_registers * 8 < exact


def test_dgim_counts_ones_in_the_window_within_its_guarantee():
    random.seed(3)
    window = 2048
    dgim = DGIM(window_size=window)
    bits = [1 if random.random() < 0.35 else 0 for _ in range(10_000)]
    for b in bits:
        dgim.add(b)
    exact = sum(bits[-window:])
    est = dgim.count()
    assert abs(est - exact) / max(exact, 1) < 0.5, "DGIM outside its 50% worst-case bound"
    assert len(dgim.buckets) < 4 * math.log2(window), "bucket count is not O(log^2 N)-small"


def test_reservoir_sample_is_bounded_and_spread_over_the_stream():
    rs = ReservoirSample(k=500, seed=11)
    for i in range(50_000):
        rs.add(i)
    assert rs.n == 50_000 and len(rs.items) == 500
    assert min(rs.items) < 15_000 and max(rs.items) > 35_000, "sample concentrated — not uniform"


def test_exponential_decay_counters_forget_the_distant_past():
    c = ExponentialDecayCounters(half_life=50.0)
    for _ in range(100):
        c.add("a", 1.0)
    early = c.counters["a"]
    for _ in range(500):
        c.add("b", 1.0)
    c.add("a", 0.0)
    assert c.counters["a"] < early, "decayed counter did not decay"
    assert c.top(1)[0][0] == "b"
