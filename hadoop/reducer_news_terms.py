#!/usr/bin/env python3
"""Hadoop Streaming reducer — inverted index over news headlines.

Input  (stdin) : sorted TSV  term<TAB>symbol
Output (stdout): TSV  term<TAB>document_frequency<TAB>symbol_count<TAB>symbols(comma separated)
"""
import sys


def emit(term, postings):
    if term is None or not postings:
        return
    symbols = sorted(postings)
    sys.stdout.write(f"{term}\t{sum(postings.values())}\t{len(symbols)}\t{','.join(symbols)}\n")


def main() -> None:
    cur, postings = None, {}
    for line in sys.stdin:
        try:
            term, symbol = line.rstrip("\n").split("\t", 1)
        except ValueError:
            continue
        if term != cur:
            emit(cur, postings)
            cur, postings = term, {}
        postings[symbol] = postings.get(symbol, 0) + 1
    emit(cur, postings)


if __name__ == "__main__":
    main()
