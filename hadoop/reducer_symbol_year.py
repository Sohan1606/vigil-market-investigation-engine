#!/usr/bin/env python3
"""Hadoop Streaming reducer (also usable as a combiner) — symbol/year OHLCV profile.

Input  (stdin) : sorted TSV  symbol|year<TAB>n,close_sum,volume_sum,high,low
Output (stdout): TSV  symbol<TAB>year<TAB>sessions<TAB>avg_close<TAB>total_volume<TAB>
                      year_high<TAB>year_low<TAB>range_pct
"""
import sys


def emit(key, n, close_sum, vol_sum, hi, lo):
    if key is None or n == 0:
        return
    symbol, year = key.split("|", 1)
    rng = 100.0 * (hi - lo) / max(lo, 1e-9)
    sys.stdout.write(
        f"{symbol}\t{year}\t{n}\t{round(close_sum / n, 4)}\t{vol_sum}\t"
        f"{round(hi, 4)}\t{round(lo, 4)}\t{round(rng, 2)}\n")


def main() -> None:
    cur, n, close_sum, vol_sum = None, 0, 0.0, 0.0
    hi, lo = float("-inf"), float("inf")
    for line in sys.stdin:
        try:
            key, value = line.rstrip("\n").split("\t", 1)
            c, cs, vs, h, l = value.split(",")
            c, cs, vs, h, l = int(c), float(cs), float(vs), float(h), float(l)
        except ValueError:
            continue
        if key != cur:
            emit(cur, n, close_sum, vol_sum, hi, lo)
            cur, n, close_sum, vol_sum, hi, lo = key, 0, 0.0, 0.0, float("-inf"), float("inf")
        n += c
        close_sum += cs
        vol_sum += vs
        hi, lo = max(hi, h), min(lo, l)
    emit(cur, n, close_sum, vol_sum, hi, lo)


if __name__ == "__main__":
    main()
