"""Walk-forward (rolling-origin) evaluation — the only validation protocol VIGIL accepts.

Guarantees enforced in code:
  * training rows are strictly older than test rows (assert_chronological_split),
  * a purge gap of `horizon` sessions sits between train and test so an overlapping label can
    never be learned (label at t is observed at t+h),
  * scaling/imputation statistics are fit on the training fold ONLY,
  * sequence windows for deep models are built inside a fold and never cross the boundary.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..features.pit import assert_chronological_split, assert_no_future_columns
from ..logging_utils import get_logger
from .base import FoldResult, ModelRun, ModelSpec
from .zoo import build_model

log = get_logger("vigil.models.walkforward")


@dataclass
class SplitPlan:
    train_years: int = 3
    test_months: int = 12
    min_train_rows: int = 400
    purge_days: int = 1


def make_folds(dates: pd.Series, plan: SplitPlan) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    dates = pd.to_datetime(pd.Series(sorted(dates.unique())))
    start, end = dates.min(), dates.max()
    folds = []
    test_start = start + pd.DateOffset(years=plan.train_years)
    while test_start < end:
        test_end = min(test_start + pd.DateOffset(months=plan.test_months), end + pd.Timedelta(days=1))
        folds.append((start, test_start, test_end))
        test_start = test_end
    return folds


class WalkForwardRunner:
    def __init__(self, feature_cols: Sequence[str], horizon: int = 1, plan: Optional[SplitPlan] = None,
                 seed: int = 42) -> None:
        assert_no_future_columns(feature_cols)
        self.feature_cols = list(feature_cols)
        self.horizon = horizon
        self.plan = plan or SplitPlan()
        self.seed = seed

    # ---------------- matrix helpers ----------------
    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        label = f"label_dir_{self.horizon}"
        fwd = f"fwd_ret_{self.horizon}"
        need = self.feature_cols + [label, fwd, "date", "symbol"]
        out = df[need].copy()
        out = out.dropna(subset=[label])
        return out.sort_values(["date", "symbol"]).reset_index(drop=True)

    @staticmethod
    def _fit_scaler(train: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        median = np.nanmedian(train, axis=0)
        median = np.where(np.isnan(median), 0.0, median)
        filled = np.where(np.isnan(train), median, train)
        mean = filled.mean(axis=0)
        std = filled.std(axis=0)
        std[std == 0] = 1.0
        return median, mean, std

    @staticmethod
    def _apply_scaler(X: np.ndarray, median: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
        filled = np.where(np.isnan(X), median, X)
        return np.clip((filled - mean) / std, -8, 8)

    def _sequences(self, frame: pd.DataFrame, Xs: np.ndarray, seq_len: int
                   ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Build per-symbol sequence windows that never cross a symbol or a fold boundary."""
        seqs, idxs = [], []
        frame = frame.reset_index(drop=True)
        for _, g in frame.groupby("symbol", sort=False):
            pos = g.index.to_numpy()
            if len(pos) < seq_len:
                continue
            block = Xs[pos]
            for i in range(seq_len - 1, len(pos)):
                seqs.append(block[i - seq_len + 1: i + 1])
                idxs.append(pos[i])
        if not seqs:
            return np.empty((0, seq_len, Xs.shape[1])), np.array([], dtype=int), np.array([], dtype=int)
        order = np.argsort(idxs)
        return np.asarray(seqs)[order], np.asarray(idxs)[order], np.asarray(idxs)[order]

    # ---------------- main loop ----------------
    def run(self, df: pd.DataFrame, spec: ModelSpec) -> ModelRun:
        data = self._prepare(df)
        label, fwd = f"label_dir_{self.horizon}", f"fwd_ret_{self.horizon}"
        folds = make_folds(data["date"], self.plan)
        rows: List[pd.DataFrame] = []
        fold_results: List[FoldResult] = []
        failures: List[str] = []

        for k, (train_start, test_start, test_end) in enumerate(folds, start=1):
            purge_end = test_start - pd.Timedelta(days=self.horizon + self.plan.purge_days)
            train = data[(data["date"] >= train_start) & (data["date"] <= purge_end)]
            test = data[(data["date"] >= test_start) & (data["date"] < test_end)]
            if len(train) < self.plan.min_train_rows or test.empty:
                continue
            assert_chronological_split(train["date"], test["date"])

            Xtr_raw = train[self.feature_cols].to_numpy(dtype=float)
            Xte_raw = test[self.feature_cols].to_numpy(dtype=float)
            median, mean, std = self._fit_scaler(Xtr_raw)       # train-only statistics
            Xtr = self._apply_scaler(Xtr_raw, median, mean, std)
            Xte = self._apply_scaler(Xte_raw, median, mean, std)
            ytr = train[label].to_numpy(dtype=float)
            yte = test[label].to_numpy(dtype=float)

            t0 = time.perf_counter()
            try:
                if spec.sequence_model:
                    seq_len = int(spec.params.get("seq_len", 20))
                    Str, itr, _ = self._sequences(train, Xtr, seq_len)
                    Ste, ite, _ = self._sequences(test, Xte, seq_len)
                    if len(Str) < 200 or len(Ste) == 0:
                        failures.append(f"fold {k}: insufficient sequences")
                        continue
                    model = build_model(spec, n_features=Xtr.shape[1], seed=self.seed)
                    model.fit(Str, ytr[itr])
                    fit_s = time.perf_counter() - t0
                    t1 = time.perf_counter()
                    proba = model.predict_proba(Ste)[:, 1]
                    infer_ms = (time.perf_counter() - t1) * 1000 / max(len(Ste) / 1000, 1e-9)
                    test_slice = test.iloc[ite]
                    y_used = yte[ite]
                else:
                    model = build_model(spec, n_features=Xtr.shape[1], seed=self.seed)
                    model.fit(Xtr, ytr)
                    fit_s = time.perf_counter() - t0
                    t1 = time.perf_counter()
                    proba = model.predict_proba(Xte)[:, 1]
                    infer_ms = (time.perf_counter() - t1) * 1000 / max(len(Xte) / 1000, 1e-9)
                    test_slice, y_used = test, yte
            except Exception as exc:
                failures.append(f"fold {k}: {type(exc).__name__}: {exc}")
                log.warning("model %s failed on fold %d: %s", spec.model_id, k, exc)
                continue

            rows.append(pd.DataFrame({
                "date": test_slice["date"].values,
                "symbol": test_slice["symbol"].values,
                "p_up": np.clip(proba, 1e-6, 1 - 1e-6),
                "y_true": y_used,
                "fwd_ret": test_slice[fwd].values,
                "fold": k,
                "model_id": spec.model_id,
            }))
            fold_results.append(FoldResult(
                fold=k, train_start=str(train["date"].min().date()), train_end=str(train["date"].max().date()),
                test_start=str(test_slice["date"].min().date()), test_end=str(test_slice["date"].max().date()),
                n_train=len(train), n_test=len(test_slice), fit_seconds=round(fit_s, 2),
                inference_ms_per_1k=round(infer_ms, 3)))

        preds = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
            columns=["date", "symbol", "p_up", "y_true", "fwd_ret", "fold", "model_id"])
        available = len(preds) > 0
        if not available:
            failures.append("no fold produced predictions")
        log.info("walk-forward %s: folds=%d oos_rows=%d", spec.model_id, len(fold_results), len(preds))
        return ModelRun(spec=spec, predictions=preds, folds=fold_results, failures=failures,
                        available=available)
