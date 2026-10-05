# VIGIL architecture — ten layers

Every technology below earns its place by solving a problem VIGIL actually has. Where a
distributed component cannot run on a single CPU machine, VIGIL implements the *same semantics*
locally and says so — it never pretends a fallback is the real thing.

```
 1 SOURCES ──▶ 2 INGESTION ──▶ 3 STORAGE ──▶ 4 BATCH (MapReduce+Spark) ──▶ 5 STREAM
                                   │                                          │
                                   ▼                                          ▼
 6 INTELLIGENCE (regime/anomaly/graph) ──▶ 7 MODELS ──▶ 8 RELIABILITY ──▶ 9 DECISION ──▶ 10 EXPERIENCE
```

## 1 — Sources
Real NSE OHLCV for 14 equities plus `^NSEI` and `^NSEBANK` (Yahoo Finance, `yfinance`), and real
headlines from Google News RSS with their publication timestamps. Headline coverage is a recent
window only; this limitation is carried into every downstream metric instead of being hidden.

## 2 — Ingestion & data quality
`vigil/ingestion/` fetches, normalises and **contracts** every row. Each row is classified
VALID / SUSPICIOUS → quarantine / INVALID → reject against explicit checks (non-positive prices,
OHLC ordering, impossible returns, zero volume, duplicate sessions, calendar gaps). The data
quality score that the Decision Gate consumes is computed here, per symbol, and nothing else is
allowed to invent one.

## 3 — Storage
**Parquet data lake** (`data/lake/`) in Hive-partitioned zones `raw / cleaned / features`,
partitioned by `symbol` and `year` — byte-for-byte the layout you would put on HDFS, which is why
`HDFS_URI` is a one-line switch. Timestamps are written as `datetime64[us]` because Spark cannot
read nanosecond Parquet.
**Document store** for semi-structured records that have no fixed schema: events, patterns, cases,
news, model registry, forecasts, decisions, audits, cemetery, experiments, human votes. Uses
MongoDB when `MONGODB_URI` is set, otherwise `mongita` — the same pymongo API, local files.

## 4 — Batch analytics
**MapReduce** (`vigil/mapreduce/jobs.py`): genuine map → shuffle/sort → reduce over Parquet
splits with a process pool and real counters (input splits, map in/out, shuffle keys, reduce out,
ms). Jobs: per-symbol-year volatility, volume profile, gap statistics, sector co-movement.
**Spark** (`vigil/spark_jobs/features_job.py`): the 38-feature matrix is built with Spark window
functions when a JVM is present, and with an identical pandas implementation when it is not. Both
paths produce the same schema and `feature_version`, so results are comparable either way.

## 5 — Streaming
Events are replayed through an `EventBus`: Kafka when `KAFKA_BOOTSTRAP_SERVERS` is set, otherwise
a local ordered replay bus with the same `produce/consume` contract. On the stream VIGIL runs real
sketches, each reported against an exact count so its error is *measured*, not asserted:
- **Bloom filter** (2²⁰ bits, 7 hashes) for event de-duplication — sized to the stream after a
  smaller filter produced a 32% false-positive dedup rate.
- **Flajolet–Martin** (PCSA, 1024 registers) for distinct event keys (~33k) — deliberately *not*
  demonstrated on the 14-symbol cardinality, where sketch error is meaningless.
- **DGIM** for 1-bits in a 2048-event sliding window, in O(log² N) buckets.
- **Reservoir sampling** for a uniform stream sample used by drift monitoring.
- **Exponentially decaying counters** for the attention scores shown in PULSE.
Plus Welford streaming statistics for online z-scores and anomaly flags.

## 6 — Intelligence
Regime detection (BULL / BEAR / SIDEWAYS / HIGH_VOLATILITY / TRANSITION) from volatility,
trend and breadth statistics with explicit probabilities; anomaly detection from streaming
z-scores; a **correlation graph** with community detection, per-component eigenvector centrality
(computed per connected component — `eigenvector_centrality_numpy` raises on disconnected graphs)
and contagion-style propagation paths. Market Cases are generated from anomalies plus graph
context and stored with their Evidence Ledger.

## 7 — Models
A single walk-forward harness (`vigil/models/walkforward.py`) runs every family on identical
folds, identical features and one seed: majority/momentum **baselines**, logistic regression,
random forest, XGBoost, **LSTM** and a compact **temporal-attention** transformer as the modern
benchmark. 14 folds, ~23.3k out-of-sample rows per horizon. An adaptive **ensemble** weights
members by recent measured skill and exposes member disagreement, which the Decision Gate uses.

## 8 — Reliability
Isotonic/Platt **calibration** fit strictly on past folds; **conformal** prediction intervals with
measured empirical coverage; ensemble **uncertainty** decomposed into sharpness deficit, member
spread and disagreement; **drift** monitoring with PSI per feature plus a measured performance
decay rule — *feature drift alone is a warning; drift plus measured decay is DEGRADED.*

## 9 — Decision
Cost model (commission + slippage + impact, charged on a round trip), position risk (β, vol,
VaR95), the **Decision Gate** (weighted checks, any one of which can force NO ACTION), cost-aware
**backtesting** of four strategies, decision-quality accounting that separates forecast accuracy
from decision quality from outcome quality, **Forecast Stress** (re-running the gate under
perturbed inputs) and the **VIGIL Audit** (an automated integrity check of a single forecast).

## 10 — Experience
FastAPI serves JSON plus a static, build-free ES-module front end. Server-side aggregation only —
the browser never receives a raw row-level dataset. Caching is mtime-based per artefact, and the
expensive point-in-time refit is shared through one `ForecastService` instance.
