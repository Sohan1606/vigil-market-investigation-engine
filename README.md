# VIGIL — Market Investigation Engine

> **Investigate before you believe.**
> A market *investigation* and *decision-support* system, not a trading bot and not a prediction oracle.

VIGIL ingests real NSE market data, rebuilds it through a ten-layer Big Data pipeline, forecasts
short-horizon direction with a model tournament, then spends most of its effort doing the thing
that dashboards never do: **challenging its own forecast** before it is allowed to become an
opinion. Its most common verdict is **NO ACTION**, and that is a feature.

University of Mumbai, Semester VII — Big Data Analytics.

---

## The honest headline

VIGIL's own measured conclusion, reported exactly as it came out of the pipeline:

> On 14 NSE equities over ~11 years, next-session directional edge is real but tiny
> (winner `random_forest`, ROC-AUC ≈ 0.52, Brier ≈ 0.2497 versus a 0.25 coin). **It does not
> survive 18 bps of round-trip cost.** The cost-aware portfolio backtest loses money:
> ADAPTIVE_GATE -8.928% annualised (−13.464% when execution is delayed one session) against
> BUY_AND_HOLD +13.022%. Of six research hypotheses, five are NOT SUPPORTED. The one supported
> result is that *abstaining* beats trading everything: on the research arm, forced trading
> returns −13.98 bps per trade while the cost-gated policy returns +2.02 bps per trade
> (+0.401 bps per evaluated opportunity, abstentions counted as zero) — and that is a
> single-model policy study, not a profitable strategy.

A project that reported a winning strategy here would be lying. VIGIL reports the loss and then
builds the decision machinery that such a finding actually justifies.

**How decision quality is reported.** There is no single "good decision rate" — the v0.9 metric
that counted every abstention as a good decision (99.18% while acting on 4.08% of opportunities)
was withdrawn in v1.0. The published set is: forecast accuracy 51.32%, selective accuracy 53.2%
on the acted subset, action coverage 4.08%, abstention rate 95.92%, net return when acted
−20.77 bps, **decision utility −0.848 bps per evaluated opportunity** (abstaining contributes
exactly zero), downside avoidance +12.4 bps. Each metric is defined inside
`reports/results/decision_quality_h1.json`.

---

## The seven spaces

Each space owns exactly one question, and each concept has exactly one home. There is no
"Dashboard", no "Analytics", no "Settings".

| Space | Question it owns | What lives here (and nowhere else) |
|---|---|---|
| **PULSE** | What is happening? | market state, pressure, participation, attention, signal field, open cases |
| **INVESTIGATE** | Why is it happening? | Market Cases, Evidence Ledger, information conflict, similar historical states |
| **FORECAST** | What could happen next? | probability, interval, trajectory, model debate, technical evidence, Forecast Contract |
| **DECISION GATE** | Can it be trusted? | gate checks, uncertainty, calibration, drift, risk, costs, VIGIL Verdict, Forecast Stress, VIGIL Audit |
| **REPLAY** | Would we have known then? | point-in-time reconstruction, leak guard, outcome grading |
| **RESEARCH LAB** | Does it actually work? | model tournament, experiments, reliability, cost-aware backtest, decision quality, Failure Lab, Prediction Cemetery, pattern library |
| **DATA OBSERVATORY** | How is it engineered? | lake, MapReduce, Spark, Kafka/stream sketches, document store, lineage, latency, run registry |

---

## Run it locally (CPU, no cloud, no cluster)

The packaged release already contains the built lake, document store and result artefacts, so
**step 1 is enough to see the whole product**. Nothing asks for an API key, a database server or
an edit to the source.

**Windows (native PowerShell — no WSL, no Git Bash):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt                        # core deps only; no Spark, no Torch
Copy-Item .env.example .env                            # optional - every value has a default

# 1. Serve what is already in the package (no rebuild, no network)
uvicorn vigil.api.app:app --host 0.0.0.0 --port 8000
# landing page  http://localhost:8000/
# application   http://localhost:8000/app
```

**Linux / macOS:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt                        # core deps only; no Spark, no Torch
cp .env.example .env                                   # optional — every value has a default

uvicorn vigil.api.app:app --host 0.0.0.0 --port 8000
```

Everything below this point is identical on both platforms except where a shell script is
involved; those cases show the PowerShell form first.

Rebuild from source data only if you want to:

```bash
# 2. Rebuild everything. Default horizon is 1 — the safe default.
python scripts/run_pipeline.py --all

# Extended horizons are opt-in because they multiply every model stage:
python scripts/run_pipeline.py --all --horizons 1 3 5 20

# No network at all: replay the cached raw data in data/raw/
python scripts/run_pipeline.py --all --offline
```

Stage summaries accumulate: `--stage models --horizons 1` followed by `--stage models --horizons 5`
leaves both `models.horizon_1` and `models.horizon_5` in `reports/results/pipeline_summary.json`.

**Low-memory machines (< 4 GB):** run the heavy half one stage per process.

```powershell
# Windows (recommended on native Windows)
powershell -ExecutionPolicy Bypass -File .\scripts\rebuild_models.ps1
```

```bash
# Linux / macOS
bash scripts/rebuild_models.sh
```

Both run the same eleven stages (`models` → `report`) one OS process at a time with
`--horizons 1 --offline`, print each stage before it starts, and exit non-zero if any stage
failed. The PowerShell version resolves the repository root from its own location, so it can be
launched from anywhere.

**Optional extras** (never required; VIGIL reports which engine actually ran):

```bash
pip install -r requirements-extras.txt     # PySpark feature engine + PyTorch LSTM
```

**Docker** (verified path — builds the image, installs the core requirements, serves port 8000):

```bash
docker compose -f docker/docker-compose.yml up --build       # + optional: --profile mongo
```

**Tests, audits and the front-end smoke test:**

```bash
python -m pytest tests -q               # 124 tests: leakage, PIT, storage modes, Hadoop scripts, portability
npm install && npm run ui-smoke         # boots the API, renders every space in jsdom, stops it
python scripts/self_audit.py            # property-level self-audit → reports/SELF_AUDIT.md
python scripts/release_audit.py         # 17-condition release gate → reports/RELEASE_AUDIT.md
python scripts/storage_check.py         # which storage mode is actually active
python scripts/run_hadoop_job.py --status --validate
```

### Storage and MapReduce modes

| Variable | Default | Meaning |
|---|---|---|
| `STORAGE_MODE` | `local` | `local` = POSIX Parquet lake in `data/lake`; `hdfs` = genuine HDFS IO through pyarrow's Hadoop client (needs `HDFS_URI`, a Hadoop client and `CLASSPATH`). An unreachable cluster raises an error — VIGIL never downgrades silently. See [`docs/HDFS.md`](docs/HDFS.md). |
| `MAPREDUCE_ENGINE` | `local` | `local` = the in-repo engine (real splits, shuffle, reduce in separate OS processes); `hadoop` = Hadoop Streaming submission of `hadoop/*.py`. See [`hadoop/README.md`](hadoop/README.md). |
| `KAFKA_BOOTSTRAP_SERVERS` | unset | When set and reachable, the event tape is produced/consumed through Kafka; otherwise the deterministic in-process bus is used and labelled as such. |
| `MONGODB_URI` | unset | When set, the document store is a real MongoDB; otherwise the embedded mongita engine is used and labelled `EMBEDDED MONGITA (LOCAL)`. The embedded engine is single-writer, so VIGIL serialises access with a cross-platform advisory file lock (`fcntl` on POSIX, `msvcrt` on Windows) and reports `DEGRADED` — never `healthy` — if a probe read fails. |

In this release, the published artefacts were produced with `STORAGE_MODE=local` and
`MAPREDUCE_ENGINE=local`. The DATA OBSERVATORY shows `LOCAL DATA LAKE` and
`MapReduce Engine: LOCAL FALLBACK` accordingly — no surface claims HDFS or Hadoop is operational.

---

## Data mode, stated honestly

VIGIL serves **HISTORICAL / DETERMINISTIC REPLAY** mode by default, and the badge saying so is
permanently visible in the application shell. Market data is real (Yahoo Finance via `yfinance`);
headlines are real (Google News RSS) but cover only a recent window, so most historical sessions
have **no** news coverage — VIGIL marks those rows `MISSING` rather than imputing neutrality.

News coverage is published as **two distinct measurements**, never merged:

| Measurement | Value | Definition |
|---|---|---|
| **DIRECT SESSION COVERAGE** | **2.0933%** | (symbol, session) pairs with a headline published *on that session* — `reports/results/ingestion_summary.json` |
| **ROLLING 5-DAY CONTEXT ROWS** | **5.675%** | modelling rows with ≥1 headline anywhere in the trailing 5 sessions, which is what the news features actually see — `reports/results/dataset_meta.json` |

The 5-day aggregation is **timestamp-aware**, not causal: only headlines published at or before
the session close enter the window, which makes the feature computable in real time. It says
nothing about a headline *causing* a move.
Nothing in this repository fabricates prices, headlines, metrics, latencies or outcomes. Where a
component cannot run (no Kafka broker, no Spark, no Mongo), VIGIL uses a named local fallback and
reports it as a fallback in the DATA OBSERVATORY and in `/api/health`.

---

## Stack

Python 3.11+ · pandas/numpy · PyArrow Parquet lake (Hive-partitioned, HDFS-compatible layout) ·
hand-written MapReduce (multiprocessing) · PySpark 4 (optional, auto-falls back to pandas) ·
MongoDB via pymongo or mongita · Kafka via kafka-python or the local replay bus ·
scikit-learn · XGBoost · PyTorch (LSTM + temporal-attention benchmark) · NetworkX ·
FastAPI + Uvicorn · vanilla ES-module front end (no build step, no framework, no CDN).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the ten layers and why each technology is there
- [`docs/SCIENTIFIC_METHOD.md`](docs/SCIENTIFIC_METHOD.md) — leakage defences, validation protocol, how to read the results
- [`docs/API.md`](docs/API.md) — every endpoint
- [`docs/DESIGN.md`](docs/DESIGN.md) — the product model, the visual language, accessibility
- [`reports/FINAL_REPORT.md`](reports/FINAL_REPORT.md) — build status, what passed, what did not
- [`docs/HDFS.md`](docs/HDFS.md) — local vs HDFS storage modes, setup and validation
- [`hadoop/README.md`](hadoop/README.md) — the Hadoop Streaming jobs and how to run them
- [`reports/SELF_AUDIT.md`](reports/SELF_AUDIT.md) — property-level audit with evidence
- [`reports/RELEASE_AUDIT.md`](reports/RELEASE_AUDIT.md) — the 17-condition release gate

## Disclaimer

VIGIL is an academic research and decision-support system. It is **not** investment advice, not a
trading system, and it makes no claim of guaranteed returns. Every probability it shows is a
calibrated estimate with measured error.
