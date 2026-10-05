#!/usr/bin/env python3
"""Hadoop Streaming mapper — symbol/year OHLCV profile.

Input  (stdin) : TSV  date<TAB>symbol<TAB>open<TAB>high<TAB>low<TAB>close<TAB>volume
Output (stdout): TSV  symbol|year<TAB>n,close_sum,volume_sum,high,low

Pure stdin/stdout with no VIGIL imports, because the file is shipped to the cluster with
`-files` and executed by the Hadoop Streaming task JVMs.
"""
import sys


def main() -> None:
    for line in sys.stdin:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 7 or parts[0] == "date":
            continue
        date, symbol, _o, high, low, close, volume = parts[:7]
        try:
            hi, lo, cl, vol = float(high), float(low), float(close), float(volume)
        except ValueError:
            continue
        year = date[:4]
        if not year.isdigit():
            continue
        sys.stdout.write(f"{symbol}|{year}\t1,{cl},{vol},{hi},{lo}\n")


if __name__ == "__main__":
    main()
