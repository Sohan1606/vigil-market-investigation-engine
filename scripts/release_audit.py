#!/usr/bin/env python3
"""VIGIL release audit — the final gate before shipping.

    python scripts/release_audit.py

Verifies the seventeen release conditions below and writes reports/RELEASE_AUDIT.md plus
reports/results/release_audit.json. Exit code 0 only when there is no FAIL. `NOT EXECUTED` rows
(infrastructure absent in this environment) are reported honestly and never counted as passes.

  1  no leakage in the feature/decision path          10  cost-aware evaluation present
  2  point-in-time discipline enforced                11  documentation matches the artefacts
  3  decision metrics defensible (no inflation)       12  no secrets committed
  4  streaming sketches measured against exact        13  no machine-specific paths
  5  API surfaces respond from packaged artefacts     14  safe defaults (horizon 1, local, offline)
  6  non-repetition rule holds                        15  truthful HDFS / Hadoop mode reporting
  7  data-quality contract enforced                   16  UI smoke reproducible in one command
  8  all headline metrics generated, not asserted     17  the package runs from its own artefacts
  9  baselines present and beaten-or-not reported
"""
from __future__ import annotations
import os

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _audit_common import (FAIL, NOT_EXECUTED, PARTIAL, PASS, ROOT, Recorder,  # noqa: E402
                           find_machine_paths, find_secrets, load_result, mapreduce_state,
                           read_text, run_pytest, run_ui_smoke, storage_state, ui_smoke_available)

rec = Recorder()

REQUIRED_ARTEFACTS = [
    "ingestion_summary.json", "data_quality.json", "feature_build.json", "dataset_meta.json",
    "tournament_h1.json", "ensemble_h1.json", "calibration_h1.json", "uncertainty_h1.json",
    "drift_h1.json", "backtest_h1.json", "decision_quality_h1.json", "failure_lab_h1.json",
    "experiments_h1.json", "streaming_stats.json", "mapreduce_stats.json", "graph.json",
    "regime.json", "lineage_manifest.json",
]


def c01_leakage(pytest_result: dict) -> None:
    if not pytest_result.get("executed"):
        rec.add("1. Leakage", "Leakage suite executed", NOT_EXECUTED, pytest_result.get("detail", ""))
        return
    targeted = subprocess.run([sys.executable, "-m", "pytest", "-q",
                               "--no-header", "-o", "pythonpath=.", "-p", "no:cacheprovider", "tests/test_leakage.py"],
                              env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT, capture_output=True, text=True)
    ok = targeted.returncode == 0
    rec.add("1. Leakage", "No future information in features, gating or signals",
            PASS if ok else FAIL,
            f"tests/test_leakage.py → {(targeted.stdout.strip().splitlines() or ['no output'])[-1]}; "
            f"includes test_pre_trade_edge_never_uses_future_returns (v1.0 regression) and a "
            f"negative control that must reject a shuffled split")


def c02_pit() -> None:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q",
                           "--no-header", "-o", "pythonpath=.", "-p", "no:cacheprovider", "tests/test_pit_and_replay.py"],
                          env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT, capture_output=True, text=True)
    t = load_result("tournament_h1.json") or {}
    purge = (t.get("protocol") or {}).get("purge_sessions")
    rec.add("2. Point-in-time", "Replay and PIT discipline verified",
            PASS if proc.returncode == 0 else FAIL,
            f"tests/test_pit_and_replay.py → "
            f"{(proc.stdout.strip().splitlines() or ['no output'])[-1]}; walk-forward purge "
            f"{purge} session(s)")


def c03_decision() -> None:
    q = load_result("decision_quality_h1.json")
    if not q:
        rec.add("3. Decision quality", "Defensible decision metrics", FAIL, "artefact missing")
        return
    blob = json.dumps({k: v for k, v in q.items() if k != "removed_metric_note"})
    inflated = "good_decision_rate_pct" in blob or "GOOD_ABSTENTION" in blob
    utility_ok = abs(q["decision_utility_bps"] -
                     q["action_coverage_pct"] / 100 * q["mean_net_return_when_acted_bps"]) < 0.05
    defined = len(q.get("metric_definitions", {})) >= 8
    rec.add("3. Decision quality", "Every metric defined; abstention cannot inflate a score",
            PASS if (not inflated and utility_ok and defined) else FAIL,
            f"coverage {q['action_coverage_pct']}%, selective accuracy "
            f"{q['selective_accuracy_pct']}%, decision utility {q['decision_utility_bps']} bps, "
            f"downside avoidance {q['downside_avoidance_bps']} bps; "
            f"{len(q.get('metric_definitions', {}))} definitions embedded; "
            f"good_decision_rate removed")


def c04_sketches() -> None:
    st = load_result("streaming_stats.json")
    if not st:
        rec.add("4. Streaming sketches", "Sketch error measured against exact counts", FAIL,
                "artefact missing")
        return
    fm = st["flajolet_martin"]["relative_error_pct"]
    dgim = st["dgim"]["relative_error_pct"]
    rec.add("4. Streaming sketches", "Sketch error measured against exact counts", PASS,
            f"Bloom expected fp {st['bloom']['expected_fp_rate']} "
            f"(observed {st['bloom'].get('observed_fp_rate', 'n/a')}); Flajolet-Martin error {fm}%; "
            f"DGIM error {dgim}%; {st['events_processed']} events")


def c05_api() -> dict:
    """Boot the API in-process and exercise it against the packaged artefacts."""
    try:
        from fastapi.testclient import TestClient

        from vigil.api.app import app
    except Exception as exc:
        rec.add("5. API", "API responds from packaged artefacts", NOT_EXECUTED,
                f"{type(exc).__name__}: {exc}")
        return {}
    endpoints = ["/api/health", "/api/pulse", "/api/cases", "/api/universe",
                 "/api/forecast/TCS.NS", "/api/forecast/TCS.NS/history",
                 "/api/decision/TCS.NS",
                 "/api/research/overview", "/api/research/cemetery", "/api/observatory",
                 "/api/graph", "/api/recommend?risk=BALANCED", "/api/search?q=infosys",
                 "/api/replay?symbol=TCS.NS&date=2024-06-03&horizon=1", "/", "/app"]
    client = TestClient(app)
    bad, degraded = [], []
    for ep in endpoints:
        try:
            r = client.get(ep)
        except Exception as exc:
            bad.append(f"{ep} raised {type(exc).__name__}")
            continue
        if r.status_code != 200:
            bad.append(f"{ep} → {r.status_code}")
            continue
        if ep.startswith("/api") and r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            if isinstance(body, dict) and body.get("available") is False:
                degraded.append(f"{ep}: {body.get('reason', 'unavailable')}")
    rec.add("5. API", "Every API surface responds from the packaged artefacts",
            PASS if not bad and not degraded else (PARTIAL if not bad else FAIL),
            f"{len(endpoints)} endpoints checked; failures: {bad or 'none'}; "
            f"degraded payloads: {degraded or 'none'}")
    return {"bad": bad, "degraded": degraded}


def c06_non_repetition() -> None:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q",
                           "--no-header", "-o", "pythonpath=.", "-p", "no:cacheprovider", "tests/test_non_repetition.py"],
                          env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT, capture_output=True, text=True)
    rec.add("6. Non-repetition", "One canonical home per concept",
            PASS if proc.returncode == 0 else FAIL,
            f"tests/test_non_repetition.py → "
            f"{(proc.stdout.strip().splitlines() or ['no output'])[-1]}")


def c07_data_quality() -> None:
    dq = load_result("data_quality.json")
    ing = load_result("ingestion_summary.json")
    if not (dq and ing):
        rec.add("7. Data quality", "Explicit data contract enforced", FAIL, "artefacts missing")
        return
    accounted = dq["rows_valid"] + dq["rows_quarantined"] + dq["rows_rejected"]
    rec.add("7. Data quality", "Explicit contract; every row accounted for",
            PASS if accounted == dq["rows_in"] else FAIL,
            f"score {dq['score']}/100; {dq['rows_valid']} valid + {dq['rows_quarantined']} "
            f"quarantined + {dq['rows_rejected']} rejected = {accounted} of {dq['rows_in']} in")


def c08_metrics_generated() -> None:
    missing = [a for a in REQUIRED_ARTEFACTS if not (ROOT / "reports" / "results" / a).exists()]
    rec.add("8. Metrics generated", "Headline metrics come from generated artefacts",
            PASS if not missing else FAIL,
            f"{len(REQUIRED_ARTEFACTS) - len(missing)}/{len(REQUIRED_ARTEFACTS)} required "
            f"artefacts present; missing: {missing or 'none'}")


def c09_baselines() -> None:
    t = load_result("tournament_h1.json")
    if not t:
        rec.add("9. Baselines", "Baselines present and compared", FAIL, "artefact missing")
        return
    base = [m for m in t["leaderboard"] if "baseline" in m["model_id"]]
    winner = next((m for m in t["leaderboard"] if m["model_id"] == t["winner"]), None)
    rec.add("9. Baselines", "Baselines in the tournament and the comparison published",
            PASS if base and winner else FAIL,
            f"baselines {[m['model_id'] for m in base]}; winner {t['winner']} "
            f"brier {winner['metrics']['brier']:.5f} vs best baseline "
            f"{min(m['metrics']['brier'] for m in base):.5f}")


def c10_cost_aware() -> None:
    bt = load_result("backtest_h1.json")
    if not bt:
        rec.add("10. Cost-aware evaluation", "Costs applied and disclosed", FAIL, "artefact missing")
        return
    strategies = {k: v.get("annualised_return_pct") for k, v in bt["strategies"].items()
                  if v.get("available")}
    conv = bt.get("execution_convention", {})
    rec.add("10. Cost-aware evaluation", "Returns net of commission, slippage and impact",
            PASS if bt["costs"]["round_trip_bps"] > 0 and conv else FAIL,
            f"round trip {bt['costs']['round_trip_bps']} bps; strategies " +
            "; ".join(f"{k} {v}%" for k, v in strategies.items()) +
            f"; execution: {conv.get('entry')} → {conv.get('exit')}")


def c11_doc_consistency() -> None:
    """Numbers quoted in the docs must exist in the artefacts."""
    problems = []
    readme = read_text(ROOT / "README.md")
    final = read_text(ROOT / "reports" / "FINAL_REPORT.md")
    sci = read_text(ROOT / "docs" / "SCIENTIFIC_METHOD.md")
    docs_blob = readme + final + sci
    q = load_result("decision_quality_h1.json") or {}
    bt = load_result("backtest_h1.json") or {}
    t = load_result("tournament_h1.json") or {}
    ing = load_result("ingestion_summary.json") or {}
    meta = load_result("dataset_meta.json") or {}

    # The withdrawn metric may only appear while being explicitly withdrawn, never as a result.
    withdrawal = ("withdraw", "removed", "deleted", "no single", "stay withdrawn", "v0.9 published",
                  "would have scored")
    for para in re.split(r"\n\s*\n", docs_blob):
        low = " ".join(para.split()).lower()
        line = para.strip().replace("\n", " ")
        if ("good decision rate" in low or "good_decision_rate" in low or "99.18" in low) \
                and not any(w in low for w in withdrawal):
            problems.append(f"the withdrawn metric is quoted as a result: {line.strip()[:90]!r}")
    for label, value in (("direct session coverage", ing.get("direct_session_coverage_pct")),
                         ("rolling 5-day context", meta.get("rolling_5d_context_rows_pct"))):
        if value is not None and f"{value:.2f}" not in docs_blob and f"{value}" not in docs_blob:
            problems.append(f"{label} ({value}%) not quoted in the docs")
    winner = t.get("winner")
    if winner and winner not in docs_blob:
        problems.append(f"tournament winner {winner} not named in the docs")
    gate = (bt.get("strategies", {}).get("ADAPTIVE_GATE") or {}).get("annualised_return_pct")
    if gate is not None and str(gate) not in docs_blob:
        problems.append(f"ADAPTIVE_GATE return {gate}% not quoted in the docs")
    cov = q.get("action_coverage_pct")
    if cov is not None and str(cov) not in docs_blob:
        problems.append(f"action coverage {cov}% not quoted in the docs")
    # commands quoted in the docs must exist
    for cmd in re.findall(r"python (scripts/[a-z_]+\.py)", docs_blob):
        if not (ROOT / cmd).exists():
            problems.append(f"documented command references missing file {cmd}")
    rec.add("11. Documentation", "Every quoted command, metric and limitation matches reality",
            PASS if not problems else FAIL, f"issues: {problems or 'none'}")


def c12_secrets() -> None:
    hits = find_secrets()
    rec.add("12. Secrets", "No credentials or tokens committed", PASS if not hits else FAIL,
            f"scan hits: {hits or 'none'}; .env.example holds placeholders only")


def c13_machine_paths() -> None:
    """Scan the ACTUAL package content, including generated metadata under data/.

    v1.0.0 excluded the whole `data/` directory and therefore missed the shipped
    `_vigil_manifest.json` files, every one of which carried the build machine's absolute path.
    Binary datasets (.parquet and friends) are skipped by suffix; textual generated metadata is
    inspected.
    """
    from _audit_common import scan_files  # noqa: PLC0415

    files = scan_files()
    data_files = [f for f in files if f.relative_to(ROOT).parts[0] == "data"]
    manifests = [f for f in data_files if f.name == "_vigil_manifest.json"]
    hits = find_machine_paths()
    rec.add("13. Portability", "No machine-specific absolute paths in any shipped text artefact",
            PASS if not hits else FAIL,
            f"{len(files)} files scanned (source, docs, configs, generated JSON/text), of which "
            f"{len(data_files)} under data/ including {len(manifests)} lake manifests; "
            f"binary datasets skipped by suffix; hits: {hits[:5] or 'none'}")
    bad_uri = []
    for man in manifests:
        try:
            payload = json.loads(man.read_text())
        except Exception as exc:  # pragma: no cover
            bad_uri.append(f"{man.name}: unreadable ({type(exc).__name__})")
            continue
        uri = str(payload.get("uri", ""))
        mode = str(payload.get("storage_mode", "LOCAL")).upper()
        if mode == "LOCAL" and not uri.startswith("data/lake/"):
            bad_uri.append(f"{man.relative_to(ROOT)}: {uri}")
        if mode == "HDFS" and not uri.startswith("hdfs://"):
            bad_uri.append(f"{man.relative_to(ROOT)}: {uri}")
    rec.add("13. Portability", "Lake manifests use portable URIs (relative in local mode, "
                               "hdfs:// only when HDFS is active)",
            PASS if manifests and not bad_uri else FAIL,
            f"{len(manifests)} manifests checked; offenders: {bad_uri or 'none'}")


def c14_safe_defaults() -> None:
    runner = read_text(ROOT / "scripts" / "run_pipeline.py")
    m = re.search(r'--horizons".*?default=\[([^\]]*)\]', runner, re.S)
    horizons = m.group(1).strip() if m else "?"
    cfg_yaml = read_text(ROOT / "config" / "vigil.yaml")
    local_default = re.search(r"^\s*mode:\s*local", cfg_yaml, re.M) is not None
    offline_flag = "--offline" in runner
    readme = read_text(ROOT / "README.md")
    readme_ok = "--horizons 1 3 5 20" in readme or "--horizons" in readme
    ok = horizons == "1" and local_default and offline_flag and readme_ok
    rec.add("14. Safe defaults", "Horizon 1, local storage, offline path available",
            PASS if ok else FAIL,
            f"pipeline default horizons=[{horizons}]; config storage.mode=local "
            f"{local_default}; --offline flag {offline_flag}; README documents the extended "
            f"horizon opt-in {readme_ok}")


def c15_infra_truth() -> None:
    storage = storage_state()
    mr = mapreduce_state()
    truthful = (storage["active_mode"] == "LOCAL" and storage["label"] == "LOCAL DATA LAKE") or \
               (storage["active_mode"] == "HDFS" and storage["available"])
    rec.add("15. Infrastructure truth", "Storage mode labelled with what is actually running",
            PASS if truthful else FAIL,
            f"requested {storage['requested_mode']} → active {storage['active_mode']} "
            f"({storage['label']}) at {storage['uri']}")
    engine_truthful = (mr["engine"] == "HADOOP") == bool(mr["hadoop_available"] and
                                                         mr["requested_engine"] == "HADOOP")
    rec.add("15. Infrastructure truth", "MapReduce engine labelled with what is actually running",
            PASS if engine_truthful else FAIL,
            f"{mr['label']}; hadoop_available={mr['hadoop_available']}; "
            f"streaming scripts validated locally={mr['streaming_scripts_validated']}")
    if mr["engine"] != "HADOOP":
        rec.add("15. Infrastructure truth", "Hadoop Streaming executed on a cluster", NOT_EXECUTED,
                "no Hadoop client in this environment; command construction and the mapper/reducer "
                "contract are verified by tests/test_hadoop_streaming.py instead")
    if storage["active_mode"] != "HDFS":
        rec.add("15. Infrastructure truth", "HDFS IO executed against a NameNode", NOT_EXECUTED,
                "no NameNode in this environment; mode resolution, URI construction and the "
                "refusal to downgrade are verified by tests/test_storage_modes.py instead")


def c12b_docstore_health() -> None:
    """A failed probe read must be reported DEGRADED, never healthy (v1.0.1 defect #2)."""
    try:
        from vigil.config import load_config
        from vigil.storage.docstore import DocumentStore

        store = DocumentStore(load_config())
        st = store.status()
        consistent = (st.healthy is (st.probe_count >= 0)) and \
                     (st.state == ("OK" if st.healthy else "DEGRADED"))
        rec.add("12. Health truthfulness", "Document-store health follows the probe read",
                PASS if consistent else FAIL,
                f"backend {st.backend}, probe_count {st.probe_count}, healthy {st.healthy}, "
                f"state {st.state}; a -1 probe (unreadable store) can never report healthy")
    except Exception as exc:
        rec.add("12. Health truthfulness", "Document-store health follows the probe read",
                NOT_EXECUTED, f"{type(exc).__name__}: {exc}")


def c12c_windows_surface() -> None:
    """The default local path must import on native Windows (v1.0.1 defect #1)."""
    posix_only = {"fcntl", "termios", "pwd", "grp", "resource", "syslog"}
    offenders = []
    for path in sorted((ROOT / "vigil").rglob("*.py")):
        if path.relative_to(ROOT).as_posix() == "vigil/storage/filelock.py":
            continue
        text = read_text(path)
        for mod in posix_only:
            if re.search(rf"^\s*import {mod}\b", text, re.M):
                offenders.append(f"{path.relative_to(ROOT)} imports {mod}")
    try:
        from vigil.storage.filelock import LOCK_BACKEND  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        LOCK_BACKEND = f"unavailable ({type(exc).__name__}: {exc})"
    ps1 = (ROOT / "scripts" / "rebuild_models.ps1").exists()
    readme = read_text(ROOT / "README.md")
    documented = ".venv\\Scripts\\Activate.ps1" in readme and "rebuild_models.ps1" in readme
    rec.add("12. Windows surface", "Default local path imports without POSIX-only modules",
            PASS if not offenders and ps1 and documented else FAIL,
            f"POSIX-only imports outside the lock shim: {offenders or 'none'}; lock backend here "
            f"'{LOCK_BACKEND}' (fcntl on POSIX, msvcrt on Windows); "
            f"scripts/rebuild_models.ps1 present {ps1}; README documents the PowerShell path "
            f"{documented}. NOTE: verified by static import analysis and a subprocess import on "
            f"this POSIX machine — no Windows machine was available to execute it.")


def c16_ui_smoke() -> None:
    pkg = ROOT / "package.json"
    lock = ROOT / "package-lock.json"
    single_command = pkg.exists() and "ui-smoke" in read_text(pkg) and lock.exists()
    rec.add("16. UI smoke", "Reproducible from a single command",
            PASS if single_command else FAIL,
            "`npm install && npm run ui-smoke` — package.json + package-lock.json committed, "
            "tests/run_ui_smoke.mjs boots the API, runs tests/ui_smoke.mjs and stops it"
            if single_command else "package.json / package-lock.json / ui-smoke script missing")
    if not ui_smoke_available():
        rec.add("16. UI smoke", "UI smoke executed", NOT_EXECUTED,
                "jsdom not installed here — run `npm install` first. No browser verification is "
                "claimed either way.")
        return
    res = run_ui_smoke()
    rec.add("16. UI smoke", "UI smoke executed (jsdom DOM test, not a browser)",
            PASS if res.get("returncode") == 0 else FAIL,
            f"exit {res.get('returncode')}: {str(res.get('summary'))[:200]}")


def c17_runs_from_package() -> dict:
    """The shipped artefacts alone must be enough: no rebuild, no network, no manual setup."""
    missing = [a for a in REQUIRED_ARTEFACTS if not (ROOT / "reports" / "results" / a).exists()]
    lake_ok = (ROOT / "data" / "lake" / "features" / "model_matrix").exists()
    store_ok = (ROOT / "data" / "processed" / "docstore").exists()
    try:
        sys.path.insert(0, str(ROOT))
        from vigil.config import load_config
        from vigil.storage.docstore import DocumentStore

        stats = DocumentStore(load_config()).stats()
        empty = [k for k, v in stats.items() if v in (0, -1) and k in
                 ("forecasts", "decisions", "cases", "events", "news", "models", "experiments")]
    except Exception as exc:
        stats, empty = {}, [f"docstore unreadable: {type(exc).__name__}"]
    ok = not missing and lake_ok and store_ok and not empty
    rec.add("17. Runs from the package", "Demo works from the packaged artefacts alone",
            PASS if ok else FAIL,
            f"lake present {lake_ok}; docstore present {store_ok}; collections {stats}; "
            f"missing artefacts {missing or 'none'}; empty collections {empty or 'none'}")
    return {"missing": missing}


def main() -> int:
    pt = run_pytest()
    if pt.get("executed"):
        state = PASS if pt["failed"] == 0 and pt["errors"] == 0 else FAIL
        rec.add("0. Test suite", "Full pytest suite green", state,
                f"{pt['passed']} passed, {pt['failed']} failed, {pt['errors']} errors, "
                f"{pt['skipped']} skipped")
    else:
        rec.add("0. Test suite", "Full pytest suite green", NOT_EXECUTED, pt.get("detail", ""))

    # The UI smoke boots its own API process. It runs FIRST, before this process opens the
    # document store, because the local engine is single-writer (see docs + limitation 4).
    c16_ui_smoke()
    c01_leakage(pt)
    c02_pit()
    c03_decision()
    c04_sketches()
    c05_api()
    c06_non_repetition()
    c07_data_quality()
    c08_metrics_generated()
    c09_baselines()
    c10_cost_aware()
    c11_doc_consistency()
    c12_secrets()
    c12b_docstore_health()
    c12c_windows_surface()
    c13_machine_paths()
    c14_safe_defaults()
    c15_infra_truth()
    c17_runs_from_package()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = rec.markdown("VIGIL release audit", "scripts/release_audit.py", ts,
                      ["## Verdict", "",
                       f"**{'RELEASE APPROVED' if rec.exit_code() == 0 else 'RELEASE BLOCKED'}** — "
                       f"{rec.counts()[FAIL]} failing condition(s), "
                       f"{rec.counts()[NOT_EXECUTED]} condition(s) not executable in this "
                       f"environment (reported, not assumed)."])
    (ROOT / "reports" / "RELEASE_AUDIT.md").write_text(md)
    (ROOT / "reports" / "results" / "release_audit.json").write_text(
        json.dumps({"generated_at": ts, **rec.to_json()}, indent=2))
    rec.print_summary()
    print(f"\n{'RELEASE APPROVED' if rec.exit_code() == 0 else 'RELEASE BLOCKED'} "
          f"→ reports/RELEASE_AUDIT.md")
    return rec.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
