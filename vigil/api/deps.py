"""Shared, cached service objects for the API layer."""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import VigilConfig, load_config
from ..decision.forecast_service import ForecastService
from ..logging_utils import get_logger
from ..storage.docstore import DocumentStore
from ..storage.lake import DataLake

log = get_logger("vigil.api.deps")
_CACHE: Dict[str, Any] = {}


@lru_cache(maxsize=1)
def config() -> VigilConfig:
    return load_config()


@lru_cache(maxsize=1)
def store() -> DocumentStore:
    return DocumentStore(config())


@lru_cache(maxsize=1)
def lake() -> DataLake:
    return DataLake(config())


@lru_cache(maxsize=1)
def forecast_service() -> ForecastService:
    return ForecastService(config())


def result(name: str, default: Optional[Any] = None) -> Any:
    """Read a pipeline artefact with an mtime-aware cache (artefacts are immutable per run)."""
    if not name.endswith(".json"):          # callers may pass either 'regime' or 'regime.json'
        name = f"{name}.json"
    path: Path = config().reports_root / "results" / name
    if not path.exists():
        return default
    key = f"{path}:{path.stat().st_mtime_ns}"
    if key not in _CACHE:
        try:
            _CACHE[key] = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.error("corrupt artefact %s: %s", name, exc)
            return default
    return _CACHE[key]


def artefact_missing(name: str, what: str) -> Dict[str, Any]:
    """Meaningful degraded-state payload — never 'something went wrong'."""
    return {
        "available": False,
        "state": "ARTEFACT_MISSING",
        "what_failed": f"{what} has not been generated yet ({name} not found)",
        "affected": what,
        "fallback": "Run `python scripts/run_pipeline.py --all` to build the artefacts.",
    }
