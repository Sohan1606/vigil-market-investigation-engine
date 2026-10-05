"""Decision quality — separated, individually defensible metrics.

VIGIL reports three different things and never merges them into one flattering number:

  FORECAST QUALITY : was the direction right? (accuracy over every evaluated opportunity)
  POLICY BEHAVIOUR : how often did the gate act, how often did it abstain?
  ECONOMIC OUTCOME : what did acting earn net of costs, and what did abstaining avoid?

Why there is no "good decision rate" any more
---------------------------------------------
Earlier versions of VIGIL counted every abstention below the cost threshold as a GOOD_ABSTENTION
and divided by all rows, producing a 99.18% "good decision rate" while the system acted on 4.08%
of opportunities. That metric rewarded doing nothing: a system that always abstains scores 100%.
It has been deleted. The replacement set below is defined so that abstention contributes exactly
zero to the utility metric and is reported as its own, separately labelled quantity.

Metric definitions (also emitted into the JSON artefact)
--------------------------------------------------------
forecast_accuracy_pct            : directional hit rate over ALL evaluated opportunities.
selective_accuracy_pct           : directional hit rate over the subset the gate ACTED on.
                                   > forecast_accuracy means the gate selects better-than-average
                                   opportunities; <= means its selection adds nothing.
action_coverage_pct              : share of opportunities acted on (the gate's coverage).
abstention_rate_pct              : 100 - action_coverage_pct. Reported, never scored.
mean_net_return_when_acted_bps   : mean signed forward return of acted rows minus round-trip cost.
decision_utility_bps             : coverage-weighted economic value PER EVALUATED OPPORTUNITY
                                   = action_coverage x mean_net_return_when_acted.
                                   Abstaining contributes 0, so abstaining cannot inflate it.
downside_avoidance_bps           : minus the mean net return the abstained opportunities WOULD have
                                   produced if traded. Positive = the refused trades would have lost.
abstention_value_bps             : mean_net_return_when_acted - mean_net_return_if_abstained_traded.
                                   Positive = acted trades beat the refused ones.
outcome_quality                  : realised outcome of the acted subset only — net win rate, mean
                                   and median net bps, worst/best trade.
diagnostics.policy_adherence_pct : mechanical self-consistency check (did the executed action match
                                   the stated gate rule?). Near 100% by construction. NOT a
                                   performance metric and must never be quoted as one.

All gating inputs (calibrated probability, model agreement, 20-session realised volatility) are
known before the trade. `fwd_ret` / `y_true` are used only to evaluate decisions after the fact.
"""
from __future__ import annotations

import json
from typing import Dict, Optional

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake
from .costs import CostModel

log = get_logger("vigil.decision.quality")

#: Descriptive categories. They describe WHAT happened; none of them is a score.
CATEGORIES = (
    "ACTED_WITH_EDGE_CORRECT",       # gate acted, edge cleared costs, direction right
    "ACTED_WITH_EDGE_INCORRECT",     # gate acted, edge cleared costs, direction wrong
    "ACTED_WITHOUT_EDGE",            # gate acted although the edge did not clear costs (defect)
    "ABSTAINED_BELOW_EDGE",          # no action, edge below cost threshold (policy behaving)
    "ABSTAINED_DESPITE_EDGE",        # no action although the edge cleared costs (missed)
)


def classify(row: pd.Series, cost_threshold_bps: float) -> str:
    """Descriptive label for one evaluated opportunity. Deliberately not a verdict."""
    acted, correct, edge = bool(row["acted"]), bool(row["correct"]), float(row["edge_bps"])
    if acted and edge >= cost_threshold_bps:
        return "ACTED_WITH_EDGE_CORRECT" if correct else "ACTED_WITH_EDGE_INCORRECT"
    if acted:
        return "ACTED_WITHOUT_EDGE"
    return "ABSTAINED_BELOW_EDGE" if edge < cost_threshold_bps else "ABSTAINED_DESPITE_EDGE"


METRIC_DEFINITIONS = {
    "forecast_accuracy_pct": "Directional hit rate over all evaluated opportunities.",
    "selective_accuracy_pct": "Directional hit rate over the subset the gate acted on. Compare "
                              "against forecast_accuracy_pct to judge whether selection helps.",
    "action_coverage_pct": "Share of evaluated opportunities the gate acted on.",
    "abstention_rate_pct": "Share of evaluated opportunities the gate declined. Reported, not scored.",
    "mean_net_return_when_acted_bps": "Mean signed forward return of acted opportunities minus the "
                                      "round-trip cost, in basis points.",
    "decision_utility_bps": "action_coverage x mean_net_return_when_acted, i.e. economic value per "
                            "evaluated opportunity. Abstaining contributes exactly 0.",
    "downside_avoidance_bps": "Minus the mean net return the abstained opportunities would have "
                              "produced if traded. Positive means the refused trades would have lost.",
    "abstention_value_bps": "mean_net_return_when_acted minus the mean net return of the abstained "
                            "opportunities had they been traded.",
    "outcome_quality": "Realised outcome of the acted subset only: net win rate, mean/median net "
                       "bps, worst and best trade.",
    "diagnostics.policy_adherence_pct": "Mechanical check that the executed action matched the "
                                        "stated gate rule. Near 100% by construction — NOT a "
                                        "performance metric.",
}


def run_decision_quality(cfg: Optional[VigilConfig] = None, horizon: int = 1,
                         band: float = 0.055) -> Dict:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    costs = CostModel.from_config(cfg)
    df = lake.read("analytics", f"calibrated_predictions_h{horizon}").dropna(subset=["y_true"])
    feats = lake.read("features", "equity_features")[["date", "symbol", "vol_20"]]
    df = df.merge(feats, on=["date", "symbol"], how="left")
    df["p"] = df["p_cal"].fillna(df["p_up"])
    # Pre-trade edge estimate: probability displacement x expected absolute move, where the
    # expected move is the trailing 20-session realised volatility (known at decision time).
    df["edge_bps"] = 2 * (df["p"] - 0.5).abs() * (df["vol_20"] / np.sqrt(252)).fillna(0.012) * 10_000
    threshold = costs.round_trip_bps + float(cfg.get("decision.min_edge_bps", 12.0))
    df["acted"] = ((df["p"] - 0.5).abs() >= band) & \
                  (df["model_agreement"].fillna(0) >= float(cfg.get("decision.min_model_agreement", 0.55))) & \
                  (df["edge_bps"] >= threshold)
    df["correct"] = ((df["p"] >= 0.5).astype(int) == df["y_true"].astype(int))
    df["category"] = df.apply(lambda r: classify(r, threshold), axis=1)
    counts = {c: int((df["category"] == c).sum()) for c in CATEGORIES}

    cost_frac = costs.round_trip_bps / 10_000
    acted = df[df["acted"]]
    abstained = df[~df["acted"]]
    net_acted = (np.sign(acted["p"] - 0.5) * acted["fwd_ret"]) - cost_frac
    net_abstained_if_traded = (np.sign(abstained["p"] - 0.5) * abstained["fwd_ret"]) - cost_frac
    mean_acted = float(net_acted.mean()) if len(acted) else 0.0
    mean_abstained = float(net_abstained_if_traded.mean()) if len(abstained) else 0.0
    coverage = float(df["acted"].mean())

    outcome_quality = {"trades": int(len(acted))}
    if len(acted):
        outcome_quality.update({
            "net_win_rate_pct": round(100 * float((net_acted > 0).mean()), 2),
            "mean_net_bps": round(10_000 * mean_acted, 2),
            "median_net_bps": round(10_000 * float(net_acted.median()), 2),
            "worst_trade_bps": round(10_000 * float(net_acted.min()), 2),
            "best_trade_bps": round(10_000 * float(net_acted.max()), 2),
        })

    out = {
        "horizon": horizon,
        "n_opportunities": int(len(df)),
        # --- forecast quality -------------------------------------------------
        "forecast_accuracy_pct": round(100 * float(df["correct"].mean()), 2),
        "selective_accuracy_pct": round(100 * float(acted["correct"].mean()), 2) if len(acted) else None,
        # --- policy behaviour -------------------------------------------------
        "action_coverage_pct": round(100 * coverage, 2),
        "abstention_rate_pct": round(100 * (1 - coverage), 2),
        "categories": counts,
        # --- economic outcome -------------------------------------------------
        "mean_net_return_when_acted_bps": round(10_000 * mean_acted, 2),
        "decision_utility_bps": round(10_000 * coverage * mean_acted, 3),
        "downside_avoidance_bps": round(-10_000 * mean_abstained, 2),
        "mean_net_return_if_abstentions_had_been_traded_bps": round(10_000 * mean_abstained, 2),
        "abstention_value_bps": round(10_000 * (mean_acted - mean_abstained), 2),
        "outcome_quality": outcome_quality,
        # --- diagnostics ------------------------------------------------------
        "diagnostics": {
            "policy_adherence_pct": round(100 * float(
                (df["acted"] == (df["edge_bps"] >= threshold) &
                 ((df["p"] - 0.5).abs() >= band) &
                 (df["model_agreement"].fillna(0) >= float(cfg.get("decision.min_model_agreement", 0.55)))
                 ).mean()), 2),
            "caveat": "policy_adherence_pct is a mechanical self-consistency check and is ~100% by "
                      "construction. It is not a measure of decision quality and must never be "
                      "reported as one.",
            "edge_threshold_bps": round(threshold, 2),
            "band": band,
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "interpretation": (
            "Abstention is reported, never scored: decision_utility_bps weights the net return by "
            "coverage, so a system that abstains everywhere scores 0.0 rather than ~100%. Positive "
            "downside_avoidance_bps means the refused trades would, on average, have lost money."),
        "removed_metric_note": (
            "good_decision_rate_pct was removed in v1.0. It counted every below-threshold "
            "abstention as a good decision and therefore rewarded inaction (99.18% while acting on "
            "4.08% of opportunities). No replacement aggregates abstention into a success rate."),
    }
    (cfg.reports_root / "results" / f"decision_quality_h{horizon}.json").write_text(json.dumps(out, indent=2))
    log.info("decision quality: coverage=%.2f%% selective_acc=%s utility=%.3f bps avoidance=%.2f bps",
             out["action_coverage_pct"], out["selective_accuracy_pct"],
             out["decision_utility_bps"], out["downside_avoidance_bps"])
    return out
