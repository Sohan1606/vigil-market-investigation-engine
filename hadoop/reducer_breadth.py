#!/usr/bin/env python3
"""Hadoop Streaming reducer — market breadth per session.

Input  (stdin) : sorted TSV  date<TAB>advancers,decliners,unchanged,volume
Output (stdout): TSV  date<TAB>advancers<TAB>decliners<TAB>unchanged<TAB>
                      breadth_pct<TAB>total_volume
"""
import sys


def emit(date, adv, dec, unch, vol):
    if date is None:
        return
    total = adv + dec + unch
    breadth = 100.0 * adv / total if total else 0.0
    sys.stdout.write(f"{date}\t{adv}\t{dec}\t{unch}\t{round(breadth, 2)}\t{vol}\n")


def main() -> None:
    cur, adv, dec, unch, vol = None, 0, 0, 0, 0.0
    for line in sys.stdin:
        try:
            key, value = line.rstrip("\n").split("\t", 1)
            a, d, u, v = value.split(",")
            a, d, u, v = int(a), int(d), int(u), float(v)
        except ValueError:
            continue
        if key != cur:
            emit(cur, adv, dec, unch, vol)
            cur, adv, dec, unch, vol = key, 0, 0, 0, 0.0
        adv, dec, unch, vol = adv + a, dec + d, unch + u, vol + v
    emit(cur, adv, dec, unch, vol)


if __name__ == "__main__":
    main()
