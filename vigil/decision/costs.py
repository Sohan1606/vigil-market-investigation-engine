"""Transaction-cost model. All backtests in VIGIL are net of these assumptions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..config import VigilConfig, load_config


@dataclass
class CostModel:
    commission_bps: float = 3.0
    slippage_bps: float = 5.0
    impact_bps: float = 1.0

    @classmethod
    def from_config(cls, cfg: Optional[VigilConfig] = None) -> "CostModel":
        cfg = cfg or load_config()
        return cls(commission_bps=float(cfg.get("costs.commission_bps", 3.0)),
                   slippage_bps=float(cfg.get("costs.slippage_bps", 5.0)),
                   impact_bps=float(cfg.get("costs.impact_bps", 1.0)))

    @property
    def round_trip_bps(self) -> float:
        """A position opened and closed pays entry + exit."""
        return 2.0 * (self.commission_bps + self.slippage_bps + self.impact_bps)

    def cost_for_turnover(self, turnover: float) -> float:
        """Cost (as a return fraction) for a given traded fraction of capital."""
        return abs(turnover) * (self.commission_bps + self.slippage_bps + self.impact_bps) / 10_000.0

    def breakeven_edge_bps(self) -> float:
        return self.round_trip_bps

    def to_dict(self) -> Dict[str, float]:
        return {"commission_bps": self.commission_bps, "slippage_bps": self.slippage_bps,
                "impact_bps": self.impact_bps, "round_trip_bps": self.round_trip_bps}
