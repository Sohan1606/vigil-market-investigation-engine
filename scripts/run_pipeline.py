#!/usr/bin/env python3
"""VIGIL end-to-end pipeline runner.

    python scripts/run_pipeline.py --all                      # full build, horizon 1 only (default)
    python scripts/run_pipeline.py --stage features           # one stage
    python scripts/run_pipeline.py --all --offline            # no network: use the local cache
    python scripts/run_pipeline.py --all --horizons 1 3 5 20  # opt in to extended horizons

The default is DELIBERATELY the safe one: horizon 1 only. Extended horizons multiply the model,
calibration, uncertainty, drift, backtest and failure-lab stages and can exhaust memory on a
laptop, so they must be requested explicitly.

Every stage is idempotent and writes its artefacts to data/ and reports/results/.
`reports/results/pipeline_summary.json` is merged recursively across invocations (see
vigil/mlops/summary.py), so running stages in separate processes accumulates rather than clobbers.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vigil.config import load_config                                    # noqa: E402
from vigil.logging_utils import flush_metrics, get_logger               # noqa: E402
from vigil.mlops.summary import merge_summary_file                      # noqa: E402

log = get_logger("vigil.pipeline")

STAGES = ["ingest", "mapreduce", "features", "dataset", "stream", "regime", "graph",
          "patterns", "cases", "models", "ensemble", "calibration", "uncertainty", "drift",
          "backtest", "decision_quality", "forecasts", "failure_lab", "experiments", "report"]


def main() -> int:
    ap = argparse.ArgumentParser(description="VIGIL pipeline")
    ap.add_argument("--all", action="store_true", help="run every stage in order")
    ap.add_argument("--stage", action="append", default=[], choices=STAGES, help="run one stage")
    ap.add_argument("--offline", action="store_true", help="never touch the network")
    ap.add_argument("--horizons", nargs="*", type=int, default=[1],
                    help="forecast horizons to build (default: 1 — the safe default; "
                         "pass e.g. --horizons 1 3 5 20 to opt into extended horizons)")
    ap.add_argument("--force-pandas", action="store_true", help="skip Spark, use the pandas engine")
    args = ap.parse_args()
    stages = STAGES if args.all or not args.stage else args.stage
    cfg = load_config()
    summary = {}
    t_start = time.perf_counter()

    for stage in stages:
        t0 = time.perf_counter()
        log.info("=" * 24 + f" STAGE: {stage.upper()} " + "=" * 24)
        try:
            if stage == "ingest":
                from vigil.ingestion.pipeline import run_ingestion
                summary[stage] = run_ingestion(cfg, allow_network=not args.offline)
            elif stage == "mapreduce":
                from vigil.mapreduce.jobs import run_mapreduce_suite
                summary[stage] = {"jobs": [j["job_name"] for j in run_mapreduce_suite(cfg)["jobs"]]}
            elif stage == "features":
                from vigil.spark_jobs.features_job import build_features
                _, meta = build_features(cfg, force_pandas=args.force_pandas)
                summary[stage] = meta
            elif stage == "dataset":
                from vigil.features.dataset import build_dataset
                _, meta = build_dataset(cfg)
                summary[stage] = meta
            elif stage == "stream":
                from vigil.streaming.stream_job import build_event_tape, run_stream
                produced = build_event_tape(cfg)
                res = run_stream(cfg)
                summary[stage] = {**produced, "processed": res.events_processed,
                                  "throughput_eps": res.throughput_eps,
                                  "anomalies": len(res.anomalies)}
            elif stage == "regime":
                from vigil.intelligence.regime import run_regime_engine
                summary[stage] = run_regime_engine(cfg)[1]
            elif stage == "graph":
                from vigil.intelligence.graph import run_graph_analytics
                g = run_graph_analytics(cfg)
                summary[stage] = {"nodes": g["n_nodes"], "edges": g["n_edges"],
                                  "modularity": g["modularity"]}
            elif stage == "patterns":
                from vigil.intelligence.patterns import build_pattern_library
                summary[stage] = {"patterns": len(build_pattern_library(cfg))}
            elif stage == "cases":
                from vigil.intelligence.cases import generate_cases
                summary[stage] = {"cases": len(generate_cases(cfg))}
            elif stage == "models":
                from vigil.models.tournament import run_tournament
                summary[stage] = {f"horizon_{h}": run_tournament(cfg, horizon=h)["winner"]
                                  for h in args.horizons}
            elif stage == "ensemble":
                from vigil.models.ensemble import run_ensemble
                summary[stage] = {f"horizon_{h}": run_ensemble(cfg, horizon=h)["metrics"]["brier"]
                                  for h in args.horizons}
            elif stage == "calibration":
                from vigil.reliability.calibration import run_calibration
                summary[stage] = {f"horizon_{h}": run_calibration(cfg, horizon=h)["verdict"]
                                  for h in args.horizons}
            elif stage == "uncertainty":
                from vigil.reliability.uncertainty import run_uncertainty
                summary[stage] = {f"horizon_{h}": run_uncertainty(cfg, horizon=h)["conformal"]
                                  for h in args.horizons}
            elif stage == "drift":
                from vigil.reliability.drift import run_drift_monitor
                summary[stage] = {f"horizon_{h}": run_drift_monitor(cfg, horizon=h)["summary"]["unhealthy_models"]
                                  for h in args.horizons}
            elif stage == "backtest":
                from vigil.decision.backtest import run_backtest
                summary[stage] = {f"horizon_{h}": {k: v.get("annualised_return_pct")
                                                   for k, v in run_backtest(cfg, horizon=h)["strategies"].items()}
                                  for h in args.horizons}
            elif stage == "decision_quality":
                from vigil.decision.quality import run_decision_quality
                # No single "good decision rate": report the separated metrics (see vigil/decision/quality.py).
                summary[stage] = {
                    f"horizon_{h}": {k: run_decision_quality(cfg, horizon=h)[k] for k in
                                     ("forecast_accuracy_pct", "selective_accuracy_pct",
                                      "action_coverage_pct", "abstention_rate_pct",
                                      "mean_net_return_when_acted_bps", "decision_utility_bps",
                                      "downside_avoidance_bps")}
                    for h in args.horizons}
            elif stage == "forecasts":
                from vigil.decision.forecast_service import ForecastService
                svc = ForecastService(cfg)
                counts = {}
                for h in args.horizons:
                    docs = svc.generate_current(horizon=h)
                    counts[f"horizon_{h}"] = {"forecasts": len(docs),
                                              "verdicts": {v: sum(1 for d in docs if d["verdict"] == v)
                                                           for v in {d["verdict"] for d in docs}}}
                summary[stage] = counts
            elif stage == "failure_lab":
                from vigil.research.failure_lab import run_failure_lab
                summary[stage] = {f"horizon_{h}": run_failure_lab(cfg, horizon=h)["miss_rate_pct"]
                                  for h in args.horizons}
            elif stage == "experiments":
                from vigil.research.experiments import run_experiments
                res = run_experiments(cfg, horizon=args.horizons[0])
                summary[stage] = {e["experiment_id"]: e.get("conclusion", {}).get("verdict")
                                  for e in res["experiments"]}
            elif stage == "report":
                from vigil.mlops.tracking import Tracker, lineage
                tracker = Tracker(cfg)
                tracker.log_run("pipeline", {"stages": stages, "horizons": args.horizons,
                                             "offline": args.offline},
                                {k: str(v)[:400] for k, v in summary.items()},
                                artifacts=[str(p.relative_to(ROOT))
                                           for p in (cfg.reports_root / "results").glob("*.json")])
                summary[stage] = {"lineage_stages": len(lineage(cfg)["stages"])}
        except Exception as exc:
            log.exception("stage %s FAILED: %s", stage, exc)
            summary[stage] = {"error": f"{type(exc).__name__}: {exc}"}
        log.info("stage %s finished in %.1fs", stage, time.perf_counter() - t0)

    flush_metrics(cfg.reports_root / "results" / "telemetry.json")
    out = cfg.reports_root / "results" / "pipeline_summary.json"
    # Stages may be run in separate processes (memory-bounded rebuilds), so the summary is merged
    # RECURSIVELY: models.horizon_1 from one invocation and models.horizon_5 from another coexist.
    payload, overwritten = merge_summary_file(
        out, summary,
        invocation={"stages": stages, "horizons": args.horizons,
                    "seconds": round(time.perf_counter() - t_start, 1)})
    if overwritten:
        log.info("summary paths replaced by this invocation: %s", ", ".join(overwritten))
    log.info("pipeline complete in %.1fs → %s", time.perf_counter() - t_start, out)
    failures = [s for s, v in summary.items() if isinstance(v, dict) and "error" in v]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
