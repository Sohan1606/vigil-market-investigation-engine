#!/usr/bin/env python3
"""Hadoop Streaming mapper — market breadth per session.

Input  (stdin) : TSV  date<TAB>symbol<TAB>open<TAB>high<TAB>low<TAB>close<TAB>volume
Output (stdout): TSV  date<TAB>advancers,decliners,unchanged,volume
"""
import sys


def main() -> None:
    for line in sys.stdin:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 7 or parts[0] == "date":
            continue
        date, _symbol, open_, _h, _l, close, volume = parts[:7]
        try:
            o, c, v = float(open_), float(close), float(volume)
        except ValueError:
            continue
        if o <= 0:
            continue
        ret = c / o - 1.0
        adv = 1 if ret > 0.0005 else 0
        dec = 1 if ret < -0.0005 else 0
        unch = 1 if adv == 0 and dec == 0 else 0
        sys.stdout.write(f"{date[:10]}\t{adv},{dec},{unch},{v}\n")


if __name__ == "__main__":
    main()
