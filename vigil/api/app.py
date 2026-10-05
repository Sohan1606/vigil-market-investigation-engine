"""VIGIL API — FastAPI service for the web experience.

Design rules:
  * every endpoint serves *precomputed pipeline artefacts* or cheap document lookups;
  * heavy recomputation (replay, stress) is explicit and rate-limited by being opt-in;
  * a missing artefact returns a meaningful degraded payload, never a blank error;
  * no large frames are pushed to the browser — aggregation happens server-side.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import REPO_ROOT
from ..decision.audit import audit_forecast
from ..decision.stress import stress_forecast
from ..logging_utils import get_logger
from ..mapreduce.hadoop_runner import mapreduce_status
from ..mlops.tracking import Tracker, lineage
from ..recommend.recommender import recommend
from ..research.replay import replay, replay_timeline
from . import assistant as assistant_mod
from .deps import artefact_missing, config, forecast_service, lake, result, store

log = get_logger("vigil.api")

app = FastAPI(title="VIGIL — Market Investigation Engine", version="1.0.0",
              description="Investigate before you believe.")

origins = [o.strip() for o in os.getenv("VIGIL_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"], allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return response


@app.exception_handler(Exception)
async def graceful_error(request: Request, exc: Exception):
    """Explain what failed, what is affected and what fallback exists."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={
        "state": "DEGRADED",
        "what_failed": type(exc).__name__,
        "affected": request.url.path,
        "fallback": "Other VIGIL surfaces remain available; rebuild artefacts with "
                    "`python scripts/run_pipeline.py --all`.",
        "detail": "Internal error details are not exposed by design.",
    })


# --------------------------------------------------------------------------- health
@app.get("/api/health")
def health() -> Dict[str, Any]:
    cfg = config()
    st = store().status()
    stream = result("streaming_stats", {})
    feat = result("feature_build", {})
    drift = result("drift_h1", {})
    quality = result("data_quality", {})
    inv = lake().inventory()
    try:
        from ..spark_jobs.session import resolve_java_home

        java_home, java_detail = resolve_java_home()
    except Exception:
        java_home, java_detail = None, "spark module unavailable"
    storage = lake().status().to_dict()
    mr_status = mapreduce_status(cfg)
    components = [
        {"component": "DATA LAKE", "state": "OK" if inv else "EMPTY",
         "mode": storage["label"],
         "detail": f"{storage['label']} — {len(inv)} datasets, "
                   f"{sum(i['blocks'] for i in inv)} parquet blocks"
                   + ("" if storage["available"] else f" ({storage['detail']})")},
        {"component": "MAPREDUCE", "state": "OK",
         "mode": mr_status["label"],
         "detail": mr_status["detail"]},
        {"component": "DOCUMENT STORE", "state": st.state,
         "mode": ("MONGODB SERVER" if st.backend.startswith("mongodb")
                  else "EMBEDDED MONGITA (LOCAL)"),
         "detail": f"{st.backend} — {st.detail}"
                   + ("" if st.healthy else " (probe read failed: reported DEGRADED, "
                                            "not healthy)")},
        {"component": "SPARK", "state": "OK" if feat.get("engine") == "spark" else "FALLBACK",
         "mode": ("SPARK" if feat.get("engine") == "spark" else "PANDAS FALLBACK"),
         "detail": feat.get("engine_detail", java_detail)},
        {"component": "STREAM BUS", "state": "OK" if stream else "IDLE",
         "mode": ("KAFKA BROKER" if str(stream.get("bus_mode", "")).upper() == "KAFKA"
                  else "IN-PROCESS EVENT BUS"),
         "detail": f"{'KAFKA BROKER' if str(stream.get('bus_mode', '')).upper() == 'KAFKA' else 'IN-PROCESS EVENT BUS'}"
                   f" — {stream.get('events_processed', 0)} events, "
                   f"{stream.get('throughput_eps', 0)} ev/s"},
        {"component": "MODELS", "state": "OK" if store().count("models") else "MISSING",
         "detail": f"{store().count('models')} registered; unhealthy="
                   f"{drift.get('summary', {}).get('unhealthy_models', [])}"},
        {"component": "DATA QUALITY", "state": "OK" if quality.get("score", 0) >= 90 else "WATCH",
         "detail": f"score {quality.get('score')} / 100"},
    ]
    degraded = [c for c in components if c["state"] not in ("OK",)]
    return {
        "system": "VIGIL",
        "mode": cfg.mode,
        "storage_mode": storage["label"],
        "mapreduce_engine": mr_status["label"],
        "data_mode": "HISTORICAL / DETERMINISTIC REPLAY" if stream.get("bus_mode") != "KAFKA"
                     else "LIVE KAFKA STREAM",
        "vigil_health": "HEALTHY" if not degraded else "PARTIAL",
        "components": components,
        "note": "VIGIL HEALTH describes the system. Forecast trust is a separate, per-forecast measure.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- pulse
@app.get("/api/pulse")
def pulse() -> Dict[str, Any]:
    regime = result("regime")
    if not regime:
        return artefact_missing("regime.json", "market regime")
    lk = lake()
    breadth = lk.read("analytics", "mr_market_breadth").tail(60) if lk.exists("analytics", "mr_market_breadth") else pd.DataFrame()
    state = lk.read("analytics", "market_regime").tail(90)
    latest = state.iloc[-1]
    stream = result("streaming_stats", {})
    cases = store().find("cases")
    open_cases = [c for c in cases if c.get("status") in ("TRIGGERED", "INVESTIGATING")]
    attention: List[Dict[str, str]] = []
    for sym, score in (stream.get("attention") or [])[:4]:
        attention.append({"headline": f"{config().name_of(sym)} activity drew repeated attention",
                          "detail": f"decayed anomaly score {score}", "symbol": sym})
    vol_now = float(latest["bench_vol_20"]) * 100
    vol_prev = float(state.iloc[-20]["bench_vol_20"]) * 100 if len(state) > 20 else vol_now
    if vol_now > vol_prev * 1.1:
        attention.append({"headline": "Volatility expanded", "symbol": None,
                          "detail": f"20-session realised volatility {vol_now:.1f}% vs {vol_prev:.1f}% a month ago"})
    participation = float(latest.get("market_breadth_20", 0.5))
    pressure = float(np.clip(100 * (0.5 * (1 - participation) +
                                    0.3 * min(vol_now / 30.0, 1.0) +
                                    0.2 * (1 - float(latest.get("regime_confidence", 0.5)))), 0, 100))
    recent = state.tail(30)
    return {
        "available": True,
        "as_of": str(pd.Timestamp(latest["date"]).date()),
        "market_state": str(latest["regime"]),
        "regime_probabilities": {k.replace("p_", "").upper(): round(float(latest[k]), 4)
                                 for k in state.columns if str(k).startswith("p_")},
        "market_pressure": round(pressure, 0),
        "stability": "HIGH" if latest["regime_confidence"] > 0.8 else
                     "MEDIUM" if latest["regime_confidence"] > 0.55 else "LOW",
        "participation": "STRONG" if participation > 0.55 else "NEUTRAL" if participation > 0.45 else "WEAK",
        "participation_pct": round(100 * participation, 1),
        "benchmark_trend_20_pct": round(100 * float(latest["bench_trend_20"]), 2),
        "volatility_20_pct": round(vol_now, 2),
        "drawdown_pct": round(100 * float(latest["drawdown"]), 2),
        "attention": attention[:4],
        "open_cases": [{"case_id": c["case_id"], "title": c["title"], "sector": c["sector"],
                        "opened_at": c["opened_at"], "severity": round(float(c["severity"]), 1)}
                       for c in sorted(open_cases, key=lambda c: c["opened_at"], reverse=True)[:4]],
        "open_case_count": len(open_cases),
        "field": _market_field(),
        "sparkline": [{"date": str(pd.Timestamp(r["date"]).date()),
                       "pressure": round(float(100 * (1 - r.get("market_breadth_20", 0.5))), 1),
                       "regime": r["regime"]} for _, r in recent.iterrows()],
        "data_mode": "HISTORICAL / DETERMINISTIC REPLAY",
    }


def _market_field() -> Dict[str, Any]:
    """Compact node/edge payload for the signal-field visual (server-side aggregated)."""
    g = result("graph")
    forecasts = {f["symbol"]: f for f in store().find("forecasts") if f.get("horizon") == 1}
    if not g or "nodes" not in g or not isinstance(g["nodes"], list):
        return {"available": False}
    nodes = []
    for n in g["nodes"]:
        fc = forecasts.get(n["symbol"], {})
        nodes.append({**n,
                      "probability": fc.get("probability_up"),
                      "verdict": fc.get("verdict"),
                      "risk": (fc.get("risk") or {}).get("risk_score"),
                      "vol": (fc.get("risk") or {}).get("annualised_vol_pct")})
    return {"available": True, "nodes": nodes, "edges": g["edges"],
            "communities": g["communities"], "modularity": g["modularity"],
            "correlation_state": g.get("correlation_change", {}).get("state")}


# --------------------------------------------------------------------------- investigate
@app.get("/api/cases")
def cases(limit: int = 40, sector: Optional[str] = None, status: Optional[str] = None):
    docs = store().find("cases")
    if sector:
        docs = [c for c in docs if c["sector"] == sector.upper()]
    if status:
        docs = [c for c in docs if c["status"] == status.upper()]
    docs.sort(key=lambda c: c["opened_at"], reverse=True)
    return {"count": len(docs), "cases": [{
        "case_id": c["case_id"], "title": c["title"], "sector": c["sector"], "status": c["status"],
        "opened_at": c["opened_at"], "severity": round(float(c["severity"]), 2),
        "symbols": c["symbols"], "information_state": c["interpretation"]["information_state"],
        "news_available": c.get("news_available", False),
    } for c in docs[:limit]]}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    doc = store().find_one("cases", {"case_id": case_id})
    if not doc:
        raise HTTPException(404, f"case {case_id} not found")
    return doc


# --------------------------------------------------------------------------- forecast
@app.get("/api/universe")
def universe():
    cfg = config()
    forecasts = {f["symbol"]: f for f in store().find("forecasts") if f.get("horizon") == 1}
    return {"benchmark": cfg.benchmark, "instruments": [{
        "symbol": i.symbol, "name": i.name, "sector": i.sector,
        "verdict": forecasts.get(i.symbol, {}).get("verdict"),
        "probability": forecasts.get(i.symbol, {}).get("probability_up"),
    } for i in cfg.instruments]}


@app.get("/api/forecast/{symbol}")
def forecast(symbol: str, horizon: int = Query(1, ge=1, le=60), live: bool = False):
    symbol = symbol.upper()
    doc = store().find_one("forecasts", {"symbol": symbol, "horizon": horizon})
    if doc and not live:
        return {**doc, "served_from": "stored forecast snapshot"}
    try:
        svc = forecast_service()
        doc = svc.forecast(symbol, horizon=horizon, persist=True)
        return {**doc, "served_from": "computed on demand"}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/forecast/{symbol}/history")
def forecast_history(symbol: str, horizon: int = 1, limit: int = 120):
    name = f"calibrated_predictions_h{horizon}"
    if not lake().exists("analytics", name):
        return artefact_missing(name, "out-of-sample prediction history")
    df = lake().read("analytics", name)
    sub = df[df["symbol"] == symbol.upper()].sort_values("date").tail(limit)
    col = "p_cal" if "p_cal" in sub.columns else "p_up"
    return {"symbol": symbol.upper(), "horizon": horizon, "points": [
        {"date": str(pd.Timestamp(r["date"]).date()), "probability": round(float(r[col]), 4),
         "agreement": round(float(r["model_agreement"]), 3),
         "outcome": (None if pd.isna(r["y_true"]) else int(r["y_true"])),
         "realised_pct": (None if pd.isna(r["fwd_ret"]) else round(100 * float(r["fwd_ret"]), 3))}
        for _, r in sub.iterrows()]}


@app.get("/api/decision/{symbol}")
def decision(symbol: str, horizon: int = 1):
    doc = store().find_one("forecasts", {"symbol": symbol.upper(), "horizon": horizon})
    if not doc:
        raise HTTPException(404, "no stored forecast — generate one from the Forecast space first")
    return {"symbol": doc["symbol"], "as_of": doc["as_of"], "verdict": doc["verdict"],
            "gate": doc["gate"], "uncertainty": doc["uncertainty"], "stability": doc["stability"],
            "risk": doc["risk"], "regime": doc["regime"], "modality": doc["modality"],
            "member_probabilities": doc["member_probabilities"], "member_weights": doc["member_weights"],
            "model_agreement": doc["model_agreement"], "data_quality": doc["data_quality"],
            "drift": result("drift_h1", {}).get("models", {}).get("adaptive_ensemble", {}),
            "calibration": (result("calibration_h1") or {}).get("after")}


@app.post("/api/forecast/{symbol}/stress")
def stress(symbol: str, horizon: int = 1):
    doc = store().find_one("forecasts", {"symbol": symbol.upper(), "horizon": horizon})
    if not doc:
        raise HTTPException(404, "no stored forecast to stress")
    return stress_forecast(doc, config())


@app.get("/api/audit/{forecast_id}")
def audit(forecast_id: str):
    doc = store().find_one("forecasts", {"forecast_id": forecast_id})
    if not doc:
        raise HTTPException(404, f"forecast {forecast_id} not found")
    report = audit_forecast(doc, config())
    store().replace("audits", {"audit_id": report["audit_id"]}, report)
    return report


# --------------------------------------------------------------------------- replay
@app.get("/api/replay")
def replay_endpoint(symbol: str, date: str, horizon: int = Query(1, ge=1, le=20)):
    try:
        return replay(symbol.upper(), date, horizon=horizon, cfg=config(), service=forecast_service())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/replay/timeline")
def replay_timeline_endpoint(symbol: str, start: str, end: str, step: int = 5, horizon: int = 1):
    return replay_timeline(symbol.upper(), start, end, step=step, horizon=horizon, cfg=config())


# --------------------------------------------------------------------------- research lab
@app.get("/api/research/overview")
def research_overview():
    return {
        "tournament": result("tournament_h1", artefact_missing("tournament_h1.json", "model tournament")),
        "experiments": result("experiments_h1", artefact_missing("experiments_h1.json", "experiments")),
        "calibration": result("calibration_h1", artefact_missing("calibration_h1.json", "calibration")),
        "uncertainty": result("uncertainty_h1", artefact_missing("uncertainty_h1.json", "uncertainty")),
        "backtest": result("backtest_h1", artefact_missing("backtest_h1.json", "backtest")),
        "decision_quality": result("decision_quality_h1",
                                   artefact_missing("decision_quality_h1.json", "decision quality")),
        "failure_lab": result("failure_lab_h1", artefact_missing("failure_lab_h1.json", "failure lab")),
        "patterns": result("patterns", artefact_missing("patterns.json", "pattern library")),
        "drift": result("drift_h1", artefact_missing("drift_h1.json", "drift monitor")),
    }


@app.get("/api/research/cemetery")
def cemetery(limit: int = 20):
    docs = store().find("cemetery")
    docs.sort(key=lambda d: -abs(float(d.get("stated_probability", 0.5)) - 0.5))
    return {"count": len(docs), "tombstones": docs[:limit]}


@app.get("/api/research/compare")
def compare(kind: str = Query("model", pattern="^(model|experiment|regime|symbol)$"),
            a: Optional[str] = None, b: Optional[str] = None):
    if kind == "model":
        t = result("tournament_h1")
        if not t:
            return artefact_missing("tournament_h1.json", "model tournament")
        rows = {e["model_id"]: e for e in t["leaderboard"] if e.get("available")}
        pick = [rows.get(a or ""), rows.get(b or "")]
        pick = [p for p in pick if p] or list(rows.values())[:2]
        return {"kind": kind, "items": [{"id": p["model_id"], "name": p["name"],
                                         "metrics": p["metrics"], "by_regime": p["by_regime"]} for p in pick]}
    if kind == "experiment":
        e = result("experiments_h1")
        if not e:
            return artefact_missing("experiments_h1.json", "experiments")
        rows = {x["experiment_id"]: x for x in e["experiments"] if x.get("available")}
        pick = [rows.get(a or ""), rows.get(b or "")]
        pick = [p for p in pick if p] or list(rows.values())[:2]
        return {"kind": kind, "items": pick}
    if kind == "regime":
        t = result("tournament_h1") or {}
        winner = next((x for x in t.get("leaderboard", []) if x.get("model_id") == t.get("winner")), None)
        return {"kind": kind, "items": (winner or {}).get("by_regime", [])}
    df = lake().read("features", "equity_features")
    out = []
    for sym in [s for s in (a, b) if s]:
        sub = df[df["symbol"] == sym.upper()].tail(250)
        if sub.empty:
            continue
        out.append({"symbol": sym.upper(),
                    "annualised_vol_pct": round(100 * float(sub["vol_20"].iloc[-1]), 2),
                    "beta_60": round(float(sub["beta_60"].iloc[-1]), 3),
                    "return_250_pct": round(100 * float(sub["close"].iloc[-1] / sub["close"].iloc[0] - 1), 2),
                    "rsi_14": round(float(sub["rsi_14"].iloc[-1]), 1)})
    return {"kind": kind, "items": out}


# --------------------------------------------------------------------------- observatory
@app.get("/api/observatory")
def observatory():
    lk = lake()
    inv = lk.inventory()
    st = store().status()
    stream = result("streaming_stats", {})
    mr = result("mapreduce_stats", {})
    feat = result("feature_build", {})
    quality = result("data_quality", {})
    ingest = result("ingestion_summary", {})
    telemetry = result("telemetry", [])
    stage_latency: Dict[str, List[float]] = {}
    for rec in telemetry if isinstance(telemetry, list) else []:
        stage_latency.setdefault(rec["stage"], []).append(rec.get("duration_ms", 0.0))
    # Infrastructure modes are PROBED, never inferred from the existence of an artefact file.
    storage = lk.status().to_dict()
    mr_status = mapreduce_status(config())
    bus_mode = (stream.get("bus_mode") or "UNKNOWN").upper()
    kafka_mode = "KAFKA BROKER" if bus_mode == "KAFKA" else "IN-PROCESS EVENT BUS"
    mongo_mode = "MONGODB SERVER" if st.backend.startswith("mongodb") else "EMBEDDED MONGITA (LOCAL)"
    return {
        "infrastructure_modes": {
            "storage": {"label": storage["label"], "requested": storage["requested_mode"],
                        "active": storage["active_mode"], "available": storage["available"],
                        "uri": storage["uri"], "detail": storage["detail"]},
            "mapreduce": {"label": mr_status["label"], "requested": mr_status["requested_engine"],
                          "active": mr_status["engine"],
                          "hadoop_available": mr_status["hadoop_available"],
                          "detail": mr_status["detail"]},
            "streaming": {"label": f"Event bus: {kafka_mode}", "active": bus_mode,
                          "kafka_available": bus_mode == "KAFKA",
                          "detail": "Kafka is used when KAFKA_BOOTSTRAP_SERVERS points at a reachable "
                                    "broker; otherwise the deterministic in-process bus replays the "
                                    "event tape. The sketch algorithms are identical either way."},
            "docstore": {"label": f"Document store: {mongo_mode}"
                                  + ("" if st.healthy else " — DEGRADED"),
                         "active": st.backend,
                         "server_backed": st.backend.startswith("mongodb"),
                         "healthy": st.healthy, "state": st.state,
                         "probe_count": st.probe_count, "detail": st.detail},
            "note": "Each label reflects what is RUNNING NOW, probed at request time. The presence "
                    "of an artefact file never implies that its infrastructure is operational.",
        },
        "architecture": [
            {"id": "SOURCE", "title": "Market sources", "what": "Yahoo Finance OHLCV for the NSE "
             "universe plus Google News RSS headlines.",
             "why": "Real, timestamped, reproducible inputs — no synthetic prices anywhere.",
             "input": "HTTP APIs", "output": "CSV cache + raw lake zone",
             "syllabus": "Big Data characteristics: volume, velocity, variety, veracity",
             "metrics": {"symbols": ingest.get("symbols"), "rows": ingest.get("rows_raw"),
                         "news_documents": ingest.get("news_documents"),
                         "direct_session_coverage_pct": ingest.get("direct_session_coverage_pct"),
                         "rolling_5d_context_rows_pct": (result("dataset_meta") or {}).get(
                             "rolling_5d_context_rows_pct")}},
            {"id": "HDFS", "title": storage["label"],
             "what": "Hive-partitioned Parquet zones raw → curated → features → analytics, "
                     f"currently served by the {storage['active_mode']} backend.",
             "why": "Column pruning and partition pruning; byte-identical layout in both modes, so "
                    "STORAGE_MODE=hdfs changes the filesystem, not the data model.",
             "input": "validated records", "output": "parquet blocks",
             "syllabus": "HDFS, block storage, partitioning",
             "mode": storage["active_mode"], "mode_label": storage["label"],
             "mode_detail": storage["detail"],
             "metrics": {"storage_mode": storage["active_mode"],
                         "requested_mode": storage["requested_mode"],
                         "root": storage["uri"],
                         "datasets": len(inv), "blocks": sum(i["blocks"] for i in inv),
                         "bytes": sum(i["bytes"] for i in inv)}},
            {"id": "MAPREDUCE", "title": f"MapReduce analytics — {mr_status['engine']}",
             "what": "Process-parallel map → shuffle/sort → reduce over parquet splits."
                     if mr_status["engine"] != "HADOOP" else
                     "Hadoop Streaming jobs submitted to the cluster (hadoop/).",
             "why": "Aggregations whose programming model is the point: yearly profiles, market "
                    "breadth, and a news inverted index.",
             "input": "parquet splits", "output": "analytics datasets",
             "syllabus": "MapReduce, combiners, shuffle & sort",
             "mode": mr_status["engine"], "mode_label": mr_status["label"],
             "mode_detail": mr_status["detail"],
             "metrics": {"engine": mr_status["engine"],
                         "requested_engine": mr_status["requested_engine"],
                         "hadoop_available": mr_status["hadoop_available"],
                         "streaming_scripts_validated": mr_status["streaming_scripts_validated"],
                         "jobs": [j["job_name"] for j in mr.get("jobs", [])],
                         "total_map_input_records": sum(j["map_input_records"] for j in mr.get("jobs", [])),
                         "total_ms": round(sum(j["total_ms"] for j in mr.get("jobs", [])), 1)}},
            {"id": "SPARK", "title": "Spark feature engineering",
             "what": "PySpark window functions and Spark SQL build 34 point-in-time features.",
             "why": "Distributed, lazy, and the same code scales beyond one machine.",
             "input": "curated zone", "output": "features zone",
             "syllabus": "Spark, RDD/DataFrame, Spark SQL",
             "metrics": {"engine": feat.get("engine"), "rows": feat.get("rows"),
                         "features": feat.get("feature_count"), "duration_ms": feat.get("duration_ms")}},
            {"id": "STREAMING", "title": f"Event stream + sketches — {kafka_mode}",
             "what": "Market event tape through Bloom, Flajolet-Martin, DGIM and decay counters.",
             "why": "Bounded-memory analytics over an unbounded stream.",
             "input": "session events", "output": "anomalies, sketch statistics",
             "syllabus": "Data stream model, Bloom filter, Flajolet-Martin, DGIM",
             "mode": bus_mode, "mode_label": f"Event bus: {kafka_mode}",
             "metrics": {"bus_mode": kafka_mode, "kafka_available": bus_mode == "KAFKA",
                         "processed": stream.get("events_processed"),
                         "throughput_eps": stream.get("throughput_eps"),
                         "p95_latency_us": stream.get("latency_p95_us"),
                         "duplicates_filtered": stream.get("duplicates_filtered")}},
            {"id": "MONGODB", "title": f"Document store — {mongo_mode}",
             "what": "Forecasts, cases, events, experiments, audits and news as documents.",
             "why": "Schema-flexible nested records with indexes — relational tables would fight this.",
             "input": "intelligence outputs", "output": "queryable collections",
             "syllabus": "NoSQL, document stores, indexing",
             "mode": st.backend, "mode_label": f"Document store: {mongo_mode}",
             "metrics": {"backend": st.backend, "server_backed": st.backend.startswith("mongodb"),
                         **store().stats()}},
            {"id": "INTELLIGENCE", "title": "Regime, anomaly, graph",
             "what": "Gaussian-mixture regimes, streaming anomaly detection, correlation networks.",
             "why": "Context decides whether a prediction means anything.",
             "input": "features + events", "output": "regimes, cases, communities",
             "syllabus": "Clustering, graph analytics, community discovery",
             "metrics": {"regime": (result("regime") or {}).get("current_regime"),
                         "communities": len((result("graph") or {}).get("communities", [])),
                         "cases": store().count("cases")}},
            {"id": "VIGIL", "title": "Forecast → Gate → Verdict",
             "what": "Walk-forward models, adaptive ensemble, calibration, Decision Gate.",
             "why": "A forecast is only useful with its reliability attached.",
             "input": "model matrix", "output": "forecast contracts + verdicts",
             "syllabus": "Real-life stock-market application of Big Data analytics",
             "metrics": {"forecasts": store().count("forecasts"),
                         "models": store().count("models"),
                         "winner": (result("tournament_h1") or {}).get("winner")}},
        ],
        "lake": inv,
        "storage": storage,
        "mapreduce_engine": mr_status,
        "data_quality": quality,
        "streaming": stream,
        "mapreduce": mr,
        "docstore": {"backend": st.backend, "healthy": st.healthy, "state": st.state,
                     "probe_count": st.probe_count, "detail": st.detail,
                     "collections": store().stats()},
        "latency_by_stage": {k: {"runs": len(v), "mean_ms": round(float(np.mean(v)), 1),
                                 "max_ms": round(float(np.max(v)), 1)}
                             for k, v in sorted(stage_latency.items())},
        "lineage": lineage(config()),
        "runs": Tracker(config()).runs(limit=8),
    }


@app.get("/api/graph")
def graph():
    g = result("graph")
    return g or artefact_missing("graph.json", "graph analytics")


# --------------------------------------------------------------------------- recommend / search
@app.get("/api/recommend")
def recommend_endpoint(risk: str = "BALANCED", sectors: Optional[str] = None,
                       max_vol: Optional[float] = None, horizon: int = 1, limit: int = 6):
    sector_list = [s.strip().upper() for s in sectors.split(",")] if sectors else None
    return recommend(risk, sector_list, max_vol, horizon, limit, config())


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 12):
    """Global search across every VIGIL object. Each hit routes to the concept's canonical home."""
    cfg, s = config(), store()
    term = q.strip().lower()
    hits: List[Dict[str, str]] = []

    def add(kind: str, label: str, route: str, detail: str = "") -> None:
        hits.append({"kind": kind, "label": label, "route": route, "detail": detail})

    for ins in cfg.instruments:
        if term in ins.symbol.lower() or term in ins.name.lower() or term in ins.sector.lower():
            add("INSTRUMENT", f"{ins.name} ({ins.symbol})", f"#forecast/{ins.symbol}", ins.sector)
    for c in s.find("cases"):
        hay = " ".join(str(c.get(k, "")) for k in ("case_id", "title", "sector", "trigger")).lower()
        if term in hay:
            add("CASE", f"{c.get('case_id')} — {c.get('title', '')}",
                f"#investigate/{c.get('case_id')}", str(c.get("opened_at", "")))
    for f in s.find("forecasts"):
        hay = " ".join(str(f.get(k, "")) for k in ("forecast_id", "symbol", "verdict")).lower()
        if term in hay:
            add("FORECAST", f"{f.get('symbol')} — {f.get('verdict')}",
                f"#decision/{f.get('symbol')}", str(f.get("as_of", "")))
    for e in (result("experiments_h1") or {}).get("experiments", []):
        hay = f"{e.get('experiment_id', '')} {e.get('research_question', '')}".lower()
        if term in hay:
            add("EXPERIMENT", e.get("experiment_id", "").replace("_", " "), "#lab",
                e.get("conclusion", {}).get("verdict", ""))
    for m in (result("tournament_h1") or {}).get("leaderboard", []):
        if term in str(m.get("model_id", "")).lower() or term in str(m.get("name", "")).lower():
            add("MODEL", f"{m.get('name')} — Brier {m.get('metrics', {}).get('brier')}", "#lab",
                m.get("family", ""))
    for p in s.find("patterns"):
        if term in str(p.get("pattern_id", "")).lower() or term in str(p.get("definition", "")).lower():
            add("PATTERN", str(p.get("pattern_id", "")).replace("_", " "), "#lab",
                f"{p.get('occurrences', 0)} occurrences")
    for node in observatory().get("architecture", []):
        hay = f"{node['id']} {node['title']} {node['what']} {node['syllabus']}".lower()
        if term in hay:
            add("ARCHITECTURE", node["title"], "#observatory", node["syllabus"])
    return {"query": q, "count": len(hits), "results": hits[:limit]}


# --------------------------------------------------------------------------- human vs vigil
class HumanVote(BaseModel):
    symbol: str
    horizon: int = 1
    call: str = Field(pattern="^(POSITIVE|NEGATIVE|NO ACTION)$")
    note: Optional[str] = Field(default=None, max_length=280)


@app.post("/api/human-vote")
def human_vote(vote: HumanVote):
    fc = store().find_one("forecasts", {"symbol": vote.symbol.upper(), "horizon": vote.horizon})
    if not fc:
        raise HTTPException(404, "no stored forecast for that symbol/horizon")
    doc = {"forecast_id": fc["forecast_id"], "symbol": fc["symbol"], "as_of": fc["as_of"],
           "human_call": vote.call, "vigil_verdict": fc["verdict"], "note": vote.note,
           "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "agreement": vote.call == fc["verdict"].replace(" BIAS", "")}
    store().replace("human_votes", {"forecast_id": fc["forecast_id"]}, doc)
    votes = store().find("human_votes")
    return {"recorded": doc, "total_votes": len(votes),
            "agreement_rate_pct": round(100 * float(np.mean([v["agreement"] for v in votes])), 1),
            "note": "Stored for comparison once the horizon resolves. Neither side is assumed correct."}


# --------------------------------------------------------------------------- assistant
class Question(BaseModel):
    question: str = Field(min_length=2, max_length=400)


@app.post("/api/assistant")
def assistant(payload: Question):
    results = {name: result(f"{name}.json") for name in
               ("tournament_h1", "experiments_h1", "backtest_h1", "regime", "streaming_stats",
                "feature_build", "mapreduce_stats", "calibration_h1", "failure_lab_h1")}
    resp = assistant_mod.answer(payload.question, config(), store(), results)
    sources = resp.get("grounded_in", [])
    resp["sources"] = sources
    # point the user at the canonical home of whatever the answer was grounded in
    routes = {"tournament_h1": "#lab", "experiments_h1": "#lab", "failure_lab_h1": "#lab",
              "backtest_h1": "#lab", "calibration_h1": "#lab", "regime": "#pulse",
              "streaming_stats": "#observatory", "mapreduce_stats": "#observatory",
              "feature_build": "#observatory", "cases": "#investigate", "forecasts": "#forecast",
              "decisions": "#decision"}
    def _route_for(src: str) -> Optional[str]:
        stem = src.split("/")[-1].replace(".json", "")
        return routes.get(stem)
    resp["route"] = next((r for r in map(_route_for, sources) if r), None)
    return resp


# --------------------------------------------------------------------------- exports
@app.get("/api/export/{kind}.{fmt}")
def export(kind: str, fmt: str, id: Optional[str] = None):
    fmt = fmt.lower()
    if fmt not in ("json", "csv", "pdf"):
        raise HTTPException(400, "format must be json, csv or pdf")
    if kind == "case":
        doc = store().find_one("cases", {"case_id": id or ""})
        if not doc:
            raise HTTPException(404, "case not found")
        payload, title = doc, f"VIGIL Case Report — {doc['case_id']}"
    elif kind == "forecast":
        doc = store().find_one("forecasts", {"forecast_id": id or ""})
        if not doc:
            raise HTTPException(404, "forecast not found")
        payload, title = doc, f"VIGIL Forecast Passport — {doc['forecast_id']}"
    elif kind == "experiments":
        payload, title = result("experiments_h1", {}), "VIGIL Research Results"
    elif kind == "data-quality":
        payload, title = result("data_quality", {}), "VIGIL Dataset Quality Report"
    else:
        raise HTTPException(404, "unknown export kind")

    if fmt == "json":
        return JSONResponse(payload, headers={
            "Content-Disposition": f'attachment; filename="vigil_{kind}_{id or "report"}.json"'})
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["key", "value"])
        for k, v in _flatten(payload).items():
            writer.writerow([k, v])
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="vigil_{kind}_{id or "report"}.csv"'})
    return Response(_pdf(title, payload), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="vigil_{kind}_{id or "report"}.pdf"'})


def _flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _pdf(title: str, payload: Dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 25 * mm
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, y, title[:80])
    y -= 7 * mm
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, f"VIGIL — Market Investigation Engine · generated "
                             f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} · research output, "
                             f"not investment advice")
    y -= 8 * mm
    c.setFont("Helvetica", 8.5)
    for k, v in list(_flatten(payload).items())[:300]:
        text = f"{k}: {v}"
        for chunk in [text[i:i + 110] for i in range(0, min(len(text), 440), 110)]:
            if y < 20 * mm:
                c.showPage()
                y = height - 20 * mm
                c.setFont("Helvetica", 8.5)
            c.drawString(20 * mm, y, chunk)
            y -= 4.4 * mm
    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------- static web app
WEB_DIR = REPO_ROOT / "apps" / "web"
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def landing():
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/app")
    def application():
        return FileResponse(str(WEB_DIR / "app.html"))
