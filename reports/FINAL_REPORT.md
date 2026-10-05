# VIGIL — FINAL REPORT

**Project:** VIGIL — Market Investigation Engine ("Investigate before you believe")
**Course:** University of Mumbai, Semester VII — Big Data Analytics
**Version:** **1.0.1** (Windows + portability hardening of 1.0.0, which hardened 0.9.0) · **Report date:** 2026-10-05
**Data mode:** HISTORICAL / DETERMINISTIC REPLAY over real market data (never presented as live)
**Storage mode:** LOCAL DATA LAKE · **MapReduce engine:** LOCAL FALLBACK (both labelled as such everywhere)

---

## 1. What VIGIL is

A market *investigation* and decision-support engine built on a ten-layer Big Data pipeline. It
observes the market, opens investigation cases, forecasts short-horizon direction, then
challenges its own forecast through a Decision Gate before issuing one of three verdicts:
POSITIVE BIAS / NEGATIVE BIAS / **NO ACTION**. It is not a trading bot and makes no
guaranteed-return claims. v1.0 changes nothing about the product: same identity, same seven
canonical spaces plus WORKSPACE, same visual system. It changes what VIGIL is allowed to *claim*.

## 2. Inspection summary (how v1.0 was produced)

The 0.9.0 tree was audited file by file against one question: *does the artefact support the
sentence printed next to it?* The inspection covered the storage layer, the MapReduce engine, the
feature and decision path, the research arms, the pipeline runner, the audit scripts, the API, the
front end and every document. Seventeen defects were confirmed with evidence and fixed **in
place** — no rewrite, no second repository, no regenerated dataset. Nothing was tuned, no
experiment was deleted, no losing model was hidden, and no finding moved in VIGIL's favour.

Two categories of defect dominated:

* **Claims the artefacts did not support** — "HDFS operational" while writing to POSIX, a local
  engine presented as Hadoop, a 99.18% "good decision rate" that rewarded doing nothing (now withdrawn), "causal"
  news aggregates, implied browser verification, and audits that reported file existence as PASS.
* **Methodology defects that affected the numbers** — a pre-trade edge computed from the forward
  return (leakage in the abstention gate), an execution convention that code and documentation
  disagreed about, a summary merge that silently discarded nested stages, fingerprints computed
  from file names and sizes, and a duplicated news lake that inflated a MapReduce counter.

## 3. The seventeen fixes — BEFORE → AFTER

| # | Defect | BEFORE (0.9.0) | AFTER (1.0.0) | Why it mattered |
|---|---|---|---|---|
| 1 | HDFS was a claim, not a path | Parquet always written to `data/lake`; surfaces said "HDFS-style / operational" | `STORAGE_MODE=local\|hdfs`; `HdfsBackend` performs genuine IO through `pyarrow.fs.HadoopFileSystem`; an unreachable NameNode **raises** `StorageUnavailable` with a remediation checklist and never downgrades silently; `scripts/storage_check.py` does connect → mkdir → write → read-back → delete; the UI shows `LOCAL DATA LAKE` or `HDFS DATA LAKE` | A label that cannot be false carries no information |
| 2 | "MapReduce" implied Hadoop | in-repo multiprocessing engine only | `hadoop/` ships real streaming mappers, combiners and reducers plus `scripts/run_hadoop_job.py`; `MAPREDUCE_ENGINE=local\|hadoop`; a genuine `hadoop jar $HADOOP_STREAMING_JAR -input … -mapper …` submission is constructed; with no client present the suite **refuses to run** (`RuntimeError: will not fabricate a Hadoop run`) and the scripts are validated statically instead; the Observatory shows `MapReduce Engine: LOCAL FALLBACK` | Fabricating a cluster run is the worst thing a Big Data report can do |
| 3 | `good_decision_rate_pct` = 99.18% | every abstention below threshold counted as a "good decision", so never acting scored ~100% | metric deleted; the published set is forecast accuracy 51.32%, selective accuracy 53.2%, action coverage 4.08%, abstention rate 95.92%, net when acted -20.77 bps, **decision utility -0.848 bps**, downside avoidance 12.4 bps and outcome quality — each defined inside the artefact | A metric that rewards inaction measures nothing |
| 4 | Future information before the trade | the abstention gate's expected move was `\|fwd_ret\|` — the answer it was deciding about | edge = `2·\|p−0.5\|·vol_20/√252`, entirely pre-trade; `fwd_ret` survives only in evaluation; `tests/test_leakage.py` scans every assignment in the feature/gating/signal path and a **negative control** proves the scanner fails when a leak is reintroduced | The leak inflated precisely the one hypothesis that was SUPPORTED |
| 5 | Execution semantics disagreed | docs said "next session close", code used `fwd_ret_1[t]`; costs described inconsistently | one convention, stated once: signal at the **close of session t**, entry at that close, exit at the close of t+h, realised return `fwd_ret_1[t] = close[t+1] / close[t] - 1`, full round trip 18.0 bps; `ADAPTIVE_GATE_T1` publishes the one-session-delayed variant; `tests/test_backtest_semantics.py` checks code, artefact and docs agree | Ambiguous accounting makes a backtest unfalsifiable |
| 6 | Unsafe pipeline default | `--all` implied horizons 1/3/5/20 and was OOM-killed on small machines | default is **horizon 1**; extended horizons are explicit (`--horizons 1 3 5 20`); README and scripts match | A first run must not silently cost 4x the documented compute |
| 7 | Summary merge lost stages | a second `--stage models` overwrote the first | true recursive merge: `models.horizon_1` and `models.horizon_5` coexist; deterministic sorted output; intentional scalar overwrites are logged; `tests/test_pipeline_summary.py` | Stage-by-stage execution is the documented low-memory path |
| 8 | One flattering coverage number | "news coverage 5.579% of sessions" conflated two measurements | **DIRECT SESSION COVERAGE 2.0967%** and **ROLLING 5-DAY CONTEXT ROWS 5.579%** (1,878/33,661) reported separately in the README, this report, SCIENTIFIC_METHOD and the UI | The larger number described the feature window, not the data |
| 9 | "Causal" language | "causal 5-day news aggregates" | "timestamp-aware 5-day news aggregates"; observed / inferred / predicted / causally-established remain distinct in the UI | VIGIL measures association and has established no causation |
| 10 | Fingerprints hashed names and sizes | a same-length edit produced an identical fingerprint | BLAKE2b-64 over file **content**; manifests sorted by repo-relative path, deterministic across machines; `tests/test_fingerprints.py` includes the same-length-edit case | Lineage that cannot detect a change is decoration |
| 11 | Self-audit checked file existence | 24/24 PASS, infrastructure included | properties are recomputed (fold ordering, metric identities, counter agreement, fingerprint sensitivity, cost consistency); states are PASS / PARTIAL / FAIL / **NOT EXECUTED**; absent infrastructure can never become PASS | An audit that cannot fail is not an audit |
| 12 | UI smoke was manual | "needs `npm install jsdom` and a running server" | `package.json` + `package-lock.json`; **`npm run ui-smoke`** boots the API, renders the landing page, shell, seven spaces, workspace, replay, lab tabs, drawer, search and assistant in jsdom, then stops the server; zero console errors and zero degraded panels required | Reproducibility is part of the claim |
| 13 | "Verified in a browser" | asserted, never performed | jsdom coverage extended to keyboard focus, ARIA, tab order, reduced-motion / breakpoint / focus-visible rules, and empty-state rendering; real-browser verification is reported **NOT EXECUTED** (no browser engine here) | A pixel claim needs pixels |
| 14 | Docs quoted stale numbers | 0.9 figures, withdrawn metrics, obsolete commands | every metric in the README, this report and SCIENTIFIC_METHOD is regenerated from the artefacts, and `release_audit.py` condition 11 fails the build when a documented number, command or withdrawn metric does not match reality | Documentation drift is a correctness bug |
| 15 | First-run experience | required a rebuild, a copied `.env` and exact flags | the package serves immediately with `uvicorn vigil.api.app:app`; `.env` optional; verified in a **clean virtualenv built from `requirements.txt` alone**; Docker path verified; offline rebuild documented; the missing `httpx` test dependency added | "Works on the author's machine" is not a release |
| 16 | Observatory inferred modes from files | an artefact's existence implied its infrastructure ran | `/api/observatory.infrastructure_modes` is **probed at request time** for storage, MapReduce, event bus and document store; the front end renders each real label and flags a requested-but-inactive mode | The Observatory is the one place that must never flatter |
| 17 | No final gate | — | **`python scripts/release_audit.py`** verifies all seventeen conditions and writes `reports/RELEASE_AUDIT.md` + `reports/results/release_audit.json`; exit code 0 only when nothing FAILs | One command that is allowed to say "no" |

### What the fixes did to the numbers

Only defect #4, and the news deduplication found while repairing the document store, changed any
published figure. Both are reported in full, including the parts that look worse:

| Figure | BEFORE | AFTER | Cause |
|---|---|---|---|
| Arm F, trade-everything net | −13.8 bps/trade | **-13.98 bps/trade** | leak removed from the edge |
| Arm F, gated net | −2.94 bps/trade | **2.02 bps/trade** | the gate is now a genuine pre-trade filter |
| Arm F, trades taken | 7 482 | **4,632** | the honest edge fires less often |
| Arm F, abstention | 67.93% | **80.15%** | same |
| Arm F, per *opportunity* (new) | not reported | **0.401 bps** | abstentions now count as zero, not as wins |
| `news_inverted_index` MapReduce | 5 508 in → 1 975 out | **1 377 in → 735 out** | the curated news lake held four duplicate copies of 1 377 headlines |
| Decision-quality headline | "99.18% good decisions" | **decision utility -0.848 bps** | metric withdrawn and replaced |

The two OHLCV MapReduce jobs are bit-identical before and after (`symbol_year_profile`
37 535→160, `market_breadth` 37 535→2 406) — a clean regression signal that only the news job
changed. Tournament, calibration, conformal, drift, backtest and Failure Lab numbers are
unchanged, because no model was retrained and no feature matrix was rebuilt.

## 3b. v1.0.1 — the Windows and release-truthfulness pass

v1.0.1 changes no model, no metric and no product surface. It fixes four defects that would have
broken a native-Windows run or shipped an untrue claim, plus the audit blind spot that let one of
them through.

| # | Defect found in 1.0.0 | BEFORE | AFTER | Evidence |
|---|---|---|---|---|
| W1 | **`import fcntl` at module scope** in `vigil/storage/docstore.py` — POSIX-only, so `import vigil.api.app` raised `ModuleNotFoundError` on native Windows. The documented Windows path did not exist. | the v1.0.0 write lock imported `fcntl` directly | new `vigil/storage/filelock.py`: one interface, `fcntl` on POSIX, `msvcrt.locking` on Windows, documented no-op elsewhere; standard library only; thread-re-entrant, so a read issued inside a write cannot deadlock the process against itself; released on normal exit **and** on exception; reads now take a shared lock so they cannot observe a half-rewritten collection | `tests/test_portability.py` — AST scan for POSIX-only imports across the package, an import test with `fcntl` hidden (simulated Windows), a subprocess import with `STORAGE_MODE=local`, exception-release, re-entrancy, and a two-process ordering test proving the second process waits |
| W2 | **Document-store health could lie.** `count()` returns `-1` when a read fails; `status()` only checked that no exception escaped, so `/api/health` could answer `healthy: true` for an unreadable store. | `healthy = True` whenever nothing was raised | health follows the probe: `probe_count >= 0` → `OK`, `-1` → `DEGRADED`, with the probe count and reason exposed on `/api/health`, `/api/observatory` and the Observatory panel | regression tests force a `-1` probe and assert `healthy is False`, `state == "DEGRADED"`, and that `/api/health` stops reporting `HEALTHY` |
| W3 | **Shipped artefacts carried the build machine's path.** All 13 `data/lake/**/_vigil_manifest.json` files held `/home/<user>/vigil/...`. | `"uri": "/home/<user>/vigil/data/lake/curated/ohlcv"` | `LocalBackend.uri()` returns the repo-relative POSIX form (`data/lake/curated/ohlcv`); HDFS mode still returns a real `hdfs://host:port/...` URI; the 13 shipped manifests were normalised in place — metadata only, no Parquet rewritten, no model retrained | tests assert every shipped manifest and every freshly written manifest is portable, and that the HDFS URI stays explicit |
| W4 | **The audit could not have caught W3** — `data/` was in `SKIP_DIRS`. | whole directory excluded from the scan | `data/` is scanned; binary datasets are skipped by suffix (`.parquet`, `.bson`, images, archives …) and generated JSON/text metadata is inspected; the release gate additionally validates every manifest URI against its storage mode | release-audit condition 13 reports how many files and manifests it scanned |
| W6 | **Two document-store clients in one process gave an incoherent view.** Opening a second `MongitaClientDisk` on a directory another client already held made collections read back as empty, so `/api/recommend` answered "no forecasts available" while an audit held the store open — and a concurrent open could empty the on-disk collection (it did, twice, during this work; the 14 forecast documents were restored from the v1.0.0 package, not regenerated). | a new client per `DocumentStore` instance | one cached client per directory per process, and the client *open* (which rewrites mongita's `$.metadata`) now happens under the exclusive advisory lock | regression tests assert two stores share a client and that three concurrent reader processes leave a seeded collection intact; the previously failing `self_audit` → `npm run ui-smoke` sequence now passes with the store open |
| W5 | **No native-Windows low-memory path.** | `scripts/rebuild_models.sh` (bash only) | `scripts/rebuild_models.ps1` — the same eleven stages, one OS process per stage, `--horizons 1 --offline`, resolves its own repository root, prints each stage, preserves exit codes, with `-StopOnError` / `-Horizons` / `-AllowNetwork`; no bash, no WSL, no Git Bash | a test checks it covers the same stages as the shell script and depends on no shell; the README documents both platforms |

Windows compatibility is verified by **static import analysis plus a clean subprocess import** on
this POSIX machine. **No Windows machine was available here**, so this is an import-surface
guarantee, not an executed Windows run.

## 4. Measured results (real, reproducible, not curated)

### Dataset
33,661 rows · 38 features (`fv-1.3.0`) · 14 symbols ·
2017-01-02 → 2026-10-01 · data-quality score 98.61/100
(1,072 rows quarantined, 0 rejected, of 38,607 ingested).
News: 1,377 real headlines · **DIRECT SESSION COVERAGE 2.0967%** ·
**ROLLING 5-DAY CONTEXT ROWS 5.579%** (1,878 rows).

### Model tournament (h=1, walk-forward rolling origin, 14 folds, 23,331 out-of-sample rows, purge 2 sessions)

| Model | Brier ↓ | ROC-AUC ↑ | Accuracy | ECE |
|---|---|---|---|---|
| Momentum Baseline | 0.25124 | 0.4918 | 49.46% | 0.0284 |
| Logistic Regression | 0.25112 | 0.5096 | 50.53% | 0.0246 |
| Random Forest | 0.24973 | 0.5208 | 51.74% | 0.0056 |
| XGBoost | 0.25364 | 0.5150 | 50.86% | 0.0512 |
| LSTM (20-session) | 0.25695 | 0.5130 | 51.02% | 0.0683 |
| Temporal Attention (TST-lite) | 0.25225 | 0.5070 | 50.42% | 0.0396 |

Winner: **random_forest**. Adaptive ensemble Brier **0.25013** — *worse than its
best member*; reported, not hidden.

### Reliability
- Calibration: ECE 0.01740 → 0.01216, Brier 0.25013 → 0.25036 → **calibration did NOT improve probability quality on this dataset**.
- Conformal 80% intervals: empirical coverage **80.24%**, mean width 4.03%.
- Drift: unhealthy models **['lstm']**; worst PSI close_to_low_20 = 0.376.

### Economics — cost-aware backtest (net of 18.0 bps round trip, 2020-01-02 → 2026-09-30)

Convention: entry at the **close of session t**, exit at the close of t+1, full round trip per position.

| Strategy | Annualised | Sharpe | Max DD | Hit rate | Abstention |
|---|---|---|---|---|---|
| Buy And Hold | 13.022% | 0.371 | -37.207% | 53.96% | 0.0% |
| Always Long Signal | -27.907% | -2.848 | -89.359% | 39.63% | 0.0% |
| Model | -16.229% | -1.345 | -75.629% | 21.52% | 53.6% |
| Adaptive Gate | -7.0% | -0.934 | -54.961% | 8.81% | 82.13% |
| Adaptive Gate T1 | -13.464% | -1.27 | -65.729% | 7.79% | 82.13% |

**Headline honest finding: the daily directional edge does not survive transaction costs.**
`ADAPTIVE_GATE_T1` is the same policy executed one session late — the honest sensitivity to
implementation lag.

### Decision quality (h=1, 23,331 evaluated opportunities)

| Metric | Value |
|---|---|
| Forecast accuracy (all opportunities) | 51.32% |
| Selective accuracy (acted subset, n=953) | 53.2% |
| Action coverage | 4.08% |
| Abstention rate | 95.92% |
| Mean net return when acted | -20.77 bps |
| **Decision utility (per opportunity)** | **-0.848 bps** |
| Downside avoidance | 12.4 bps |
| Outcome quality (acted) | net win rate 49.42%, median -2.77 bps |

Abstaining contributes exactly zero to decision utility. On this data the gate is a loss-reducer,
not a profit-maker: it avoids 12.4 bps of downside and still returns
-0.848 bps per evaluated opportunity.

### Research Lab experiments

| Experiment | Verdict | Metrics |
|---|---|---|
| A PRICE ONLY | **REFERENCE** | Brier 0.25340 / AUC 0.5083 |
| B PRICE CONTEXT | **NOT SUPPORTED** | Brier 0.25362 / AUC 0.5160 |
| C WITH NEWS | **NOT SUPPORTED** | Brier 0.25365 / AUC 0.5154 |
| D REGIME AWARE | **NOT SUPPORTED** | Brier 0.25366 / AUC 0.5184 |
| E DELAYED DATA | **NOT SUPPORTED** | Brier 0.25434 / AUC 0.5043 |
| F ABSTENTION POLICY | **SUPPORTED** | policy arm — 2.02 bps/trade gated vs -13.98 bps/trade unfiltered |

Five of six hypotheses are NOT SUPPORTED. AUC rises while Brier worsens from A to D — sharpness
bought at the cost of calibration. Arm F (abstention policy) remains SUPPORTED after the leak was
removed: trade-everything -13.98 bps/trade versus
gated 2.02 bps/trade
(0.401 bps per evaluated opportunity at
80.15% abstention).
**Caveat shipped inside the artefact:** arm F is a policy over a single XGBoost model, measured
per executed trade, with no model-agreement gate and no portfolio construction. The production
backtest still loses 7.0% a year.

### Failure Lab (h=1)
23,331 predictions, 11,358 misses (48.68%), 40 tombstones.

| Failure condition | Misses tagged |
|---|---|
| Missing Modality | 10424 |
| Insufficient Evidence | 10180 |
| Forecast Instability | 2138 |
| Volatility Shock | 1679 |
| Regime Shift | 802 |
| Model Overconfidence | 391 |
| Unexplained | 65 |

### Big Data layer, measured
- MapReduce (**LOCAL FALLBACK**): symbol_year_profile 37535→160 in 423 ms; market_breadth 37535→2406 in 459 ms; news_inverted_index 1377→735 in 69 ms
- Streaming: 33,661 events at 52,430 ev/s, p50 11.85 µs / p95 23.37 µs / p99 39.0 µs, 674 duplicates removed.
- Sketch error vs exact: Bloom fp rate 1.3e-05, Flajolet-Martin 6.69%, DGIM 2.04%.
- Feature engine: spark (PySpark 4.0.1 local executor), 33,661 rows in 23.6 s.

## 5. Testing executed, and the results

| Run | Command | Result |
|---|---|---|
| Full suite (repo) | `python -m pytest tests -q` | **126 passed, 0 failed** |
| Full suite (clean virtualenv, `requirements.txt` only) | `python -m pytest -q` | **124 passed, 0 failed** (run before the last two concurrency tests were added) |
| Byte-compile | `python -m compileall -q vigil scripts hadoop tests` | clean, no syntax errors |
| Storage mode | `python scripts/storage_check.py` | PASS (LOCAL DATA LAKE); with `STORAGE_MODE=hdfs` it exits 1 — an honest failure, never a silent fallback |
| Hadoop scripts | `python scripts/run_hadoop_job.py --status --validate` | every job **SCRIPTS VALIDATED LOCALLY — NOT A HADOOP RUN** |
| Front end | `npm run ui-smoke` | **PASS** — landing, shell, 7 spaces, workspace, replay, 6 lab tabs, drawer, search, assistant; 0 console errors, 0 degraded panels |
| Self-audit | `python scripts/self_audit.py` | **42 PASS · 0 PARTIAL · 0 FAIL · 4 NOT EXECUTED** |
| Release gate | `python scripts/release_audit.py` | **23 PASS · 0 FAIL · 2 NOT EXECUTED → RELEASE APPROVED** |
| Clean-environment install | fresh venv → `pip install -r requirements.txt` → extracted copy → pytest + `uvicorn` + `npm run ui-smoke` | **124 passed**; every probed endpoint HTTP 200; health HEALTHY with all 7 components OK; UI smoke passed |

| `test_portability.py` (20, new in 1.0.1) | no POSIX-only import outside the lock shim; the store imports with `fcntl` hidden; `vigil.api.app` imports in a clean subprocess; the lock releases on exception, is re-entrant and makes a second process wait; MongoDB mode skips the local lock; a `-1` probe is DEGRADED and `/api/health` says so; shipped and fresh lake manifests are portable; the audit scans `data/` metadata but no Parquet; the PowerShell rebuild script mirrors the shell one; two stores in one process share a client; concurrent reader processes cannot empty a collection |

Roughly half of the 126 tests are new or rewritten across v1.0.0 and v1.0.1:

| Suite | What it proves |
|---|---|
| `test_storage_modes.py` (10) | local default, invalid mode rejected, HDFS without a URI rejected, **no silent downgrade** when the cluster is absent, truthful status, URI construction, local round trip |
| `test_hadoop_streaming.py` (8) | each mapper/reducer obeys the streaming contract under `cat \| mapper \| sort \| reducer`, correct aggregation, stop-word handling, a well-formed `hadoop jar` command free of machine paths, and the **refusal to fabricate** a Hadoop run |
| `test_decision_metrics.py` (7) | the withdrawn metric stays withdrawn, every metric is defined, the utility identity holds, coverage + abstention = 100%, selective accuracy is measured on the acted subset, categories stay descriptive |
| `test_backtest_semantics.py` (7) | the label equals `close[t+1]/close[t]−1` per symbol, the published convention matches the configured costs, the delayed-execution variant exists, the docs state the same convention |
| `test_pipeline_summary.py` (7) | nested merge, depth recursion, logged overwrites, stage accumulation across invocations, idempotence, sorted output, corrupt-file recovery |
| `test_fingerprints.py` (8) | a same-length edit changes the fingerprint, renames preserve content digests, manifests are sorted and repo-relative, caches excluded |
| `test_leakage.py` (12, extended) | every 0.9 guarantee **plus** no `fwd_ret` in any pre-trade assignment, gating inputs known before the outcome, and a negative control that fails when a leak is reintroduced |

## 6. Scientific integrity

- Every negative finding from 0.9 survives unchanged: the ensemble still loses to its best member,
  calibration still worsens Brier, five of six hypotheses are still NOT SUPPORTED, the strategy
  still loses money after costs, and `tests/test_integrity.py` fails the build if any of that
  disappears.
- No model was retrained, no threshold was searched and no dataset was regenerated to improve a
  number. The one result that moved (arm F) moved because a leak was **removed**, and it ships
  with the caveat that it is not evidence of profitability.
- Metadata that had to be recomputed (the coverage figures) was recomputed **from the published
  feature matrix**, never by rebuilding features, precisely so the shipped models stay valid.
- Reproducibility is "to ~1e−4, not bit-exact": XGBoost thread scheduling moves arm D's Brier by
  about 2e−5 between identical runs. Stated rather than hidden.
- Data incident, disclosed: the 0.9 document store shipped with an empty `news` collection (the
  corruption predates packaging — the released ZIP has it too). It was rebuilt offline by
  replaying the untouched raw cache `data/raw/news/headlines.json`, giving 1,377
  headlines and reproducing the published coverage figures exactly. `human_votes` and `outcomes`
  remain empty because no UI votes and no realised outcomes exist; they were **not** backfilled.

## 7. Infrastructure status, stated plainly

**HDFS — NOT EXECUTED against a live NameNode.** `STORAGE_MODE=hdfs` is a real code path
(`pyarrow.fs.HadoopFileSystem`, `write_to_dataset(filesystem=…)`, identical Hive partitioning).
There is no Hadoop client, no JVM and no NameNode in this environment, so `scripts/storage_check.py`
with `STORAGE_MODE=hdfs` exits 1 with `Unable to load libjvm` — the correct, honest outcome. All
published artefacts were produced in `STORAGE_MODE=local` and every surface says
`LOCAL DATA LAKE`. Mode resolution, URI construction, the refusal to downgrade and status
truthfulness are covered by tests; the distributed IO itself is not claimed.

**Hadoop MapReduce — NOT EXECUTED on a cluster.** `hadoop/` contains genuine streaming scripts and
`scripts/run_hadoop_job.py` builds a real `hadoop jar $HADOOP_STREAMING_JAR -input … -mapper …
-combiner … -reducer …` submission. With no Hadoop client present, requesting
`MAPREDUCE_ENGINE=hadoop` raises instead of quietly running locally, and the scripts are validated
statically (compile, then `cat | mapper | sort | reducer`). All published counters come from the
local engine and are labelled **`MapReduce Engine: LOCAL FALLBACK`**.

**Kafka — NOT EXECUTED.** The deterministic in-process bus produced the sketch statistics; the
Observatory says `Event bus: IN-PROCESS EVENT BUS`.

**MongoDB — embedded mongita**, labelled `EMBEDDED MONGITA (LOCAL)`. A real server is used when
`MONGODB_URI` is set.

**Spark — executed.** The feature matrix was built by PySpark 4.0.1 local executor
(23.6 s); the pandas engine is the documented fallback.

## 8. UI verification and release audit status

**UI verification: PARTIAL.** `npm run ui-smoke` passes end to end in jsdom — DOM construction,
routing, all spaces and lab tabs, the architecture drawer, global search, the assistant, workspace
controls, keyboard focus, ARIA presence, empty states, and the presence of reduced-motion,
breakpoint and focus-visible rules in the stylesheet. jsdom performs no layout, no CSS cascade and
no paint. **No browser engine is available in this environment, so pixel-level and responsive
verification is reported NOT EXECUTED** — it is not claimed.

**Release audit: APPROVED.** `python scripts/release_audit.py` → **23 PASS ·
0 PARTIAL · 0 FAIL · 2 NOT EXECUTED**
(`reports/RELEASE_AUDIT.md`). The two NOT EXECUTED rows are Hadoop Streaming on a cluster and
HDFS IO against a NameNode; neither is counted as a pass and neither is claimed anywhere.

## 9. Known limitations (refreshed for v1.0.1 — obsolete entries removed)

1. **News coverage is thin, in two distinct senses.** Direct same-session coverage is
   2.0967%; rolling 5-day context reaches
   5.579% of modelling rows. Google News RSS serves only a recent
   window. Rows without news are marked MISSING, never imputed — which is also why the news
   experiment is NOT SUPPORTED.
2. **The HDFS and Hadoop code paths have never run against a cluster here** (§7); they are covered
   by contract tests only.
3. **Kafka semantics run on the in-process bus**; the sketches are identical either way.
4. **The document store is mongita** unless `MONGODB_URI` is set, and it is single-writer. v1.0.0
   added a cross-process advisory lock (`data/processed/docstore.writelock`) after a concurrent
   writer emptied two collections during that work; v1.0.1 made it cross-platform and extended it
   to reads. mongita still has no transactions: prefer MongoDB for concurrent use, and do not run
   the pipeline while serving.
5. **Windows support is verified at the import surface, not by running on Windows.** The
   POSIX-only import is gone, the lock has an `msvcrt` implementation, and a PowerShell rebuild
   script ships — but **no Windows machine was available in this environment**, so no Windows
   execution is claimed.
6. **Horizon 1 only in the shipped artefacts.** The code supports 1/3/5/20; extended horizons are
   an explicit opt-in costing roughly 7.5 minutes of walk-forward each.
7. **Low-memory machines need the staged rebuild** — `scripts/rebuild_models.ps1` on Windows,
   `scripts/rebuild_models.sh` on Linux/macOS (one process per heavy stage); `--all` in a single
   process was OOM-killed on the 2 GB build machine.
8. **No live intraday mode.** Everything is end-of-day and the UI badge says so permanently.
9. **The strategy loses money after costs.** That is the finding, not a bug.
10. **Arm F's positive per-trade number is not profitability** — single model, per executed trade,
   no portfolio construction.
11. **Reproducibility is to ~1e−4**, not bit-exact (XGBoost thread scheduling).
12. **The assistant is retrieval-only** (intent-matched over stored artefacts); it answers from
    measured values or refuses.
13. **`/api/forecast/{symbol}?live=true` and REPLAY retrain** (~25 s per point on CPU);
    `replay/timeline` is capped at 12 points.
14. **Human-vs-VIGIL votes are stored but unscored**, and the shipped store contains none; the
    comparison only becomes meaningful once a horizon has elapsed.
15. **Real-browser UI verification has not been performed** (§8).

## 10. How to run

Windows (native PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env            # optional - every value has a default
uvicorn vigil.api.app:app --host 0.0.0.0 --port 8000
powershell -ExecutionPolicy Bypass -File .\scripts\rebuild_models.ps1   # low-memory rebuild
```

Linux / macOS:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # optional — every value has a default

# serve what is already in the package — no rebuild, no network, no keys
uvicorn vigil.api.app:app --host 0.0.0.0 --port 8000
#   landing      http://localhost:8000/
#   application  http://localhost:8000/app

# rebuild (safe default = horizon 1)
python scripts/run_pipeline.py --all
python scripts/run_pipeline.py --all --horizons 1 3 5 20     # explicit opt-in
python scripts/run_pipeline.py --all --offline               # cached raw data, no network
bash scripts/rebuild_models.sh                               # low-memory path (PowerShell equivalent above)

python -m pytest tests -q          # 126 tests
npm install && npm run ui-smoke    # front-end smoke (jsdom)
python scripts/self_audit.py       # → reports/SELF_AUDIT.md
python scripts/release_audit.py    # → reports/RELEASE_AUDIT.md
python scripts/storage_check.py    # which storage mode is actually active
python scripts/run_hadoop_job.py --status --validate
```

**Docker:** `docker compose -f docker/docker-compose.yml up --build` (add `--profile mongo` for a
real MongoDB).

## 11. Environment variables (all optional)

`STORAGE_MODE`, `HDFS_URI`, `HDFS_USER`, `MAPREDUCE_ENGINE`, `HADOOP_STREAMING_JAR`, `VIGIL_MODE`,
`VIGIL_CONFIG`, `MONGODB_URI`, `KAFKA_BOOTSTRAP_SERVERS`, `JAVA_HOME`, `VIGIL_API_HOST`,
`VIGIL_API_PORT`, `VIGIL_CORS_ORIGINS`, `VIGIL_LOG_LEVEL`. With none of them set, VIGIL runs
entirely locally on CPU. No secrets are committed; `.env.example` holds placeholders only.

## 12. External requirements

Python 3.11+ (3.13 used here), ~2 GB RAM, ~500 MB disk. A JDK is needed **only** for the Spark
feature path and for the HDFS/Hadoop modes; without it the identical pandas engine and the local
lake run. Network access is needed only for a first ingestion — the package ships the built
artefacts.

## 13. Package contents

`vigil_v1.0.1_windows_hardened.zip` — **929 files** (1757 ZIP entries: 929 files + 828 directory
entries; directory entries are not files and are never reported as such). Excluded from the
archive: `node_modules/`, `.git/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`, `.env`
and the runtime `*.writelock`. Included: the full source, the built Parquet lake, the document
store, every result artefact, the reports, the tests, `package.json` + `package-lock.json`, and
both rebuild scripts. `unzip -t` reports no errors. The v1.0.0 archive
(`vigil_v1.0.0_hardened.zip`) and the original `vigil.zip` are left untouched.
