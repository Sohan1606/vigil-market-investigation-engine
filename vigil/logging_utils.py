"""Structured, non-leaky logging + a lightweight latency instrument used by the Observatory."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

_CONFIGURED = False
_METRICS: List[Dict[str, Any]] = []

SENSITIVE = ("password", "secret", "token", "api_key", "uri")


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=os.getenv("VIGIL_LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
            datefmt="%H:%M:%S",
        )
        _CONFIGURED = True
    return logging.getLogger(name)


def redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Never log credentials or connection strings."""
    out = {}
    for k, v in payload.items():
        out[k] = "***redacted***" if any(s in k.lower() for s in SENSITIVE) else v
    return out


@contextmanager
def timed(stage: str, **tags: Any) -> Iterator[Dict[str, Any]]:
    """Measure a real wall-clock stage latency. Values are *measured*, never invented."""
    log = get_logger("vigil.telemetry")
    rec: Dict[str, Any] = {"stage": stage, "tags": tags, "started_at": time.time()}
    t0 = time.perf_counter()
    try:
        yield rec
        rec["status"] = "OK"
    except Exception as exc:  # pragma: no cover - defensive
        rec["status"] = "ERROR"
        rec["error"] = type(exc).__name__
        raise
    finally:
        rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        _METRICS.append(rec)
        log.info("stage=%s status=%s duration_ms=%s %s", stage, rec.get("status"), rec["duration_ms"], redact(tags))


def collected_metrics() -> List[Dict[str, Any]]:
    return list(_METRICS)


def flush_metrics(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: List[Dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = []
    payload = (existing + _METRICS)[-500:]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
