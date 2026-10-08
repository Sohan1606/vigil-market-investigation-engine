"""Data sources. Real market data via Yahoo Finance; cached locally for deterministic DEMO runs.

Honesty rules enforced here:
  * Nothing is ever synthesised to look like real market data.
  * If the network is unavailable we re-use the on-disk cache and label provenance as CACHED.
  * If neither is available the ingest fails loudly instead of inventing prices.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger, timed

log = get_logger("vigil.ingestion.sources")

OHLCV_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "adj_close", "volume"]


class DataUnavailable(RuntimeError):
    """Raised when neither live nor cached data exist — VIGIL never fabricates a substitute."""


@dataclass
class Provenance:
    symbol: str
    source: str            # YAHOO_FINANCE | CACHE
    retrieved_at: str
    rows: int
    first_date: Optional[str]
    last_date: Optional[str]
    note: str = ""


class MarketDataSource:
    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.cache_dir = self.cfg.raw_root / "ohlcv"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.provenance: List[Provenance] = []

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol.replace('.', '_').replace('^', 'IDX_')}.csv"

    def fetch(self, symbol: str, start: str, end: Optional[str], allow_network: bool = True) -> pd.DataFrame:
        cache = self._cache_path(symbol)
        df: Optional[pd.DataFrame] = None
        source = "CACHE"
        note = ""
        if allow_network:
            try:
                import yfinance as yf  # imported lazily so offline runs do not pay the cost

                raw = yf.download(symbol, start=start, end=end, progress=False,
                                  auto_adjust=False, threads=False)
                if raw is not None and len(raw) > 0:
                    df = self._normalise(raw, symbol)
                    source = "YAHOO_FINANCE"
                    df.to_csv(cache, index=False)
            except Exception as exc:
                note = f"network fetch failed ({type(exc).__name__}); using cache"
                log.warning("%s: %s", symbol, note)
        if df is None:
            if not cache.exists():
                raise DataUnavailable(
                    f"No live or cached data for {symbol}. Run once with network access "
                    f"(`python scripts/run_pipeline.py --stage ingest`) to populate data/raw.")
            df = pd.read_csv(cache, parse_dates=["date"])
            note = note or "offline run served from local cache"
        self.provenance.append(Provenance(
            symbol=symbol, source=source, retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            rows=len(df), first_date=str(df["date"].min().date()) if len(df) else None,
            last_date=str(df["date"].max().date()) if len(df) else None, note=note))
        return df

    @staticmethod
    def _normalise(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df = raw.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df.reset_index().rename(columns={
            "Date": "date", "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Adj Close": "adj_close", "Volume": "volume"})
        if "adj_close" not in df.columns:
            df["adj_close"] = df["close"]
        df["symbol"] = symbol
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df = df[OHLCV_COLUMNS]
        return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

    def fetch_universe(self, allow_network: bool = True) -> pd.DataFrame:
        start = self.cfg.get("universe.start", "2017-01-01")
        end = self.cfg.get("universe.end")
        symbols = self.cfg.symbols + [self.cfg.benchmark] + list(self.cfg.sector_indices.values())
        frames = []
        with timed("ingest.fetch_universe", symbols=len(symbols)):
            for sym in dict.fromkeys(symbols):
                try:
                    frames.append(self.fetch(sym, start, end, allow_network=allow_network))
                except DataUnavailable as exc:
                    log.error(str(exc))
                time.sleep(0.05)
        if not frames:
            raise DataUnavailable("universe ingestion produced no data")
        return pd.concat(frames, ignore_index=True)

    def write_provenance(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(p) for p in self.provenance], indent=2), encoding="utf-8")
        return path


class NewsSource:
    """Timestamped headline ingestion.

    Yahoo Finance exposes only a short recent window of headlines, so coverage is sparse by design.
    VIGIL stores exactly what it receives (headline, publish timestamp, entity) and marks every
    other date as NEWS UNAVAILABLE instead of fabricating articles. Sparse coverage is itself a
    first-class product state (missing-modality handling).
    """

    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.cache = self.cfg.raw_root / "news" / "headlines.json"
        self.cache.parent.mkdir(parents=True, exist_ok=True)

    def fetch(self, allow_network: bool = True) -> List[Dict]:
        docs: List[Dict] = []
        if allow_network:
            try:
                import yfinance as yf

                for ins in self.cfg.instruments:
                    try:
                        items = yf.Ticker(ins.symbol).news or []
                    except Exception:
                        items = []
                    for it in items:
                        content = it.get("content", it)
                        title = content.get("title") or it.get("title")
                        pub = content.get("pubDate") or it.get("providerPublishTime")
                        if not title or not pub:
                            continue
                        if isinstance(pub, (int, float)):
                            ts = datetime.fromtimestamp(pub, tz=timezone.utc)
                        else:
                            ts = pd.to_datetime(pub, utc=True).to_pydatetime()
                        docs.append({
                            "news_id": f"N-{ins.slug}-{int(ts.timestamp())}",
                            "symbol": ins.symbol,
                            "headline": str(title)[:300],
                            "published_at": ts.isoformat(timespec="seconds"),
                            "provider": str((content.get("provider") or {}).get("displayName", "unknown"))
                            if isinstance(content.get("provider"), dict) else "unknown",
                            "source": "YAHOO_FINANCE_NEWS",
                        })
                if docs:
                    self.cache.write_text(json.dumps(docs, indent=2), encoding="utf-8")
            except Exception as exc:
                log.warning("news fetch unavailable (%s)", type(exc).__name__)
        if not docs and self.cache.exists():
            docs = json.loads(self.cache.read_text())
        # de-duplicate on news_id
        seen, out = set(), []
        for d in docs:
            if d["news_id"] in seen:
                continue
            seen.add(d["news_id"])
            out.append(d)
        log.info("news ingested: %d real headlines across %d symbols", len(out), len({d['symbol'] for d in out}))
        return out
