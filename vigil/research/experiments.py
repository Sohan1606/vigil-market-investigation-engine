"""RESEARCH LAB — hypothesis-driven experiments, not a wall of charts.

Every experiment declares a question, a hypothesis, the dataset, the protocol and the metrics
BEFORE the result is computed, and the conclusion is allowed to be NOT SUPPORTED. All experiments
share the identical walk-forward protocol so their numbers are comparable; the only thing that
changes is the stated manipulation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..decision.costs import CostModel
from ..features.dataset import NEWS_FEATURES, feature_groups
from ..logging_utils import get_logger, timed
from ..models.base import ModelSpec
from ..models.metrics import classification_metrics
from ..models.tournament import default_feature_columns
from ..models.walkforward import SplitPlan, WalkForwardRunner
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake

log = get_logger("vigil.research.experiments")


@dataclass
class Experiment:
    experiment_id: str
    question: str
    hypothesis: str
    manipulation: str
    build: Callable[[pd.DataFrame, List[str]], tuple]
    success_metric: str = "brier"
    compare_to: Optional[str] = None


def attach_pre_trade_edge(preds: pd.DataFrame, cfg: VigilConfig) -> pd.DataFrame:
    """Attach the pre-trade edge estimate used by every VIGIL decision surface.

    edge_bps = 2 * |p - 0.5| * expected_move, with expected_move = vol_20 / sqrt(252), the
    trailing 20-session realised volatility known at the decision time. No future information
    (`fwd_ret`, `y_true`) may enter this column — it gates trades before the outcome exists.
    The identical formula is used in `vigil/decision/quality.py` and `vigil/decision/backtest.py`.
    """
    df = preds.copy()
    if "vol_20" not in df.columns:
        try:
            feats = DataLake(cfg).read("features", "equity_features")[["date", "symbol", "vol_20"]]
            df = df.merge(feats, on=["date", "symbol"], how="left")
        except Exception as exc:  # pragma: no cover - lake always present in the shipped repo
            log.warning("vol_20 unavailable for the pre-trade edge (%s); using the config default", exc)
            df["vol_20"] = np.nan
    df["expected_move"] = (df["vol_20"] / np.sqrt(252)).fillna(0.012)
    df["edge_bps"] = 2 * (df["p_up"] - 0.5).abs() * df["expected_move"] * 10_000
    return df


def _financial_metrics(preds: pd.DataFrame, costs: CostModel, band: float = 0.055,
                       gate: bool = False, cfg: Optional[VigilConfig] = None,
                       min_edge_bps: float = 12.0) -> Dict:
    """Cost-aware economics of one decision policy.

    Execution convention (identical to vigil/decision/backtest.py and docs/SCIENTIFIC_METHOD.md):
    the signal uses information up to the close of session t, the position is opened at that close
    and closed at the close of session t+h, and pays the full round trip (entry + exit).
    """
    df = preds.dropna(subset=["fwd_ret"]).copy()
    if df.empty:
        return {"available": False}
    signal = np.where(df["p_up"] >= 0.5 + band, 1.0, np.where(df["p_up"] <= 0.5 - band, -1.0, 0.0))
    gate_detail = {"band": band, "edge_gate": False}
    if gate:
        # Abstention policy: in addition to the probability band, the PRE-TRADE edge estimate must
        # clear the round-trip cost plus the configured minimum edge. Built only from information
        # available before the trade (calibrated probability + trailing volatility).
        df = attach_pre_trade_edge(df, cfg or load_config())
        threshold = costs.round_trip_bps + float(min_edge_bps)
        signal = np.where(df["edge_bps"].to_numpy() >= threshold, signal, 0.0)
        gate_detail = {"band": band, "edge_gate": True, "edge_threshold_bps": round(threshold, 2),
                       "edge_definition": "2 * |p - 0.5| * vol_20 / sqrt(252), known before the trade"}
    df["signal"] = signal
    abstention = round(100 * float((df["signal"] == 0).mean()), 2)
    traded = df[df["signal"] != 0]
    if traded.empty:
        return {"available": True, "trades": 0, "net_return_bps_per_trade": 0.0,
                "abstention_rate_pct": 100.0, "total_net_return_pct": 0.0,
                "net_return_bps_per_opportunity": 0.0, "policy": gate_detail}
    gross = (traded["signal"] * traded["fwd_ret"]).mean()
    cost = costs.round_trip_bps / 10_000          # entry + exit on every position
    net = gross - cost
    total = float(((traded["signal"] * traded["fwd_ret"]) - cost).sum())
    return {"available": True, "trades": int(len(traded)),
            "opportunities": int(len(df)),
            "gross_return_bps_per_trade": round(10_000 * float(gross), 2),
            "net_return_bps_per_trade": round(10_000 * float(net), 2),
            "net_return_bps_per_opportunity": round(10_000 * float(net) * len(traded) / len(df), 3),
            "total_net_return_pct": round(100 * total / max(len(df["symbol"].unique()), 1), 2),
            "abstention_rate_pct": abstention,
            "policy": gate_detail}


def _add_regime_features(df: pd.DataFrame, cfg: VigilConfig) -> tuple:
    lake = DataLake(cfg)
    if not lake.exists("analytics", "market_regime"):
        return df, []
    reg = lake.read("analytics", "market_regime")
    cols = [c for c in reg.columns if c.startswith("p_")]
    merged = df.merge(reg[["date"] + cols], on="date", how="left")
    for c in cols:
        merged[c] = merged[c].fillna(0.25)
    return merged, cols


def build_experiments(cfg: VigilConfig) -> List[Experiment]:
    groups_fn = lambda df: feature_groups(default_feature_columns(df))

    def price_only(df, _):
        g = groups_fn(df)
        return df, g["price"]

    def price_context(df, _):
        g = groups_fn(df)
        return df, g["price"] + g["context"]

    def with_news(df, _):
        g = groups_fn(df)
        return df, g["price"] + g["context"] + g["news"]

    def regime_aware(df, _):
        g = groups_fn(df)
        merged, rcols = _add_regime_features(df, cfg)
        return merged, g["price"] + g["context"] + g["news"] + rcols

    def delayed(df, _):
        """Information arrives one session late — the streaming-latency experiment."""
        g = groups_fn(df)
        cols = g["price"] + g["context"]
        lagged = df.sort_values(["symbol", "date"]).copy()
        lagged[cols] = lagged.groupby("symbol")[cols].shift(1)
        return lagged, cols

    return [
        Experiment("A_PRICE_ONLY", "Is price history alone enough to predict next-session direction?",
                   "Price-only features carry most of the available signal.",
                   "Feature set restricted to own-price technicals.", price_only),
        Experiment("B_PRICE_CONTEXT", "Does market/sector context improve prediction?",
                   "Adding benchmark, sector and breadth context improves probability quality.",
                   "Adds cross-sectional context features.", price_context, compare_to="A_PRICE_ONLY"),
        Experiment("C_WITH_NEWS", "Does news improve prediction?",
                   "Sparse real headline sentiment adds information beyond price and context.",
                   "Adds timestamp-aware 5-day news aggregates (5.58% of rows carry a headline in their trailing 5-day window; 2.10% of sessions have a same-session headline).",
                   with_news, compare_to="B_PRICE_CONTEXT"),
        Experiment("D_REGIME_AWARE", "Does regime awareness improve robustness?",
                   "Explicit regime probabilities help the model condition on market state.",
                   "Adds walk-forward regime posterior probabilities as features.",
                   regime_aware, compare_to="C_WITH_NEWS"),
        Experiment("E_DELAYED_DATA", "Does streaming latency degrade decision quality?",
                   "A one-session information delay measurably reduces predictive quality.",
                   "All features lagged by one session before training and inference.",
                   delayed, compare_to="B_PRICE_CONTEXT"),
    ]


def run_experiments(cfg: Optional[VigilConfig] = None, horizon: int = 1,
                    model_id: str = "xgboost") -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    costs = CostModel.from_config(cfg)
    base = lake.read("features", "model_matrix")
    plan = SplitPlan(train_years=int(cfg.get("models.walk_forward.train_years", 3)),
                     test_months=int(cfg.get("models.walk_forward.test_months", 12)),
                     min_train_rows=400, purge_days=1)
    from ..models.zoo import model_specs

    spec = next((s for s in model_specs(cfg.seed) if s.model_id == model_id), None)
    if spec is None:
        spec = next(s for s in model_specs(cfg.seed) if s.model_id == "random_forest")
        log.warning("%s unavailable — experiments fall back to %s", model_id, spec.model_id)

    results: List[Dict] = []
    preds_by_exp: Dict[str, pd.DataFrame] = {}
    for exp in build_experiments(cfg):
        data, cols = exp.build(base, [])
        runner = WalkForwardRunner(cols, horizon=horizon, plan=plan, seed=cfg.seed)
        with timed("experiment", experiment=exp.experiment_id):
            run = runner.run(data, spec)
        if not run.available:
            results.append({"experiment_id": exp.experiment_id, "available": False,
                            "failures": run.failures})
            continue
        m = classification_metrics(run.predictions)
        fin = _financial_metrics(run.predictions, costs, cfg=cfg)
        fin_gated = _financial_metrics(run.predictions, costs, gate=True, cfg=cfg)
        preds_by_exp[exp.experiment_id] = run.predictions
        results.append({
            "experiment_id": exp.experiment_id,
            "available": True,
            "research_question": exp.question,
            "hypothesis": exp.hypothesis,
            "manipulation": exp.manipulation,
            "dataset": {"rows": int(len(data)), "symbols": int(data["symbol"].nunique()),
                        "period": f"{data['date'].min().date()} → {data['date'].max().date()}",
                        "n_features": len(cols), "features": cols},
            "model": {"model_id": spec.model_id, "params": spec.params},
            "protocol": {"validation": "walk-forward rolling origin", "folds": len(run.folds),
                         "train_years": plan.train_years, "test_months": plan.test_months,
                         "purge_sessions": horizon + plan.purge_days},
            "metrics": {k: m[k] for k in ("n", "accuracy", "roc_auc", "brier", "ece", "f1",
                                          "log_loss", "accuracy_vs_majority")},
            "financial_metrics": fin,
            "financial_metrics_with_abstention": fin_gated,
            "compare_to": exp.compare_to,
        })

    # --- conclusions (computed after all arms are measured) ---
    by_id = {r["experiment_id"]: r for r in results if r.get("available")}
    for r in results:
        if not r.get("available"):
            continue
        ref = by_id.get(r.get("compare_to") or "")
        if ref is None:
            r["conclusion"] = {"verdict": "REFERENCE",
                               "statement": "Baseline arm for the comparisons below."}
            continue
        d_brier = ref["metrics"]["brier"] - r["metrics"]["brier"]      # positive = improvement
        d_auc = (r["metrics"]["roc_auc"] or 0) - (ref["metrics"]["roc_auc"] or 0)
        supported = d_brier > 0.0005 and d_auc > 0
        r["conclusion"] = {
            "verdict": "SUPPORTED" if supported else "NOT SUPPORTED",
            "delta_brier_vs_reference": round(d_brier, 5),
            "delta_auc_vs_reference": round(d_auc, 4),
            "statement": (
                f"Relative to {ref['experiment_id']}, the manipulation "
                f"{'improved' if supported else 'did not improve'} probability quality "
                f"(ΔBrier {d_brier:+.5f}, ΔAUC {d_auc:+.4f}). "
                + ("" if supported else "The hypothesis is reported as NOT SUPPORTED — no result was "
                                        "re-tuned to manufacture a winner.")),
        }

    # --- abstention arm (same model as D, different decision policy) ---
    if "D_REGIME_AWARE" in preds_by_exp:
        p = preds_by_exp["D_REGIME_AWARE"]
        no_abstain = _financial_metrics(p, costs, band=0.0, cfg=cfg)
        with_abstain = _financial_metrics(p, costs, band=0.055, gate=True, cfg=cfg)
        results.append({
            "experiment_id": "F_ABSTENTION_POLICY",
            "available": True,
            "research_question": "Does abstention improve decision quality?",
            "hypothesis": "Refusing low-edge forecasts improves net outcome per decision.",
            "manipulation": "Identical model (arm D); only the decision policy changes.",
            "model": {"model_id": spec.model_id, "note": "no retraining — policy-only comparison"},
            "metrics": {"note": "prediction metrics are identical to arm D by construction"},
            "financial_metrics": {"trade_everything": no_abstain, "with_abstention": with_abstain},
            "caveat": (
                "This arm measures a POLICY on arm D's single XGBoost model, per executed trade, "
                "with no model-agreement or uncertainty gate and no portfolio construction. It is "
                "not the production Decision Gate: the cost-aware portfolio backtest "
                "(reports/results/backtest_h1.json, strategy ADAPTIVE_GATE) remains NEGATIVE at "
                "-7.0% annualised, and -13.5% when execution is delayed one session. A positive "
                "per-trade number here does not mean VIGIL is profitable."),
            "conclusion": {
                "verdict": "SUPPORTED" if with_abstain.get("net_return_bps_per_trade", -99) >
                           no_abstain.get("net_return_bps_per_trade", -99) else "NOT SUPPORTED",
                "statement": (
                    f"Net return per executed trade moves from "
                    f"{no_abstain.get('net_return_bps_per_trade')} bps (trade everything) to "
                    f"{with_abstain.get('net_return_bps_per_trade')} bps with abstention, at an "
                    f"abstention rate of {with_abstain.get('abstention_rate_pct')}%. Per evaluated "
                    f"opportunity (abstentions counted as zero) the policy moves from "
                    f"{no_abstain.get('net_return_bps_per_opportunity')} bps to "
                    f"{with_abstain.get('net_return_bps_per_opportunity')} bps. The gate uses only "
                    f"pre-trade information (calibrated probability and trailing volatility)."),
            },
        })

    out = {"horizon": horizon, "model_used": spec.model_id, "experiments": results,
           "integrity_note": "All arms share one protocol, one seed and one dataset version. "
                             "Negative results are published as-is."}
    (cfg.reports_root / "results" / f"experiments_h{horizon}.json").write_text(
        json.dumps(out, indent=2, default=str))
    store = DocumentStore(cfg)
    store.drop("experiments")
    for r in results:
        store.insert("experiments", {"experiment_id": r["experiment_id"], **r})
    log.info("experiments complete: %s", {r["experiment_id"]: r.get("conclusion", {}).get("verdict")
                                          for r in results})
    return out
