"""Evaluation metrics: classification quality, probability quality and calibration.

Accuracy alone is explicitly rejected as a success criterion (see docs/evaluation.md).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, brier_score_loss, confusion_matrix, f1_score,
                             log_loss, precision_score, recall_score, roc_auc_score)


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        ece += mask.mean() * abs(y[mask].mean() - p[mask].mean())
    return float(ece)


def reliability_curve(y: np.ndarray, p: np.ndarray, bins: int = 10) -> List[Dict[str, float]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    out = []
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        out.append({"bin_mid": round(float((edges[b] + edges[b + 1]) / 2), 3),
                    "predicted": round(float(p[mask].mean()), 4),
                    "observed": round(float(y[mask].mean()), 4),
                    "count": int(mask.sum())})
    return out


def classification_metrics(preds: pd.DataFrame, threshold: float = 0.5) -> Dict[str, object]:
    if preds.empty:
        return {"available": False}
    y = preds["y_true"].to_numpy(dtype=int)
    p = preds["p_up"].to_numpy(dtype=float)
    yhat = (p >= threshold).astype(int)
    metrics: Dict[str, object] = {
        "available": True,
        "n": int(len(y)),
        "positive_rate_actual": round(float(y.mean()), 4),
        "positive_rate_predicted": round(float(yhat.mean()), 4),
        "accuracy": round(float(accuracy_score(y, yhat)), 4),
        "precision": round(float(precision_score(y, yhat, zero_division=0)), 4),
        "recall": round(float(recall_score(y, yhat, zero_division=0)), 4),
        "f1": round(float(f1_score(y, yhat, zero_division=0)), 4),
        "brier": round(float(brier_score_loss(y, p)), 5),
        "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 5),
        "ece": round(expected_calibration_error(y, p), 5),
        "reliability": reliability_curve(y, p),
    }
    try:
        metrics["roc_auc"] = round(float(roc_auc_score(y, p)), 4)
    except ValueError:
        metrics["roc_auc"] = None
    tn, fp, fn, tp = confusion_matrix(y, yhat, labels=[0, 1]).ravel()
    metrics["confusion_matrix"] = {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)}
    # skill relative to always predicting the majority class
    majority = max(y.mean(), 1 - y.mean())
    metrics["accuracy_vs_majority"] = round(float(metrics["accuracy"]) - float(majority), 4)
    metrics["majority_class_accuracy"] = round(float(majority), 4)
    return metrics


def metrics_by_group(preds: pd.DataFrame, group: pd.Series, name: str) -> List[Dict[str, object]]:
    out = []
    df = preds.copy()
    df["_g"] = group.values
    for g, sub in df.groupby("_g"):
        if len(sub) < 50:
            continue
        m = classification_metrics(sub)
        out.append({name: str(g), "n": m["n"], "accuracy": m["accuracy"], "roc_auc": m["roc_auc"],
                    "brier": m["brier"], "ece": m["ece"]})
    return sorted(out, key=lambda d: -d["n"])
