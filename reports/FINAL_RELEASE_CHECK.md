# VIGIL v1.0.1 — final release check

_Generated for the Windows + portability hardening pass on 2026-10-05 14:16 UTC. Every line below is backed by a
command that was actually run in this environment; nothing here is asserted from inspection._

| Area | Result | Basis |
|---|---|---|
| **WINDOWS** | **PASS (import surface)** | No module in `vigil/` imports a POSIX-only module except `vigil/storage/filelock.py`, which guards `fcntl` behind `try/except ImportError` and falls back to `msvcrt.locking`. Verified three ways: an AST scan of every file in the package, an import of `vigil.storage.docstore` with `fcntl` hidden (simulated Windows), and a clean subprocess `import vigil.api.app` with `STORAGE_MODE=local` and no `MONGODB_URI`. `scripts/rebuild_models.ps1` ships and is checked against the shell script. **No Windows machine was available here, so no Windows execution is claimed.** |
| **PORTABILITY** | **PASS** | `scripts/release_audit.py` scans source, docs, configs and generated JSON/text — including the 13 `data/lake/**/_vigil_manifest.json` files, which v1.0.0 excluded — and finds no Linux home-directory path, macOS user path, Windows user-profile path or other build-machine path. Binary datasets are skipped by suffix, never scanned blindly. All 13 manifests now carry repo-relative URIs (13 distinct, all beginning `data/lake/`); HDFS mode still emits a real `hdfs://` URI. |
| **DOCSTORE** | **PASS** | Health follows an actual probe read: `probe_count >= 0` → `OK`, `-1` → `DEGRADED`. A forced `-1` probe is asserted to report `healthy=False`, and `/api/health` stops reporting `HEALTHY`. Access is serialised by a cross-platform advisory lock (exclusive for writes and for client open, shared for reads) and one mongita client is shared per directory per process. The full shipped store is intact: forecasts 14, decisions 14, cases 56, events 2589, news 1377, experiments 6, models 6, audits 1, patterns 6, cemetery 45 (outcomes and human_votes are genuinely empty). |
| **HDFS** | **NOT EXECUTED** | No Hadoop client, no JVM, no NameNode in this environment. `STORAGE_MODE=hdfs python scripts/storage_check.py` exits 1 with `Unable to load libjvm` — the honest failure, with no silent fallback. Mode resolution, URI construction, the refusal to downgrade and status truthfulness are covered by `tests/test_storage_modes.py`. |
| **HADOOP** | **NOT EXECUTED** | No Hadoop client. `python scripts/run_hadoop_job.py --status --validate` reports every job as `SCRIPTS VALIDATED LOCALLY — NOT A HADOOP RUN`; requesting `MAPREDUCE_ENGINE=hadoop` raises rather than running locally under a Hadoop label. All published counters are labelled `MapReduce Engine: LOCAL FALLBACK`. |
| **SCIENTIFIC INTEGRITY** | **PASS** | 44 targeted tests re-run green (`test_leakage` 12 incl. the negative control, `test_pit_and_replay`, `test_integrity`, `test_decision_metrics`, `test_backtest_semantics`, `test_non_repetition`). Re-verified from the artefacts: winner `random_forest` Brier 0.24973; purge 2 sessions, `random_split_used=false`; no `good_decision_rate_pct`; decision utility −0.848 bps; ADAPTIVE_GATE −7.0% and ADAPTIVE_GATE_T1 −13.464% at 18.0 bps round trip; verdicts still REFERENCE / NOT SUPPORTED ×4 / SUPPORTED. **No model was retrained and no metric changed in v1.0.1.** |
| **UI SMOKE** | **PASS (jsdom) / browser NOT EXECUTED** | `npm run ui-smoke` passes: landing, shell, seven spaces, workspace, replay, six lab tabs, drawer, search, assistant, keyboard focus, ARIA, empty states, and reduced-motion / breakpoint / focus-visible rules — 0 console errors, 0 degraded panels. jsdom does no layout, CSS cascade or paint, and no browser engine exists here. |
| **RELEASE AUDIT** | **PASS — RELEASE APPROVED** | `python scripts/release_audit.py` → 23 PASS · 0 PARTIAL · 0 FAIL · 2 NOT EXECUTED. Self-audit: 42 PASS · 0 PARTIAL · 0 FAIL · 4 NOT EXECUTED. |

## Commands executed for this check

```
python -m pytest tests -q                                  126 passed, 0 failed
python -m compileall -q vigil scripts hadoop tests         clean
npm install && npm run ui-smoke                            PASS (jsdom)
python scripts/self_audit.py                               42 PASS / 0 FAIL / 4 NOT EXECUTED
python scripts/release_audit.py                            23 PASS / 0 FAIL / 2 NOT EXECUTED → APPROVED
python scripts/storage_check.py                            PASS (LOCAL DATA LAKE)
STORAGE_MODE=hdfs python scripts/storage_check.py          exit 1 (Unable to load libjvm) — expected, honest
python scripts/run_hadoop_job.py --status --validate       all jobs: SCRIPTS VALIDATED LOCALLY — NOT A HADOOP RUN
clean virtualenv from requirements.txt + extracted copy    124 passed; API 200 on every probed endpoint; UI smoke PASS
```

## Checks NOT EXECUTED, and exactly why

These are reported as NOT EXECUTED and are never converted into PASS:

- **HDFS read/write executed against a live NameNode** — no Hadoop client/NameNode in this environment. The HDFS backend (vigil/storage/backend.py::HdfsBackend) and its refusal to downgrade are covered by tests/test_storage_modes.py; `python scripts/storage_check.py` validates a real cluster when one is reachable. Not claimed as operational.
- **Hadoop Streaming job executed on a cluster** — no usable Hadoop client here (Hadoop client not usable on this machine — hadoop binary on PATH/HADOOP_HOME, hadoop-streaming jar located, HDFS_URI configured). The streaming scripts in hadoop/ were compiled and executed locally through mapper|sort|reducer (validated=True) — that is a script validation, NOT a Hadoop run, and no Hadoop counters are claimed.
- **Kafka broker used for the event stream** — bus_mode=LOCAL_REPLAY — the deterministic in-process bus replayed the event tape. Kafka is wired (KAFKA_BOOTSTRAP_SERVERS) but no broker is present here, so no Kafka run is claimed.
- **Verified in a real browser engine** — no Playwright/Selenium browser available in this environment; the jsdom smoke test covers DOM construction, routing and data binding but executes no layout, CSS or paint. No browser verification is claimed.
- **Hadoop Streaming executed on a cluster** — no Hadoop client in this environment; command construction and the mapper/reducer contract are verified by tests/test_hadoop_streaming.py instead
- **HDFS IO executed against a NameNode** — no NameNode in this environment; mode resolution, URI construction and the refusal to downgrade are verified by tests/test_storage_modes.py instead

The three missing pieces of infrastructure are: **a Hadoop cluster**, **an HDFS NameNode** and
**a real browser engine**. A Kafka broker is also absent, so the event bus ran in-process. Nothing
in this release claims any of them was used.

## What did NOT change in v1.0.1

No model was retrained, no threshold was tuned, no dataset was regenerated and no product surface
was redesigned. The seven canonical experiences plus WORKSPACE are unchanged and each still owns
exactly one question (PULSE market state · INVESTIGATE evidence and cases · FORECAST the
prediction · DECISION GATE trust, risk, uncertainty, drift, cost, verdict · REPLAY historical
reconstruction · RESEARCH LAB experiments and failure analysis · DATA OBSERVATORY infrastructure);
`tests/test_non_repetition.py` still enforces that no concept has a second home. The only
artefacts rewritten were 13 lake manifests (the `uri` field only) and the regenerated audit
reports — no Parquet file and no model was touched.
