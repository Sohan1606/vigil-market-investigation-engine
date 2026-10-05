"""Configuration loading for VIGIL. Single source of truth: config/vigil.yaml + environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    sector: str

    @property
    def slug(self) -> str:
        return self.symbol.replace(".", "_").replace("^", "IDX_")


@dataclass
class VigilConfig:
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---------- generic access ----------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    # ---------- paths ----------
    def path(self, path_key: str, default: str) -> Path:
        value = self.get(path_key, default)
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def lake_root(self) -> Path:
        return self.path("storage.lake_root", "data/lake")

    @property
    def raw_root(self) -> Path:
        return self.path("storage.raw_root", "data/raw")

    @property
    def processed_root(self) -> Path:
        return self.path("storage.processed_root", "data/processed")

    @property
    def reports_root(self) -> Path:
        return REPO_ROOT / "reports"

    # ---------- universe ----------
    @property
    def instruments(self) -> List[Instrument]:
        return [Instrument(**row) for row in self.get("universe.equities", [])]

    @property
    def symbols(self) -> List[str]:
        return [i.symbol for i in self.instruments]

    @property
    def benchmark(self) -> str:
        return self.get("universe.benchmark", "^NSEI")

    @property
    def sector_indices(self) -> Dict[str, str]:
        return dict(self.get("universe.sector_indices", {}) or {})

    def sector_of(self, symbol: str) -> str:
        for ins in self.instruments:
            if ins.symbol == symbol:
                return ins.sector
        return "UNKNOWN"

    def name_of(self, symbol: str) -> str:
        for ins in self.instruments:
            if ins.symbol == symbol:
                return ins.name
        return symbol

    # ---------- runtime ----------
    @property
    def mode(self) -> str:
        return os.getenv("VIGIL_MODE", self.get("project.mode", "DEMO")).upper()

    @property
    def seed(self) -> int:
        return int(self.get("project.seed", 20260101))

    @property
    def mongodb_uri(self) -> Optional[str]:
        return os.getenv("MONGODB_URI") or self.get("storage.docstore.uri")

    @property
    def kafka_bootstrap(self) -> Optional[str]:
        return os.getenv("KAFKA_BOOTSTRAP_SERVERS") or self.get("streaming.kafka_bootstrap")

    @property
    def hdfs_uri(self) -> Optional[str]:
        return os.getenv("HDFS_URI") or self.get("storage.hdfs_uri")

    @property
    def feature_version(self) -> str:
        return str(self.get("features.version", "fv-1.0.0"))


@lru_cache(maxsize=4)
def load_config(path: Optional[str] = None) -> VigilConfig:
    _load_dotenv(REPO_ROOT / ".env")
    cfg_path = Path(path or os.getenv("VIGIL_CONFIG") or (REPO_ROOT / "config" / "vigil.yaml"))
    if not cfg_path.is_absolute():
        cfg_path = REPO_ROOT / cfg_path
    if not cfg_path.exists():
        raise FileNotFoundError(f"VIGIL config not found: {cfg_path}")
    with cfg_path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    return VigilConfig(raw=raw)
