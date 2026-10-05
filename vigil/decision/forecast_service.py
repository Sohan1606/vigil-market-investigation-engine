"""The forecast runtime: FORECAST CONTRACT → EVIDENCE → DECISION GATE → VERDICT → SNAPSHOT.

A forecast is produced by models trained ONLY on information available at the information cutoff.
The same code path serves "today" and any historical `as_of` date, which is what makes REPLAY
honest rather than theatrical.
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..features.dataset import NEWS_FEATURES
from ..features.pit import information_cutoff
from ..logging_utils import get_logger, timed
from ..models.tournament import default_feature_columns
from ..models.zoo import build_model, model_specs
from ..reliability.uncertainty import conformal_return_interval, forecast_stability, probability_uncertainty
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake
from .costs import CostModel
from .gate import evaluate_gate
from .risk import position_risk

log = get_logger("vigil.decision.forecast")

LIVE_MODELS = ("logistic_regression", "random_forest", "xgboost")   # fast, retrainable per as_of


@dataclass
class ForecastContract:
    forecast_id: str
    symbol: str
    name: str
    sector: str
    as_of: str
    information_cutoff: str
    horizon: int
    target: str
    evaluation_rule: str
    model_version: str
    feature_version: str
    dataset_version: str
    code_fingerprint: str
    status: str = "OPEN"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class ForecastService:
    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.lake = DataLake(self.cfg)
        self.store = DocumentStore(self.cfg)
        self.costs = CostModel.from_config(self.cfg)
        self._matrix: Optional[pd.DataFrame] = None
        self._regimes: Optional[pd.DataFrame] = None
        self._fitted: Dict[Tuple[str, int], Dict] = {}

    # ------------------------------------------------------------------ data
    @property
    def matrix(self) -> pd.DataFrame:
        if self._matrix is None:
            self._matrix = self.lake.read("features", "model_matrix")
        return self._matrix

    @property
    def regimes(self) -> pd.DataFrame:
        if self._regimes is None:
            self._regimes = (self.lake.read("analytics", "market_regime")
                             if self.lake.exists("analytics", "market_regime") else pd.DataFrame())
        return self._regimes

    def available_dates(self) -> List[pd.Timestamp]:
        return sorted(self.matrix["date"].unique())

    # ------------------------------------------------------------------ training
    def fit_as_of(self, as_of: pd.Timestamp, horizon: int = 1) -> Dict:
        """Train the live ensemble on data strictly older than the cutoff (purged by the horizon)."""
        key = (str(pd.Timestamp(as_of).date()), horizon)
        if key in self._fitted:
            return self._fitted[key]
        label = f"label_dir_{horizon}"
        cols = default_feature_columns(self.matrix)
        train = self.matrix[(self.matrix["date"] <= pd.Timestamp(as_of) - pd.Timedelta(days=horizon + 1))]
        train = train.dropna(subset=[label])
        if len(train) < 500:
            raise ValueError(f"insufficient training history before {as_of}")
        X = train[cols].to_numpy(dtype=float)
        median = np.nan_to_num(np.nanmedian(X, axis=0))
        Xf = np.where(np.isnan(X), median, X)
        mean, std = Xf.mean(axis=0), np.where(Xf.std(axis=0) == 0, 1.0, Xf.std(axis=0))
        Xs = np.clip((Xf - mean) / std, -8, 8)
        y = train[label].to_numpy(dtype=float)

        members, timings = {}, {}
        for spec in model_specs(self.cfg.seed):
            if spec.model_id not in LIVE_MODELS:
                continue
            t0 = time.perf_counter()
            try:
                model = build_model(spec, n_features=Xs.shape[1], seed=self.cfg.seed)
                model.fit(Xs, y)
                members[spec.model_id] = model
                timings[spec.model_id] = round((time.perf_counter() - t0) * 1000, 1)
            except Exception as exc:
                log.warning("live model %s unavailable: %s", spec.model_id, exc)
        if not members:
            raise RuntimeError("no live model could be trained")

        # weights from the walk-forward leaderboard (Brier), restricted to healthy models
        weights = self._leaderboard_weights(list(members))
        bundle = {"models": members, "cols": cols, "median": median, "mean": mean, "std": std,
                  "weights": weights, "train_rows": int(len(train)),
                  "train_end": str(train["date"].max().date()), "fit_ms": timings}
        self._fitted[key] = bundle
        return bundle

    def _leaderboard_weights(self, model_ids: List[str]) -> Dict[str, float]:
        docs = {d["model_id"]: d for d in self.store.find("models")}
        drift = self._drift_report()
        raw = {}
        for mid in model_ids:
            brier = float(docs.get(mid, {}).get("metrics", {}).get("brier", 0.25) or 0.25)
            health = drift.get("models", {}).get(mid, {}).get("health", {}).get("state", "HEALTHY")
            if health == "UNRELIABLE":
                continue                                  # never weight an unreliable model
            penalty = {"HEALTHY": 1.0, "WATCH": 0.85, "DEGRADED": 0.4}.get(health, 1.0)
            raw[mid] = float(np.exp(-12 * brier) * penalty)
        if not raw:
            raw = {mid: 1.0 for mid in model_ids}
        total = sum(raw.values())
        return {k: round(v / total, 4) for k, v in raw.items()}

    def _drift_report(self) -> Dict:
        path = self.cfg.reports_root / "results" / "drift_h1.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    # ------------------------------------------------------------------ forecasting
    def forecast(self, symbol: str, as_of: Optional[pd.Timestamp] = None, horizon: int = 1,
                 persist: bool = False) -> Dict:
        mat = self.matrix[self.matrix["symbol"] == symbol].sort_values("date")
        if mat.empty:
            raise ValueError(f"unknown symbol {symbol}")
        as_of = pd.Timestamp(as_of) if as_of is not None else mat["date"].max()
        hist = mat[mat["date"] <= as_of]
        if hist.empty:
            raise ValueError(f"no data for {symbol} at or before {as_of.date()}")
        row = hist.iloc[-1]
        bundle = self.fit_as_of(as_of, horizon)
        cols, median, mean, std = bundle["cols"], bundle["median"], bundle["mean"], bundle["std"]
        x = row[cols].to_numpy(dtype=float).reshape(1, -1)
        xf = np.where(np.isnan(x), median, x)
        xs = np.clip((xf - mean) / std, -8, 8)

        member_probs: Dict[str, Optional[float]] = {}
        infer_ms: Dict[str, float] = {}
        for mid, model in bundle["models"].items():
            t0 = time.perf_counter()
            try:
                member_probs[mid] = float(model.predict_proba(xs)[0, 1])
            except Exception as exc:
                log.warning("inference failed for %s: %s", mid, exc)
                member_probs[mid] = None
            infer_ms[mid] = round((time.perf_counter() - t0) * 1000, 2)

        usable = {k: v for k, v in member_probs.items() if v is not None}
        if not usable:
            raise RuntimeError("all live models failed at inference")
        w = {k: bundle["weights"].get(k, 1 / len(usable)) for k in usable}
        wsum = sum(w.values()) or 1.0
        p_raw = sum(usable[k] * w[k] for k in usable) / wsum
        dirs = [1 if v >= 0.5 else 0 for v in usable.values()]
        agreement = max(np.mean(dirs), 1 - np.mean(dirs))
        unc = probability_uncertainty(member_probs, float(agreement))
        shrink = 1.0 - 0.6 * min(float(np.std(list(usable.values()))) / 0.12, 1.0) * (1 - agreement)
        p_up = float(np.clip(0.5 + (p_raw - 0.5) * shrink, 0.01, 0.99))

        # ---------- context ----------
        sigma_daily = float(row.get("vol_20", np.nan)) / np.sqrt(252) if pd.notna(row.get("vol_20")) else np.nan
        past = hist["ret_1"].dropna().to_numpy()
        sig_hist = (hist["vol_20"] / np.sqrt(252)).to_numpy()
        band = conformal_return_interval(past[-750:], sigma_daily * np.sqrt(horizon),
                                         sig_hist[-750:], alpha=0.2)
        risk = position_risk(hist.tail(140), symbol,
                             float(self.cfg.get("risk.max_position_vol", 0.045)))
        regime_row = (self.regimes[self.regimes["date"] <= as_of].iloc[-1]
                      if not self.regimes.empty and (self.regimes["date"] <= as_of).any() else None)
        regime = str(regime_row["regime"]) if regime_row is not None else "UNKNOWN"
        regime_conf = float(regime_row["regime_confidence"]) if regime_row is not None else 0.0
        regime_probs = ({k.replace("p_", "").upper(): round(float(regime_row[k]), 4)
                         for k in regime_row.index if str(k).startswith("p_")}
                        if regime_row is not None else {})

        # trailing forecast trajectory (recomputed from the stored OOS series — no re-fit needed)
        traj = self._trajectory(symbol, as_of, horizon)
        stability = forecast_stability(traj)
        quality = self._data_quality_for(symbol)
        modality = self._modality_state(row)
        evidence = self._evidence(row, p_up, regime)

        expected_move = float(sigma_daily * np.sqrt(horizon)) if np.isfinite(sigma_daily) else 0.012
        ece = float(self._calibration_ece())
        drift_state = self._drift_report().get("models", {}).get("adaptive_ensemble", {}) \
            .get("health", {}).get("state", "HEALTHY")
        gate = evaluate_gate(
            probability=p_up, expected_move_pct=expected_move, data_quality=quality["score"],
            model_agreement=float(agreement), uncertainty=unc["uncertainty"], calibration_ece=ece,
            drift_state=drift_state, regime=regime, regime_confidence=regime_conf,
            risk_score=risk.risk_score, stability_score=float(stability.get("stability_score", 0.5)),
            modality_coverage=modality["coverage"], cfg=self.cfg, cost_model=self.costs)

        cutoff = information_cutoff(as_of)
        fid = f"F-{symbol.replace('.', '_')}-{pd.Timestamp(as_of).strftime('%Y%m%d')}-h{horizon}"
        contract = ForecastContract(
            forecast_id=fid, symbol=symbol, name=self.cfg.name_of(symbol),
            sector=self.cfg.sector_of(symbol), as_of=str(pd.Timestamp(as_of).date()),
            information_cutoff=cutoff.isoformat(timespec="minutes"), horizon=horizon,
            target=f"sign of the {horizon}-session forward close-to-close return",
            evaluation_rule=f"scored at the close {horizon} session(s) after {pd.Timestamp(as_of).date()}; "
                            f"correct if realised direction matches the stated bias",
            model_version="+".join(f"{k}@{self.cfg.feature_version}" for k in sorted(usable)),
            feature_version=self.cfg.feature_version,
            dataset_version=f"rows={len(self.matrix)}|end={str(self.matrix['date'].max().date())}",
            code_fingerprint=self._code_fingerprint(),
        )

        doc = {
            **asdict(contract),
            "probability_up": round(p_up, 4),
            "probability_raw": round(float(p_raw), 4),
            "direction": gate.direction,
            "expected_move_pct": round(100 * expected_move, 3),
            "interval_80": {"low_pct": round(100 * band.lower, 3) if np.isfinite(band.lower) else None,
                            "high_pct": round(100 * band.upper, 3) if np.isfinite(band.upper) else None,
                            "method": "volatility-scaled split conformal, alpha=0.2"},
            "member_probabilities": {k: (None if v is None else round(v, 4)) for k, v in member_probs.items()},
            "member_weights": bundle["weights"],
            "model_agreement": round(float(agreement), 3),
            "uncertainty": unc,
            "stability": stability,
            "regime": {"state": regime, "confidence": round(regime_conf, 3), "probabilities": regime_probs},
            "risk": risk.to_dict(),
            "data_quality": quality,
            "modality": modality,
            "evidence": evidence,
            "verdict": gate.verdict,
            "gate": gate.to_dict(),
            "latency_ms": {"inference": infer_ms, "training_ms": bundle["fit_ms"],
                           "train_rows": bundle["train_rows"], "train_end": bundle["train_end"]},
            "price_context": {
                "close": round(float(row["close"]), 2),
                "ret_1_pct": round(100 * float(row["ret_1"]), 3) if pd.notna(row.get("ret_1")) else None,
                "rsi_14": round(float(row["rsi_14"]), 1) if pd.notna(row.get("rsi_14")) else None,
                "vol_20_pct": round(100 * float(row["vol_20"]), 2) if pd.notna(row.get("vol_20")) else None,
                "rel_volume": round(float(row["rel_volume"]), 2) if pd.notna(row.get("rel_volume")) else None,
            },
            "trajectory": traj,
            "disclaimer": "Research output. Not investment advice. No guarantee of future returns.",
        }
        if persist:
            self.store.replace("forecasts", {"forecast_id": fid}, doc)
            self.store.replace("decisions", {"forecast_id": fid},
                               {"forecast_id": fid, "symbol": symbol, "as_of": doc["as_of"],
                                "verdict": gate.verdict, "trust_score": gate.trust_score,
                                "blocking_reasons": gate.blocking_reasons, "reason": gate.reason})
        return doc

    # ------------------------------------------------------------------ helpers
    def _trajectory(self, symbol: str, as_of: pd.Timestamp, horizon: int) -> List[float]:
        name = f"calibrated_predictions_h{horizon}"
        if not self.lake.exists("analytics", name):
            return []
        df = self.lake.read("analytics", name)
        sub = df[(df["symbol"] == symbol) & (df["date"] <= as_of)].sort_values("date").tail(10)
        col = "p_cal" if "p_cal" in sub.columns else "p_up"
        return [round(float(v), 4) for v in sub[col].dropna().tolist()]

    def _calibration_ece(self) -> float:
        path = self.cfg.reports_root / "results" / "calibration_h1.json"
        if path.exists():
            try:
                return float(json.loads(path.read_text())["after"]["ece"])
            except Exception:
                return 0.05
        return 0.05

    def _data_quality_for(self, symbol: str) -> Dict:
        path = self.cfg.reports_root / "results" / "data_quality.json"
        if not path.exists():
            return {"score": 100.0, "detail": "data quality report unavailable"}
        rep = json.loads(path.read_text())
        per = rep.get("per_symbol", {}).get(symbol, {})
        return {"score": float(per.get("coverage_score", rep.get("score", 95.0))),
                "rows": per.get("rows"), "quarantined": per.get("quarantined"),
                "staleness_days": per.get("staleness_days"),
                "detail": "share of this symbol's rows that passed the data contract"}

    @staticmethod
    def _modality_state(row: pd.Series) -> Dict:
        news_ok = bool(row.get("news_available", 0.0) > 0)
        channels = {"PRICE": True, "VOLUME": True, "MARKET_CONTEXT": pd.notna(row.get("market_breadth")),
                    "SECTOR": pd.notna(row.get("sector_ret_1")), "NEWS": news_ok}
        coverage = float(np.mean([1.0 if v else 0.0 for v in channels.values()]))
        return {"channels": {k: ("AVAILABLE" if v else "UNAVAILABLE") for k, v in channels.items()},
                "coverage": round(coverage, 3),
                "note": "Missing channels reduce forecast trust; they never silently default to neutral."}

    @staticmethod
    def _evidence(row: pd.Series, p_up: float, regime: str) -> Dict:
        items = []

        def add(channel: str, statement: str, value: Optional[float], supports: Optional[bool],
                strength: float, state: str):
            items.append({"channel": channel, "statement": statement,
                          "value": None if value is None else round(float(value), 4),
                          "direction": "SUPPORTS" if supports else ("CONTRADICTS" if supports is False else "NEUTRAL"),
                          "strength": round(float(np.clip(strength, 0, 1)), 3),
                          "information_state": state})

        bias_up = p_up >= 0.5
        if pd.notna(row.get("trend_strength")):
            t = float(row["trend_strength"])
            add("TREND", f"10/50-session trend gap {t*100:+.2f}%", t, (t > 0) == bias_up,
                min(abs(t) / 0.05, 1), "KNOWN")
        if pd.notna(row.get("rsi_14")):
            r = float(row["rsi_14"])
            add("MOMENTUM", f"RSI(14) at {r:.0f}", r, (r > 50) == bias_up, min(abs(r - 50) / 25, 1),
                "KNOWN")
        if pd.notna(row.get("rel_volume")):
            v = float(row["rel_volume"])
            add("VOLUME", f"Volume {v:.2f}x its 20-session average", v, v > 1.0,
                min(abs(v - 1) / 1.0, 1), "KNOWN")
        if pd.notna(row.get("sector_ret_1")):
            s = float(row["sector_ret_1"])
            add("SECTOR", f"Sector moved {s*100:+.2f}% in the session", s, (s > 0) == bias_up,
                min(abs(s) / 0.02, 1), "SUPPORTED")
        if pd.notna(row.get("market_breadth")):
            b = float(row["market_breadth"])
            add("BREADTH", f"{b*100:.0f}% of the universe advanced", b, (b > 0.5) == bias_up,
                min(abs(b - 0.5) * 2, 1), "KNOWN")
        if float(row.get("news_available", 0)) > 0:
            s = float(row.get("news_sentiment_5d", 0.0))
            add("NEWS", f"{int(row.get('news_count_5d', 0))} headline(s) in 5 days, mean sentiment {s:+.2f}",
                s, (s > 0) == bias_up, min(abs(s) + 0.2, 1), "SUPPORTED" if abs(s) > 0.15 else "UNCERTAIN")
        else:
            add("NEWS", "No headline coverage available at the information cutoff", None, None, 0.0, "UNKNOWN")
        supporting = [i for i in items if i["direction"] == "SUPPORTS"]
        contradicting = [i for i in items if i["direction"] == "CONTRADICTS"]
        s_strength = sum(i["strength"] for i in supporting)
        c_strength = sum(i["strength"] for i in contradicting)
        total = s_strength + c_strength
        conflict = round(min(s_strength, c_strength) / total, 3) if total else 0.0
        return {
            "items": items, "supporting": supporting, "contradicting": contradicting,
            "unavailable": [i for i in items if i["information_state"] == "UNKNOWN"],
            "conflict_index": conflict,
            "conflict_state": "CONFLICTED" if conflict > 0.38 else "ALIGNED" if conflict < 0.18 else "MIXED",
            "regime_context": regime,
            "causal_note": "Temporal association only. Causal relationship not established.",
        }

    def _code_fingerprint(self) -> str:
        """Content hash of the whole `vigil` package.

        Hashes the BYTES of every module (see vigil/mlops/fingerprints.py), so any edit to the
        forecasting code changes the fingerprint even when the file size is unchanged. Previously
        this hashed file names and sizes, which silently collided on same-length edits.
        """
        from pathlib import Path as _Path

        from ..mlops.fingerprints import tree_fingerprint

        return tree_fingerprint(_Path(__file__).resolve().parents[1], ("*.py",))

    # ------------------------------------------------------------------ batch
    def generate_current(self, horizon: int = 1) -> List[Dict]:
        out = []
        with timed("forecast.generate_current", horizon=horizon):
            for sym in self.cfg.symbols:
                try:
                    out.append(self.forecast(sym, horizon=horizon, persist=True))
                except Exception as exc:
                    log.error("forecast failed for %s: %s", sym, exc)
        verdicts = {}
        for d in out:
            verdicts[d["verdict"]] = verdicts.get(d["verdict"], 0) + 1
        log.info("generated %d forecasts: %s", len(out), verdicts)
        return out
