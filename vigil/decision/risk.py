"""Risk engine: volatility, drawdown, Sharpe/Sortino, VaR/CVaR, beta and concentration.

Risk is not decoration — `position_risk` feeds the Decision Gate and can veto an otherwise
positive forecast.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def annualised_return(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    total = float((1 + returns.fillna(0)).prod())
    years = len(returns) / TRADING_DAYS
    return float(total ** (1 / years) - 1) if years > 0 and total > 0 else -1.0


def annualised_vol(returns: pd.Series) -> float:
    return float(returns.std() * np.sqrt(TRADING_DAYS)) if len(returns) > 2 else 0.0


def sharpe(returns: pd.Series, rf: float = 0.065) -> float:
    vol = annualised_vol(returns)
    return float((annualised_return(returns) - rf) / vol) if vol > 1e-9 else 0.0


def sortino(returns: pd.Series, rf: float = 0.065) -> float:
    downside = returns[returns < 0]
    dvol = float(downside.std() * np.sqrt(TRADING_DAYS)) if len(downside) > 2 else 0.0
    return float((annualised_return(returns) - rf) / dvol) if dvol > 1e-9 else 0.0


def max_drawdown(returns: pd.Series) -> float:
    curve = (1 + returns.fillna(0)).cumprod()
    return float((curve / curve.cummax() - 1).min()) if len(curve) else 0.0


def value_at_risk(returns: pd.Series, confidence: float = 0.95) -> float:
    return float(np.quantile(returns.dropna(), 1 - confidence)) if len(returns) > 20 else 0.0


def conditional_var(returns: pd.Series, confidence: float = 0.95) -> float:
    var = value_at_risk(returns, confidence)
    tail = returns[returns <= var]
    return float(tail.mean()) if len(tail) else var


def performance_summary(returns: pd.Series, rf: float = 0.065, confidence: float = 0.95) -> Dict[str, float]:
    returns = pd.Series(returns).dropna()
    return {
        "n_sessions": int(len(returns)),
        "total_return_pct": round(100 * float((1 + returns).prod() - 1), 3),
        "annualised_return_pct": round(100 * annualised_return(returns), 3),
        "annualised_vol_pct": round(100 * annualised_vol(returns), 3),
        "sharpe": round(sharpe(returns, rf), 3),
        "sortino": round(sortino(returns, rf), 3),
        "max_drawdown_pct": round(100 * max_drawdown(returns), 3),
        "var_95_pct": round(100 * value_at_risk(returns, confidence), 3),
        "cvar_95_pct": round(100 * conditional_var(returns, confidence), 3),
        "hit_rate_pct": round(100 * float((returns > 0).mean()), 2) if len(returns) else 0.0,
    }


@dataclass
class PositionRisk:
    symbol: str
    annualised_vol: float
    beta: float
    var_95: float
    drawdown_60: float
    risk_score: float          # 0 (calm) .. 100 (hostile)
    label: str

    def to_dict(self) -> Dict[str, float]:
        d = self.__dict__.copy()
        d["annualised_vol_pct"] = round(100 * self.annualised_vol, 2)
        d["var_95_pct"] = round(100 * self.var_95, 2)
        d["drawdown_60_pct"] = round(100 * self.drawdown_60, 2)
        return d


def position_risk(history: pd.DataFrame, symbol: str, max_position_vol: float = 0.045) -> PositionRisk:
    """history: per-symbol frame with columns [ret_1, vol_20, beta_60] ending at the decision date."""
    rets = history["ret_1"].dropna().tail(120)
    vol = float(history["vol_20"].iloc[-1]) if "vol_20" in history and pd.notna(history["vol_20"].iloc[-1]) \
        else annualised_vol(rets)
    beta = float(history["beta_60"].iloc[-1]) if "beta_60" in history and pd.notna(history["beta_60"].iloc[-1]) else 1.0
    var = value_at_risk(rets)
    dd = max_drawdown(rets.tail(60))
    daily_vol = vol / np.sqrt(TRADING_DAYS)
    score = float(np.clip(100 * (0.5 * min(daily_vol / max_position_vol, 2.0) / 2 +
                                 0.25 * min(abs(beta) / 1.8, 1.0) +
                                 0.25 * min(abs(dd) / 0.25, 1.0)), 0, 100))
    label = "LOW" if score < 35 else "MEDIUM" if score < 62 else "HIGH"
    return PositionRisk(symbol, vol, beta, var, dd, round(score, 1), label)
