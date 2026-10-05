#!/usr/bin/env python3
"""Hadoop Streaming mapper — inverted index over news headlines.

Input  (stdin) : TSV  news_id<TAB>symbol<TAB>headline
Output (stdout): TSV  term<TAB>symbol
"""
import re
import sys

STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "is", "as", "at", "by", "with",
        "from", "its", "it", "be", "has", "have", "will", "after", "over", "that", "this", "are",
        "was", "new", "says", "say", "amid", "may", "but", "not", "up", "down"}
TOKEN = re.compile(r"[a-z][a-z0-9'&.-]+")


def main() -> None:
    for line in sys.stdin:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3 or parts[0] == "news_id":
            continue
        _news_id, symbol, headline = parts[0], parts[1], parts[2]
        seen = set()
        for tok in TOKEN.findall(headline.lower()):
            tok = tok.strip(".-'")
            if len(tok) < 3 or tok in STOP or tok in seen:
                continue
            seen.add(tok)
            sys.stdout.write(f"{tok}\t{symbol}\n")


if __name__ == "__main__":
    main()
