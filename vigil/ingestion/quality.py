"""Data quality contracts and the VALID / SUSPICIOUS / INVALID triage engine."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..logging_utils import get_logger

log = get_logger("vigil.ingestion.quality")

VALID, SUSPICIOUS, INVALID = "VALID", "SUSPICIOUS", "INVALID"


@dataclass
class DataContract:
    """Declarative contract checked on every ingest. Breaches are counted, never silently dropped."""

    required_columns: Tuple[str, ...] = ("date", "symbol", "open", "high", "low", "close", "volume")
    non_negative: Tuple[str, ...] = ("open", "high", "low", "close", "volume")
    max_abs_daily_return: float = 0.35      # > 35% single-session move => quarantine for review
    max_gap_days: int = 12                  # calendar gap tolerance (holidays/suspension)
    stale_tolerance_days: int = 10
    min_rows_per_symbol: int = 250


@dataclass
class QualityReport:
    rows_in: int = 0
    rows_valid: int = 0
    rows_quarantined: int = 0
    rows_rejected: int = 0
    checks: Dict[str, int] = field(default_factory=dict)
    per_symbol: Dict[str, Dict[str, float]] = field(default_factory=dict)
    contract_violations: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Data quality score (0-100) actually derived from the triage counts."""
        if self.rows_in == 0:
            return 0.0
        return round(100.0 * (self.rows_valid + 0.5 * self.rows_quarantined) / self.rows_in, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = self.score
        return d


class DataQualityEngine:
    def __init__(self, contract: Optional[DataContract] = None) -> None:
        self.contract = contract or DataContract()

    def run(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, QualityReport]:
        """Returns (valid_rows, quarantined_rows, report). Invalid rows are rejected."""
        c = self.contract
        rep = QualityReport(rows_in=len(df))
        missing_cols = [col for col in c.required_columns if col not in df.columns]
        if missing_cols:
            rep.contract_violations.append(f"schema: missing columns {missing_cols}")
            raise ValueError(f"data contract violation — missing columns: {missing_cols}")

        work = df.copy()
        work["date"] = pd.to_datetime(work["date"])
        work["_status"] = VALID
        work["_reason"] = ""

        def mark(mask: pd.Series, status: str, reason: str) -> None:
            mask = mask.fillna(False)
            n = int(mask.sum())
            if n:
                rep.checks[reason] = rep.checks.get(reason, 0) + n
                # INVALID dominates SUSPICIOUS
                upgradable = mask & (work["_status"] != INVALID)
                work.loc[upgradable, "_status"] = status
                work.loc[upgradable, "_reason"] = (work.loc[upgradable, "_reason"] + f"|{reason}").str.strip("|")

        # --- hard integrity (INVALID → reject) ---
        mark(work[list(c.non_negative)].isna().any(axis=1), INVALID, "missing_values")
        mark((work[list(c.non_negative)] < 0).any(axis=1), INVALID, "negative_values")
        mark(work["close"] <= 0, INVALID, "non_positive_price")
        mark(work["high"] < work["low"], INVALID, "high_below_low")
        mark((work["high"] < work[["open", "close"]].max(axis=1) - 1e-6) |
             (work["low"] > work[["open", "close"]].min(axis=1) + 1e-6), INVALID, "impossible_ohlc")
        mark(work.duplicated(subset=["symbol", "date"], keep="first"), INVALID, "duplicate_rows")
        mark(work["date"] > pd.Timestamp.utcnow().tz_localize(None).normalize(), INVALID, "future_timestamp")
        mark(work["date"].dt.dayofweek >= 5, SUSPICIOUS, "weekend_timestamp")

        # --- statistical / calendar (SUSPICIOUS → quarantine) ---
        work = work.sort_values(["symbol", "date"])
        ret = work.groupby("symbol")["close"].pct_change()
        mark(ret.abs() > c.max_abs_daily_return, SUSPICIOUS, "extreme_return")
        gap = work.groupby("symbol")["date"].diff().dt.days
        mark(gap > c.max_gap_days, SUSPICIOUS, "calendar_gap")
        mark((work["volume"] == 0), SUSPICIOUS, "zero_volume")
        flat = work.groupby("symbol")["close"].diff().abs()
        flat_run = (flat == 0).groupby(work["symbol"]).rolling(5).sum().reset_index(level=0, drop=True)
        mark(flat_run >= 5, SUSPICIOUS, "stale_price_run")

        # robust outlier detection on returns (median absolute deviation)
        med = ret.groupby(work["symbol"]).transform("median")
        mad = (ret - med).abs().groupby(work["symbol"]).transform("median")
        z = (ret - med).abs() / (1.4826 * mad.replace(0, np.nan))
        mark(z > 12, SUSPICIOUS, "return_outlier_mad")

        valid = work[work["_status"] == VALID].copy()
        quarantine = work[work["_status"] == SUSPICIOUS].copy()
        rejected = work[work["_status"] == INVALID]
        rep.rows_valid, rep.rows_quarantined, rep.rows_rejected = len(valid), len(quarantine), len(rejected)

        last_date = work["date"].max()
        for sym, grp in work.groupby("symbol"):
            sym_valid = grp[grp["_status"] != INVALID]
            staleness = int((last_date - grp["date"].max()).days)
            rep.per_symbol[str(sym)] = {
                "rows": int(len(grp)),
                "valid": int((grp["_status"] == VALID).sum()),
                "quarantined": int((grp["_status"] == SUSPICIOUS).sum()),
                "rejected": int((grp["_status"] == INVALID).sum()),
                "staleness_days": staleness,
                "coverage_score": round(100.0 * len(sym_valid) / max(len(grp), 1), 2),
            }
            if len(sym_valid) < c.min_rows_per_symbol:
                rep.contract_violations.append(f"{sym}: only {len(sym_valid)} usable rows "
                                               f"(< {c.min_rows_per_symbol})")
            if staleness > c.stale_tolerance_days:
                rep.contract_violations.append(f"{sym}: stale by {staleness} days")

        log.info("data quality: in=%d valid=%d quarantine=%d reject=%d score=%.2f",
                 rep.rows_in, rep.rows_valid, rep.rows_quarantined, rep.rows_rejected, rep.score)
        drop_cols = ["_status", "_reason"]
        return (valid.drop(columns=drop_cols).reset_index(drop=True),
                quarantine.reset_index(drop=True), rep)
