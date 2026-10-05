"""Timestamped headline ingestion + transparent lexicon sentiment.

Providers (all REAL, never generated):
  1. Google News RSS  — public RSS feed, gives headline + RFC-822 publish timestamp.
  2. Yahoo Finance news — short recent window.

Historical coverage of free news is intrinsically shallow. VIGIL therefore treats news as an
*optional modality*: dates without coverage are reported as NEWS UNAVAILABLE and forecast trust is
reduced accordingly (see vigil/decision/gate.py). No headline is ever synthesised.

Sentiment is a deterministic finance lexicon score (documented in docs/ml-methodology.md), not a
black-box claim: every score can be traced back to the matched terms.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger

log = get_logger("vigil.ingestion.news")

POSITIVE = {
    "beat": 2, "beats": 2, "surge": 2, "surges": 2, "jump": 2, "jumps": 2, "rally": 2, "rallies": 2,
    "gain": 1, "gains": 1, "rise": 1, "rises": 1, "upgrade": 2, "upgrades": 2, "outperform": 2,
    "profit": 1, "record": 1, "expansion": 1, "growth": 1, "wins": 1, "win": 1, "approval": 1,
    "bullish": 2, "strong": 1, "boost": 1, "dividend": 1, "buyback": 2, "высок": 0,
}
NEGATIVE = {
    "miss": -2, "misses": -2, "fall": -1, "falls": -1, "plunge": -2, "plunges": -2, "slump": -2,
    "drop": -1, "drops": -1, "downgrade": -2, "downgrades": -2, "underperform": -2, "loss": -2,
    "losses": -2, "probe": -2, "fraud": -3, "penalty": -2, "fine": -1, "weak": -1, "bearish": -2,
    "cut": -1, "cuts": -1, "lawsuit": -2, "resign": -1, "resigns": -1, "default": -3, "selloff": -2,
}
EVENT_CATEGORIES = {
    "EARNINGS": ("earnings", "results", "profit", "revenue", "quarter", "q1", "q2", "q3", "q4"),
    "RATING": ("upgrade", "downgrade", "target price", "rating", "analyst"),
    "REGULATORY": ("sebi", "rbi", "probe", "penalty", "regulator", "court", "lawsuit", "tax"),
    "CORPORATE_ACTION": ("dividend", "buyback", "split", "bonus", "merger", "acquisition", "stake"),
    "MACRO": ("inflation", "rate", "gdp", "crude", "rupee", "fed", "budget", "tariff"),
}


def score_headline(text: str) -> Dict[str, object]:
    """Transparent lexicon sentiment. Returns score in [-1, 1] plus the evidence terms used."""
    tokens = [t.strip(".,:;!?()[]\"'").lower() for t in text.split()]
    hits = []
    raw = 0
    for tok in tokens:
        if tok in POSITIVE:
            raw += POSITIVE[tok]
            hits.append((tok, POSITIVE[tok]))
        elif tok in NEGATIVE:
            raw += NEGATIVE[tok]
            hits.append((tok, NEGATIVE[tok]))
    score = max(-1.0, min(1.0, raw / 4.0))
    low = text.lower()
    category = next((cat for cat, keys in EVENT_CATEGORIES.items() if any(k in low for k in keys)), "GENERAL")
    label = "POSITIVE" if score > 0.15 else "NEGATIVE" if score < -0.15 else "NEUTRAL"
    return {"sentiment": round(score, 3), "sentiment_label": label, "matched_terms": hits,
            "event_category": category, "strength": round(min(1.0, abs(score) + 0.1 * len(hits)), 3)}


class NewsIngestor:
    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.cache: Path = self.cfg.raw_root / "news" / "headlines.json"
        self.cache.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- providers ----------------
    def _google_news(self, query: str, symbol: str) -> List[Dict]:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query) +
               "&hl=en-IN&gl=IN&ceid=IN:en")
        req = urllib.request.Request(url, headers={"User-Agent": "VIGIL-Research/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
        out = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            pub = item.findtext("pubDate")
            if not title or not pub:
                continue
            ts = pd.to_datetime(pub, utc=True, errors="coerce")
            if pd.isna(ts):
                continue
            doc = {
                "news_id": f"N-{symbol}-{int(ts.timestamp())}-{abs(hash(title)) % 10**6}",
                "symbol": symbol,
                "headline": title[:300],
                "published_at": ts.isoformat(),
                "provider": "GOOGLE_NEWS_RSS",
                "source_url_domain": (item.findtext("source") or "unknown"),
            }
            doc.update(score_headline(title))
            out.append(doc)
        return out

    def _yahoo(self, symbol: str) -> List[Dict]:
        try:
            import yfinance as yf

            items = yf.Ticker(symbol).news or []
        except Exception:
            return []
        out = []
        for it in items:
            content = it.get("content", it)
            title = content.get("title") or it.get("title")
            pub = content.get("pubDate") or it.get("providerPublishTime")
            if not title or not pub:
                continue
            ts = (datetime.fromtimestamp(pub, tz=timezone.utc) if isinstance(pub, (int, float))
                  else pd.to_datetime(pub, utc=True).to_pydatetime())
            doc = {
                "news_id": f"N-{symbol}-{int(ts.timestamp())}-y",
                "symbol": symbol,
                "headline": str(title)[:300],
                "published_at": ts.isoformat(),
                "provider": "YAHOO_FINANCE_NEWS",
                "source_url_domain": "finance.yahoo.com",
            }
            doc.update(score_headline(str(title)))
            out.append(doc)
        return out

    # ---------------- orchestration ----------------
    def fetch(self, allow_network: bool = True) -> List[Dict]:
        docs: List[Dict] = []
        if allow_network:
            for ins in self.cfg.instruments:
                query = f"{ins.name} NSE share"
                try:
                    docs.extend(self._google_news(query, ins.symbol))
                except Exception as exc:
                    log.warning("google news unavailable for %s (%s)", ins.symbol, type(exc).__name__)
                docs.extend(self._yahoo(ins.symbol))
        if docs:
            self.cache.write_text(json.dumps(docs, indent=2))
        elif self.cache.exists():
            docs = json.loads(self.cache.read_text())
            log.warning("news served from local cache (network unavailable)")
        seen, out = set(), []
        for d in sorted(docs, key=lambda d: d["published_at"]):
            key = (d["symbol"], d["headline"])
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        log.info("news ingested: %d real timestamped headlines, %d symbols",
                 len(out), len({d["symbol"] for d in out}))
        return out
