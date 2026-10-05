"""Model contracts. Every VIGIL model exposes the same fit / predict_proba surface so the
tournament, the ensemble and the audit trail can treat them identically."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import numpy as np
import pandas as pd


class ProbabilisticModel(Protocol):
    name: str
    family: str

    def fit(self, X: np.ndarray, y: np.ndarray, **kwargs: Any) -> "ProbabilisticModel": ...
    def predict_proba(self, X: np.ndarray) -> np.ndarray: ...


@dataclass
class ModelSpec:
    model_id: str
    name: str
    family: str                      # BASELINE | LINEAR | TREE | BOOSTING | DEEP | ATTENTION
    sequence_model: bool = False
    params: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class FoldResult:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    fit_seconds: float
    inference_ms_per_1k: float


@dataclass
class ModelRun:
    spec: ModelSpec
    predictions: pd.DataFrame            # date, symbol, p_up, y_true, fwd_ret, fold
    folds: List[FoldResult] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)
    available: bool = True
