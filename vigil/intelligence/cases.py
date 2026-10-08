"""Market Cases — the investigation objects of VIGIL.

A case is opened when the streaming anomaly detector finds a cluster of unusual activity in one
sector/session. Each case carries an EVIDENCE LEDGER (supporting + contradicting, timestamped,
with strength), an interpretation written in the KNOWN/SUPPORTED/UNCERTAIN/CONTRADICTED/UNKNOWN
vocabulary, and links to the most similar historical cases by market-state distance.

Lifecycle: TRIGGERED -> INVESTIGATING -> FORECASTED -> CHALLENGED -> DECIDED -> RESOLVED -> LEARNED
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from .regime import market_state_frame

log = get_logger("vigil.intelligence.cases")

LIFECYCLE = ["TRIGGERED", "INVESTIGATING", "FORECASTED", "CHALLENGED", "DECIDED", "RESOLVED", "LEARNED"]
SIM_FEATURES = ["bench_vol_20", "market_breadth_20", "dispersion_20", "bench_trend_20"]


def _evidence(symbol_rows: pd.DataFrame, sector_rows: pd.DataFrame, market_row: pd.Series,
              news: List[Dict]) -> Dict[str, List[Dict]]:
    """Builds the evidence ledger from observable facts only."""
    supporting, contradicting, unknown = [], [], []

    def add(bucket: List[Dict], channel: str, statement: str, value, strength: float, state: str):
        bucket.append({"channel": channel, "statement": statement,
                       "value": None if value is None else round(float(value), 4),
                       "strength": round(float(strength), 3), "information_state": state})

    row = symbol_rows.iloc[-1]
    if pd.notna(row.get("volume_z")):
        z = float(row["volume_z"])
        add(supporting if z > 0 else contradicting, "VOLUME",
            f"Volume {'expanded' if z > 0 else 'contracted'} {abs(z):.1f}σ vs its 20-session norm",
            z, min(abs(z) / 4.0, 1.0), "KNOWN")
    if pd.notna(row.get("ret_1")):
        r = float(row["ret_1"])
        add(supporting if r > 0 else contradicting, "PRICE",
            f"Session return {r*100:+.2f}%", r, min(abs(r) / 0.04, 1.0), "KNOWN")
    if not sector_rows.empty and pd.notna(sector_rows["ret_1"].mean()):
        sr = float(sector_rows["ret_1"].mean())
        breadth = float((sector_rows["ret_1"] > 0).mean())
        add(supporting if sr > 0 else contradicting, "SECTOR",
            f"Sector average move {sr*100:+.2f}% with {breadth*100:.0f}% of names advancing",
            sr, min(abs(sr) / 0.03, 1.0), "SUPPORTED")
    if market_row is not None and pd.notna(market_row.get("market_breadth")):
        mb = float(market_row["market_breadth"])
        add(supporting if mb >= 0.5 else contradicting, "BREADTH",
            f"Market breadth {mb*100:.0f}% of the universe advancing", mb,
            min(abs(mb - 0.5) * 2, 1.0), "KNOWN")
    if pd.notna(row.get("corr_bench_60")):
        c = float(row["corr_bench_60"])
        add(supporting if c > 0.4 else contradicting, "CORRELATION",
            f"60-session correlation with the benchmark is {c:.2f}", c, min(abs(c), 1.0),
            "SUPPORTED" if abs(c) > 0.3 else "UNCERTAIN")
    if news:
        sent = float(np.mean([n.get("sentiment", 0.0) for n in news]))
        add(supporting if sent >= 0 else contradicting, "NEWS",
            f"{len(news)} headline(s) in the 5 days before the cutoff, mean lexicon sentiment {sent:+.2f}",
            sent, min(abs(sent) + 0.2, 1.0), "SUPPORTED" if abs(sent) > 0.15 else "UNCERTAIN")
    else:
        unknown.append({"channel": "NEWS", "statement": "No headline coverage available at the cutoff",
                        "value": None, "strength": 0.0, "information_state": "UNKNOWN"})
    return {"supporting": supporting, "contradicting": contradicting, "unavailable": unknown}


def _interpretation(ledger: Dict[str, List[Dict]], sector: str) -> Dict[str, str]:
    s = sum(e["strength"] for e in ledger["supporting"])
    c = sum(e["strength"] for e in ledger["contradicting"])
    total = s + c
    conflict = round(min(s, c) / total, 3) if total else 0.0
    if conflict > 0.38:
        state, text = "CONTRADICTED", (
            f"Evidence channels disagree about {sector}. Directional read is not supported; "
            f"VIGIL treats this as an information conflict rather than a signal.")
    elif s > c:
        state, text = "SUPPORTED", (
            f"Multiple independent channels point the same way in {sector}. "
            f"Temporal association only — causality is not established.")
    elif c > s:
        state, text = "SUPPORTED", (
            f"The dominant evidence in {sector} is negative. Temporal association only.")
    else:
        state, text = "UNCERTAIN", f"Evidence in {sector} is balanced and does not resolve the question."
    return {"information_state": state, "text": text, "conflict_index": str(conflict)}


def generate_cases(cfg: Optional[VigilConfig] = None, max_cases: int = 60) -> List[Dict]:
    cfg = cfg or load_config()
    store = DocumentStore(cfg)
    lake = DataLake(cfg)
    events = store.find("events")
    if not events:
        log.warning("no anomaly events — run the stream job first")
        return []
    ev = pd.DataFrame(events)
    ev["date"] = pd.to_datetime(ev["ts"]).dt.normalize()
    feats = lake.read("features", "equity_features")
    state = market_state_frame(cfg).set_index("date")
    news_df = pd.DataFrame(store.find("news"))
    if not news_df.empty:
        news_df["published_at"] = pd.to_datetime(news_df["published_at"], utc=True,
                                                 format="mixed").dt.tz_localize(None)

    clusters = (ev.groupby(["date", "sector"])
                .agg(symbols=("symbol", lambda s: sorted(set(s))),
                     severity=("severity", "max"),
                     mean_severity=("severity", "mean"),
                     n_events=("symbol", "size"),
                     types=("event_type", lambda s: sorted(set(s))))
                .reset_index()
                .sort_values(["severity", "n_events"], ascending=False))
    clusters = clusters.head(max_cases).sort_values("date")

    state_matrix = state[SIM_FEATURES].dropna()
    norm = (state_matrix - state_matrix.mean()) / state_matrix.std()

    cases: List[Dict] = []
    for i, row in enumerate(clusters.itertuples(index=False), start=1):
        day, sector = row.date, row.sector
        syms = row.symbols
        sym_rows = feats[(feats["symbol"] == syms[0]) & (feats["date"] <= day)].tail(30)
        sector_rows = feats[(feats["sector"] == sector) & (feats["date"] == day)]
        if sym_rows.empty or day not in state.index:
            continue
        market_row = state.loc[day]
        cutoff = day + pd.Timedelta(hours=23, minutes=59)
        news_items = []
        if not news_df.empty:
            sub = news_df[(news_df["symbol"].isin(syms)) &
                          (news_df["published_at"] <= cutoff) &
                          (news_df["published_at"] >= cutoff - pd.Timedelta(days=5))]
            news_items = sub.sort_values("published_at").to_dict("records")[:5]
        ledger = _evidence(sym_rows, sector_rows, market_row, news_items)
        interp = _interpretation(ledger, sector)

        similar = []
        if day in norm.index:
            target = norm.loc[day]
            past = norm[norm.index < day - pd.Timedelta(days=20)]
            if len(past) > 50:
                dist = ((past - target) ** 2).sum(axis=1) ** 0.5
                for d, dd in dist.nsmallest(3).items():
                    similar.append({"date": str(pd.Timestamp(d).date()), "distance": round(float(dd), 3)})

        case = {
            "case_id": f"V-{day.strftime('%y%m')}-{i:03d}",
            "title": f"{sector.title()} activity became unusual",
            "status": "RESOLVED" if day < feats["date"].max() - pd.Timedelta(days=20) else "INVESTIGATING",
            "lifecycle": LIFECYCLE,
            "opened_at": str(day.date()),
            "sector": sector,
            "symbols": syms,
            "trigger": f"{row.n_events} anomalous observation(s) ({', '.join(row.types)}) "
                       f"with peak severity {row.severity:.1f}σ",
            "severity": float(row.severity),
            "evidence": ledger,
            "interpretation": interp,
            "similar_historical_cases": similar,
            "regime_at_open": str(market_row.get("regime", "UNKNOWN")) if "regime" in market_row else None,
            "news_available": bool(news_items),
            "causal_language_note": "Temporal association detected. Causal relationship not established.",
        }
        cases.append(case)

    store.drop("cases")
    store.insert_many("cases", cases)
    (cfg.reports_root / "results" / "cases.json").write_text(json.dumps(cases, indent=2, default=str), encoding="utf-8")
    log.info("cases generated: %d", len(cases))
    return cases
