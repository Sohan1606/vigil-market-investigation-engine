"""The VIGIL model zoo: every family required by the syllabus plus one modern benchmark."""
from __future__ import annotations

from typing import Dict, List

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..logging_utils import get_logger
from .base import ModelSpec
from .deep import torch_available

log = get_logger("vigil.models.zoo")

try:
    from xgboost import XGBClassifier

    XGB_AVAILABLE = True
except Exception:  # pragma: no cover
    XGB_AVAILABLE = False


class MomentumBaseline:
    """Non-trivial baseline: probability from the sign and strength of recent momentum only.

    A model that cannot beat this has learned nothing useful.
    """

    name, family = "baseline_momentum", "BASELINE"

    def __init__(self, feature_index: int = 0) -> None:
        self.feature_index = feature_index
        self.base_rate = 0.5
        self.scale = 1.0

    def fit(self, X: np.ndarray, y: np.ndarray, **kw):
        self.base_rate = float(np.clip(y.mean(), 0.01, 0.99))
        col = X[:, self.feature_index]
        self.scale = float(np.std(col)) or 1.0
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        z = np.tanh(X[:, self.feature_index] / (2 * self.scale)) * 0.08
        p = np.clip(self.base_rate + z, 0.01, 0.99)
        return np.column_stack([1 - p, p])


def model_specs(seed: int = 42) -> List[ModelSpec]:
    specs = [
        ModelSpec("baseline_momentum", "Momentum Baseline", "BASELINE",
                  rationale="Reference point: any model must beat a trivial momentum prior."),
        ModelSpec("logistic_regression", "Logistic Regression", "LINEAR",
                  params={"C": 0.5, "max_iter": 2000},
                  rationale="Transparent linear benchmark with well-behaved probabilities."),
        ModelSpec("random_forest", "Random Forest", "TREE",
                  params={"n_estimators": 300, "max_depth": 7, "min_samples_leaf": 40},
                  rationale="Captures non-linear interactions; low variance via bagging."),
    ]
    if XGB_AVAILABLE:
        specs.append(ModelSpec("xgboost", "XGBoost", "BOOSTING",
                               params={"n_estimators": 350, "max_depth": 4, "learning_rate": 0.045,
                                       "subsample": 0.85, "colsample_bytree": 0.8,
                                       "reg_lambda": 2.0, "min_child_weight": 8},
                               rationale="State-of-practice tabular learner for financial features."))
    if torch_available():
        specs.append(ModelSpec("lstm", "LSTM (20-session)", "DEEP", sequence_model=True,
                               params={"hidden": 32, "epochs": 8, "lr": 0.003, "seq_len": 20},
                               rationale="Sequence memory over the recent feature trajectory."))
        specs.append(ModelSpec("temporal_attention", "Temporal Attention (TST-lite)", "ATTENTION",
                               sequence_model=True,
                               params={"hidden": 32, "epochs": 8, "lr": 0.002, "seq_len": 20, "heads": 4},
                               rationale="Modern time-series benchmark; reported honestly, win or lose."))
    return specs


def build_model(spec: ModelSpec, n_features: int, seed: int = 42):
    if spec.model_id == "baseline_momentum":
        return MomentumBaseline()
    if spec.model_id == "logistic_regression":
        return Pipeline([("scale", StandardScaler()),
                         ("clf", LogisticRegression(random_state=seed, **spec.params))])
    if spec.model_id == "random_forest":
        return RandomForestClassifier(random_state=seed, n_jobs=2, **spec.params)
    if spec.model_id == "xgboost":
        if not XGB_AVAILABLE:
            raise RuntimeError("xgboost unavailable")
        return XGBClassifier(random_state=seed, n_jobs=2, tree_method="hist",
                             eval_metric="logloss", **spec.params)
    if spec.sequence_model:
        from .deep import SequenceClassifier

        kind = "lstm" if spec.model_id == "lstm" else "attention"
        return SequenceClassifier(kind=kind, n_features=n_features,
                                  seq_len=spec.params.get("seq_len", 20),
                                  hidden=spec.params.get("hidden", 32),
                                  epochs=spec.params.get("epochs", 8),
                                  lr=spec.params.get("lr", 0.003), seed=seed)
    raise ValueError(f"unknown model: {spec.model_id}")
