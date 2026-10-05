"""Sequence models: an LSTM and a compact temporal-attention benchmark (both PyTorch, CPU-friendly).

The attention model is included as the 'modern time-series benchmark'. If it loses to gradient
boosting, VIGIL reports the loss — no model is promoted for marketing reasons.
Torch is optional: if it is not installed the models report themselves unavailable and the
tournament continues with the remaining families (graceful degradation).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ..logging_utils import get_logger

log = get_logger("vigil.models.deep")

try:  # torch is optional
    import torch
    import torch.nn as nn

    TORCH_AVAILABLE = True
except Exception:  # pragma: no cover
    TORCH_AVAILABLE = False
    torch = None  # type: ignore
    nn = object  # type: ignore


def torch_available() -> bool:
    return TORCH_AVAILABLE


if TORCH_AVAILABLE:

    class _LSTMNet(nn.Module):
        def __init__(self, n_features: int, hidden: int = 32, layers: int = 1, dropout: float = 0.1):
            super().__init__()
            self.lstm = nn.LSTM(n_features, hidden, num_layers=layers, batch_first=True,
                                dropout=dropout if layers > 1 else 0.0)
            self.head = nn.Sequential(nn.LayerNorm(hidden), nn.Dropout(dropout), nn.Linear(hidden, 1))

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.head(out[:, -1, :]).squeeze(-1)

    class _AttentionNet(nn.Module):
        """Single-block temporal transformer encoder with learned positional embedding."""

        def __init__(self, n_features: int, d_model: int = 32, heads: int = 4, seq_len: int = 20,
                     dropout: float = 0.1):
            super().__init__()
            self.proj = nn.Linear(n_features, d_model)
            self.pos = nn.Parameter(torch.zeros(1, seq_len, d_model))
            layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=heads, dim_feedforward=2 * d_model,
                                               dropout=dropout, batch_first=True, norm_first=True)
            self.encoder = nn.TransformerEncoder(layer, num_layers=1)
            self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, 1))

        def forward(self, x):
            h = self.proj(x) + self.pos[:, : x.shape[1], :]
            h = self.encoder(h)
            return self.head(h.mean(dim=1)).squeeze(-1)


class SequenceClassifier:
    """Shared training loop for both deep models (binary direction, BCE-with-logits)."""

    def __init__(self, kind: str = "lstm", n_features: int = 1, seq_len: int = 20, hidden: int = 32,
                 epochs: int = 8, lr: float = 3e-3, batch_size: int = 256, seed: int = 42,
                 dropout: float = 0.1) -> None:
        if not TORCH_AVAILABLE:
            raise RuntimeError("PyTorch is not available")
        self.kind, self.seq_len, self.epochs, self.lr, self.batch_size = kind, seq_len, epochs, lr, batch_size
        torch.manual_seed(seed)
        np.random.seed(seed)
        torch.set_num_threads(2)
        self.net = (_LSTMNet(n_features, hidden, dropout=dropout) if kind == "lstm"
                    else _AttentionNet(n_features, d_model=hidden, seq_len=seq_len, dropout=dropout))

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None):
        self.net.train()
        xt = torch.tensor(X, dtype=torch.float32)
        yt = torch.tensor(y, dtype=torch.float32)
        opt = torch.optim.AdamW(self.net.parameters(), lr=self.lr, weight_decay=1e-4)
        lossf = nn.BCEWithLogitsLoss()
        n = len(xt)
        for epoch in range(self.epochs):
            perm = torch.randperm(n)
            total = 0.0
            for i in range(0, n, self.batch_size):
                idx = perm[i: i + self.batch_size]
                opt.zero_grad()
                out = self.net(xt[idx])
                loss = lossf(out, yt[idx])
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.net.parameters(), 1.0)
                opt.step()
                total += float(loss) * len(idx)
            if epoch == self.epochs - 1:
                log.debug("%s final epoch loss %.4f", self.kind, total / max(n, 1))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        self.net.eval()
        with torch.no_grad():
            logits = self.net(torch.tensor(X, dtype=torch.float32))
            p = torch.sigmoid(logits).numpy()
        return np.column_stack([1 - p, p])
