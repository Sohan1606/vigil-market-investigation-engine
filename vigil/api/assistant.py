"""Grounded research assistant.

Hard rule: the assistant may only speak from structured VIGIL records that are passed to it as
context. It performs intent matching over the available record types and renders the actual values.
When the record does not exist, it says so. There is no generative text model in this path, so it
cannot hallucinate market facts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..config import VigilConfig
from ..storage.docstore import DocumentStore


def _find_symbol(question: str, cfg: VigilConfig) -> Optional[str]:
    q = question.upper()
    for ins in cfg.instruments:
        root = ins.symbol.split(".")[0]
        if root in q or ins.name.upper() in q or ins.name.split()[0].upper() in q:
            return ins.symbol
    return None


def answer(question: str, cfg: VigilConfig, store: DocumentStore,
           results: Dict[str, Any]) -> Dict[str, Any]:
    q = question.lower().strip()
    symbol = _find_symbol(question, cfg)
    sources: List[str] = []

    def unavailable(what: str) -> Dict[str, Any]:
        return {"answer": f"VIGIL does not hold that information: {what}. "
                          f"Nothing is inferred when a record is missing.",
                "grounded_in": sources, "available": False}

    # ---- forecast / verdict questions ----
    if symbol and any(k in q for k in ("why", "verdict", "no action", "forecast", "trust", "probability",
                                       "should", "buy", "sell")):
        fc = store.find_one("forecasts", {"symbol": symbol, "horizon": 1})
        if not fc:
            return unavailable(f"no stored forecast for {symbol}")
        sources.append(f"docstore://forecasts/{fc['forecast_id']}")
        gate = fc["gate"]
        blocking = gate["blocking_reasons"]
        members = ", ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in fc["member_probabilities"].items()
                            if v is not None)
        lines = [
            f"{fc['name']} ({symbol}) — verdict {fc['verdict']} at horizon {fc['horizon']} session(s), "
            f"as of {fc['as_of']} (information cutoff {fc['information_cutoff']}).",
            f"Calibrated probability of an up move: {fc['probability_up']:.1%}; "
            f"model agreement {fc['model_agreement']:.0%}; members → {members}.",
            f"Uncertainty is {fc['uncertainty']['label']} ({fc['uncertainty']['uncertainty']}), "
            f"regime {fc['regime']['state']} at {fc['regime']['confidence']:.0%} confidence, "
            f"risk {fc['risk']['label']} ({fc['risk']['risk_score']}/100).",
            f"Expected edge {gate['expected_edge_bps']:.0f} bps vs round-trip cost "
            f"{gate['cost_bps']:.0f} bps → net {gate['net_edge_bps']:+.0f} bps.",
        ]
        if blocking:
            lines.append("Gate checks that did not clear: " + ", ".join(blocking) + ".")
        lines.append(gate["reason"])
        news = fc["modality"]["channels"].get("NEWS")
        if news == "UNAVAILABLE":
            lines.append("News coverage was UNAVAILABLE at the cutoff, so trust was reduced rather "
                         "than assuming neutral sentiment.")
        return {"answer": " ".join(lines), "grounded_in": sources, "available": True,
                "record": {"forecast_id": fc["forecast_id"], "verdict": fc["verdict"]}}

    # ---- case questions ----
    if "case" in q:
        m = re.search(r"v-\d{4}-\d{3}", q)
        case = store.find_one("cases", {"case_id": m.group(0).upper()}) if m else None
        if not case:
            cases = store.find("cases", {"symbol": symbol} if symbol else None)
            case = cases[-1] if cases else None
        if not case:
            return unavailable("no cases are stored")
        sources.append(f"docstore://cases/{case['case_id']}")
        s, c = case["evidence"]["supporting"], case["evidence"]["contradicting"]
        return {"answer": (f"{case['case_id']} — {case['title']} (opened {case['opened_at']}, "
                           f"status {case['status']}). Trigger: {case['trigger']} "
                           f"Evidence ledger holds {len(s)} supporting and {len(c)} contradicting item(s); "
                           f"interpretation is {case['interpretation']['information_state']}. "
                           f"{case['interpretation']['text']}"),
                "grounded_in": sources, "available": True}

    # ---- research / model questions ----
    if any(k in q for k in ("model", "best", "accuracy", "benchmark", "tournament", "xgboost", "lstm")):
        t = results.get("tournament_h1")
        if not t:
            return unavailable("the tournament artefact has not been generated")
        sources.append("reports/results/tournament_h1.json")
        rows = [e for e in t["leaderboard"] if e.get("available")]
        rows.sort(key=lambda e: e["metrics"]["brier"])
        table = "; ".join(f"{e['name']} Brier {e['metrics']['brier']:.5f} / AUC {e['metrics']['roc_auc']}"
                          for e in rows)
        return {"answer": (f"Ranked by probability quality on identical walk-forward folds: {table}. "
                           f"Winner: {t['winner']}. {t['honest_note']}"),
                "grounded_in": sources, "available": True}

    if any(k in q for k in ("news", "experiment", "hypothesis", "research")):
        e = results.get("experiments_h1")
        if not e:
            return unavailable("experiment results have not been generated")
        sources.append("reports/results/experiments_h1.json")
        bits = [f"{x['experiment_id']}: {x.get('conclusion', {}).get('verdict')}"
                for x in e["experiments"] if x.get("available")]
        return {"answer": "Research Lab conclusions — " + "; ".join(bits) +
                          ". Negative results are reported unchanged.",
                "grounded_in": sources, "available": True}

    if any(k in q for k in ("cost", "backtest", "profit", "return", "sharpe")):
        b = results.get("backtest_h1")
        if not b:
            return unavailable("the backtest artefact has not been generated")
        sources.append("reports/results/backtest_h1.json")
        parts = [f"{k}: {v['annualised_return_pct']}% annualised, Sharpe {v['sharpe']}"
                 for k, v in b["strategies"].items() if v.get("available")]
        return {"answer": ("Net of " + f"{b['costs']['round_trip_bps']:.0f} bps round-trip costs — " +
                           "; ".join(parts) + f". {b['honest_note']}"),
                "grounded_in": sources, "available": True}

    if any(k in q for k in ("regime", "market state", "pulse")):
        r = results.get("regime")
        if not r:
            return unavailable("the regime artefact has not been generated")
        sources.append("reports/results/regime.json")
        probs = ", ".join(f"{k} {v:.0%}" for k, v in r["current_probabilities"].items())
        return {"answer": (f"Current regime: {r['current_regime']} ({probs}). "
                           f"{r['transitions']} transitions across {r['sessions']} sessions. "
                           f"Method: {r['method']}."),
                "grounded_in": sources, "available": True}

    if any(k in q for k in ("stream", "kafka", "bloom", "dgim", "flajolet", "hdfs", "spark", "mapreduce")):
        s = results.get("streaming_stats")
        f = results.get("feature_build")
        mr = results.get("mapreduce_stats")
        if not s:
            return unavailable("streaming statistics have not been generated")
        sources += ["reports/results/streaming_stats.json", "reports/results/mapreduce_stats.json"]
        return {"answer": (
            f"Stream bus mode {s['bus_mode']}: {s['events_processed']} events processed at "
            f"{s['throughput_eps']:.0f} events/s, p95 latency {s['latency_p95_us']} µs. "
            f"Bloom filter removed {s['duplicates_filtered']} duplicates with expected FP rate "
            f"{s['bloom']['expected_fp_rate']}. Flajolet-Martin estimated "
            f"{s['flajolet_martin']['estimated_distinct']:.0f} distinct keys vs "
            f"{s['flajolet_martin']['exact_distinct']} exact "
            f"({s['flajolet_martin']['relative_error_pct']}% error). DGIM used "
            f"{s['dgim']['buckets']} buckets for a {s['dgim']['window']}-event window "
            f"({s['dgim']['relative_error_pct']}% error). "
            + (f"Feature engineering ran on {f['engine']} in {f['duration_ms']:.0f} ms. " if f else "")
            + (f"MapReduce jobs: {', '.join(j['job_name'] for j in mr['jobs'])}." if mr else "")),
            "grounded_in": sources, "available": True}

    return {"answer": ("I answer only from VIGIL's stored records. Try: 'Why is RELIANCE showing "
                       "NO ACTION?', 'Which model won the tournament?', 'Does news improve "
                       "prediction?', 'What is the current regime?' or 'How does the stream layer "
                       "perform?'"),
            "grounded_in": [], "available": False}
