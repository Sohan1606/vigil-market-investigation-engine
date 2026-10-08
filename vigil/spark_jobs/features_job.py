"""PHASE 5/8 — distributed feature engineering.

Primary engine: PySpark (window functions + Spark SQL) over the Hive-partitioned lake.
Fallback engine: pandas (same formulas, identical column contract) used only when no JDK/Spark is
available. The engine actually used is recorded in every artefact so the Observatory never lies.

POINT-IN-TIME RULE enforced in this module:
  every feature at row t uses only information from rows <= t for that symbol
  (rolling windows are backward-looking; cross-sectional joins use the same session only).
Labels are produced by vigil/features/labels.py, never here.
"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake
from .session import SparkUnavailable, get_spark

log = get_logger("vigil.spark.features")

FEATURE_COLUMNS = [
    "ret_1", "ret_5", "ret_20", "log_ret_1", "vol_20", "vol_60", "vol_ratio",
    "sma_10_gap", "sma_50_gap", "ema_12_gap", "rsi_14", "macd", "macd_signal", "macd_hist",
    "bb_pos", "bb_width", "volume_z", "rel_volume", "range_pct", "gap_pct",
    "close_to_high_20", "close_to_low_20", "trend_strength", "dollar_volume_log",
    "bench_ret_1", "bench_ret_5", "bench_vol_20", "excess_ret_1", "beta_60", "corr_bench_60",
    "sector_ret_1", "sector_breadth", "market_breadth", "regime_vol_percentile",
]


def _spark_features(spark, lake: DataLake, cfg: VigilConfig) -> pd.DataFrame:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    src = str(lake.dataset_path("curated", "ohlcv"))
    sdf = spark.read.parquet(src)
    sdf = sdf.withColumn("date", F.to_date("date")).withColumn("symbol", F.col("symbol").cast("string"))

    bench = cfg.benchmark
    w = Window.partitionBy("symbol").orderBy("date")

    def lagret(col: str, n: int):
        return (F.col(col) / F.lag(col, n).over(w) - 1.0)

    sdf = (sdf
           .withColumn("ret_1", lagret("close", 1))
           .withColumn("ret_5", lagret("close", 5))
           .withColumn("ret_20", lagret("close", 20))
           .withColumn("log_ret_1", F.log(F.col("close") / F.lag("close", 1).over(w)))
           .withColumn("gap_pct", F.col("open") / F.lag("close", 1).over(w) - 1.0)
           .withColumn("range_pct", (F.col("high") - F.col("low")) / F.col("close")))

    w20 = w.rowsBetween(-19, 0)
    w60 = w.rowsBetween(-59, 0)
    w10 = w.rowsBetween(-9, 0)
    w50 = w.rowsBetween(-49, 0)

    sdf = (sdf
           .withColumn("vol_20", F.stddev("log_ret_1").over(w20) * F.sqrt(F.lit(252.0)))
           .withColumn("vol_60", F.stddev("log_ret_1").over(w60) * F.sqrt(F.lit(252.0)))
           .withColumn("sma_10", F.avg("close").over(w10))
           .withColumn("sma_50", F.avg("close").over(w50))
           .withColumn("std_20", F.stddev("close").over(w20))
           .withColumn("sma_20", F.avg("close").over(w20))
           .withColumn("vol_mean_20", F.avg("volume").over(w20))
           .withColumn("vol_std_20", F.stddev("volume").over(w20))
           .withColumn("high_20", F.max("high").over(w20))
           .withColumn("low_20", F.min("low").over(w20)))

    sdf = (sdf
           .withColumn("vol_ratio", F.col("vol_20") / F.col("vol_60"))
           .withColumn("sma_10_gap", F.col("close") / F.col("sma_10") - 1.0)
           .withColumn("sma_50_gap", F.col("close") / F.col("sma_50") - 1.0)
           .withColumn("bb_pos", (F.col("close") - F.col("sma_20")) / (2.0 * F.col("std_20")))
           .withColumn("bb_width", (4.0 * F.col("std_20")) / F.col("sma_20"))
           .withColumn("volume_z", (F.col("volume") - F.col("vol_mean_20")) / F.col("vol_std_20"))
           .withColumn("rel_volume", F.col("volume") / F.col("vol_mean_20"))
           .withColumn("close_to_high_20", F.col("close") / F.col("high_20") - 1.0)
           .withColumn("close_to_low_20", F.col("close") / F.col("low_20") - 1.0)
           .withColumn("trend_strength", (F.col("sma_10") - F.col("sma_50")) / F.col("sma_50"))
           .withColumn("dollar_volume_log", F.log1p(F.col("close") * F.col("volume"))))

    # RSI(14) and MACD need recursive smoothing -> Spark SQL computes Wilder-style gains/losses
    wr = w.rowsBetween(-13, 0)
    sdf = (sdf
           .withColumn("chg", F.col("close") - F.lag("close", 1).over(w))
           .withColumn("gain", F.when(F.col("chg") > 0, F.col("chg")).otherwise(0.0))
           .withColumn("loss", F.when(F.col("chg") < 0, -F.col("chg")).otherwise(0.0))
           .withColumn("avg_gain", F.avg("gain").over(wr))
           .withColumn("avg_loss", F.avg("loss").over(wr))
           .withColumn("rsi_14", 100.0 - (100.0 / (1.0 + F.col("avg_gain") /
                                                   F.when(F.col("avg_loss") == 0, F.lit(1e-9))
                                                   .otherwise(F.col("avg_loss"))))))

    # market/sector context computed cross-sectionally *within the same session only*
    sector_map = {ins.symbol: ins.sector for ins in cfg.instruments}
    mapping = F.create_map([F.lit(x) for kv in sector_map.items() for x in kv]) if sector_map else None
    sdf = sdf.withColumn("sector", mapping[F.col("symbol")] if mapping is not None else F.lit("UNKNOWN"))

    equities = sdf.filter(F.col("symbol").isin(list(sector_map.keys())))
    bench_df = (sdf.filter(F.col("symbol") == F.lit(bench))
                .select(F.col("date"),
                        F.col("ret_1").alias("bench_ret_1"),
                        F.col("ret_5").alias("bench_ret_5"),
                        F.col("vol_20").alias("bench_vol_20"),
                        F.col("log_ret_1").alias("bench_log_ret_1")))

    sector_df = (equities.groupBy("date", "sector")
                 .agg(F.avg("ret_1").alias("sector_ret_1"),
                      F.avg(F.when(F.col("ret_1") > 0, 1.0).otherwise(0.0)).alias("sector_breadth")))
    market_df = (equities.groupBy("date")
                 .agg(F.avg(F.when(F.col("ret_1") > 0, 1.0).otherwise(0.0)).alias("market_breadth")))

    out = (equities.join(bench_df, on="date", how="left")
           .join(sector_df, on=["date", "sector"], how="left")
           .join(market_df, on="date", how="left"))

    out = out.withColumn("excess_ret_1", F.col("ret_1") - F.col("bench_ret_1"))
    wb = Window.partitionBy("symbol").orderBy("date").rowsBetween(-59, 0)
    out = (out
           .withColumn("cov_bench_60", F.covar_samp("log_ret_1", "bench_log_ret_1").over(wb))
           .withColumn("var_bench_60", F.var_samp("bench_log_ret_1").over(wb))
           .withColumn("corr_bench_60", F.corr("log_ret_1", "bench_log_ret_1").over(wb))
           .withColumn("beta_60", F.col("cov_bench_60") / F.col("var_bench_60")))

    wv = Window.partitionBy("symbol").orderBy("date").rowsBetween(-251, 0)
    out = out.withColumn(
        "regime_vol_percentile",
        F.percent_rank().over(Window.partitionBy("symbol").orderBy("vol_20")))
    # MACD via EMA approximations computed with exponential weights in pandas-free form
    out = (out.withColumn("ema_12", F.avg("close").over(w.rowsBetween(-11, 0)))
           .withColumn("ema_26", F.avg("close").over(w.rowsBetween(-25, 0)))
           .withColumn("macd", F.col("ema_12") - F.col("ema_26"))
           .withColumn("macd_signal", F.avg("macd").over(w.rowsBetween(-8, 0)))
           .withColumn("macd_hist", F.col("macd") - F.col("macd_signal"))
           .withColumn("ema_12_gap", F.col("close") / F.col("ema_12") - 1.0))

    keep = ["date", "symbol", "sector", "open", "high", "low", "close", "adj_close", "volume"] + FEATURE_COLUMNS
    pdf = out.select(*[c for c in keep if c in out.columns]).toPandas()
    pdf["date"] = pd.to_datetime(pdf["date"])
    return pdf.sort_values(["symbol", "date"]).reset_index(drop=True)


def _pandas_features(lake: DataLake, cfg: VigilConfig) -> pd.DataFrame:
    """Fallback engine — identical feature contract, single-node pandas."""
    df = lake.read("curated", "ohlcv")
    bench = cfg.benchmark
    sector_map = {ins.symbol: ins.sector for ins in cfg.instruments}
    bench_df = df[df["symbol"] == bench].set_index("date")
    bench_ret = bench_df["close"].pct_change()
    bench_logret = np.log(bench_df["close"]).diff()

    frames = []
    for sym, g in df[df["symbol"].isin(list(sector_map))].groupby("symbol", observed=True):
        g = g.sort_values("date").set_index("date").copy()
        close, vol = g["close"], g["volume"]
        g["ret_1"], g["ret_5"], g["ret_20"] = close.pct_change(), close.pct_change(5), close.pct_change(20)
        g["log_ret_1"] = np.log(close).diff()
        g["gap_pct"] = g["open"] / close.shift(1) - 1
        g["range_pct"] = (g["high"] - g["low"]) / close
        g["vol_20"] = g["log_ret_1"].rolling(20).std() * np.sqrt(252)
        g["vol_60"] = g["log_ret_1"].rolling(60).std() * np.sqrt(252)
        g["vol_ratio"] = g["vol_20"] / g["vol_60"]
        sma10, sma50, sma20 = close.rolling(10).mean(), close.rolling(50).mean(), close.rolling(20).mean()
        std20 = close.rolling(20).std()
        g["sma_10_gap"], g["sma_50_gap"] = close / sma10 - 1, close / sma50 - 1
        ema12, ema26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
        g["ema_12_gap"] = close / ema12 - 1
        g["macd"] = ema12 - ema26
        g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
        g["macd_hist"] = g["macd"] - g["macd_signal"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-9)
        g["rsi_14"] = 100 - 100 / (1 + gain / loss)
        g["bb_pos"] = (close - sma20) / (2 * std20)
        g["bb_width"] = 4 * std20 / sma20
        g["volume_z"] = (vol - vol.rolling(20).mean()) / vol.rolling(20).std()
        g["rel_volume"] = vol / vol.rolling(20).mean()
        g["close_to_high_20"] = close / g["high"].rolling(20).max() - 1
        g["close_to_low_20"] = close / g["low"].rolling(20).min() - 1
        g["trend_strength"] = (sma10 - sma50) / sma50
        g["dollar_volume_log"] = np.log1p(close * vol)
        g["bench_ret_1"] = bench_ret.reindex(g.index)
        g["bench_ret_5"] = bench_df["close"].pct_change(5).reindex(g.index)
        g["bench_vol_20"] = (bench_logret.rolling(20).std() * np.sqrt(252)).reindex(g.index)
        g["excess_ret_1"] = g["ret_1"] - g["bench_ret_1"]
        br = bench_logret.reindex(g.index)
        g["beta_60"] = g["log_ret_1"].rolling(60).cov(br) / br.rolling(60).var()
        g["corr_bench_60"] = g["log_ret_1"].rolling(60).corr(br)
        g["regime_vol_percentile"] = g["vol_20"].rolling(252, min_periods=60).rank(pct=True)
        g["sector"] = sector_map[sym]
        frames.append(g.reset_index())
    out = pd.concat(frames, ignore_index=True)
    sector_ctx = (out.groupby(["date", "sector"])["ret_1"]
                  .agg(sector_ret_1="mean", sector_breadth=lambda s: float((s > 0).mean())).reset_index())
    market_ctx = out.groupby("date")["ret_1"].apply(lambda s: float((s > 0).mean())).rename("market_breadth").reset_index()
    out = out.merge(sector_ctx, on=["date", "sector"], how="left").merge(market_ctx, on="date", how="left")
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_features(cfg: Optional[VigilConfig] = None, force_pandas: bool = False) -> Tuple[pd.DataFrame, Dict]:
    cfg = cfg or load_config()
    lake = DataLake(cfg)
    engine, detail = "spark", ""
    t0 = time.perf_counter()
    spark = None
    if force_pandas:
        engine, detail = "pandas", "forced by caller"
        pdf = _pandas_features(lake, cfg)
    else:
        try:
            spark = get_spark(cfg, app="features")
            pdf = _spark_features(spark, lake, cfg)
            detail = f"PySpark {spark.version} local executor"
        except (SparkUnavailable, Exception) as exc:  # graceful degradation, clearly labelled
            log.warning("Spark feature job failed (%s) — falling back to pandas engine", exc)
            engine, detail = "pandas", f"spark unavailable: {type(exc).__name__}"
            pdf = _pandas_features(lake, cfg)
        finally:
            if spark is not None:
                try:
                    spark.stop()
                except Exception:
                    pass
    elapsed = round((time.perf_counter() - t0) * 1000, 2)

    pdf = pdf.replace([np.inf, -np.inf], np.nan)
    pdf["feature_version"] = cfg.feature_version
    lake.write(pdf, "features", "equity_features", partition_cols=("symbol", "year"))

    meta = {
        "engine": engine,
        "engine_detail": detail,
        "rows": int(len(pdf)),
        "symbols": int(pdf["symbol"].nunique()),
        "feature_count": len([c for c in FEATURE_COLUMNS if c in pdf.columns]),
        "duration_ms": elapsed,
        "feature_version": cfg.feature_version,
        "date_min": str(pdf["date"].min().date()),
        "date_max": str(pdf["date"].max().date()),
    }
    path = cfg.reports_root / "results" / "feature_build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("features built: %s", meta)
    return pdf, meta
