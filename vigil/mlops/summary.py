"""Deterministic, recursive merging of pipeline summaries.

The pipeline is routinely run in several processes (`--stage models --horizons 1`, then
`--stage models --horizons 5`) because a single process that builds everything at once is
memory-bound. `reports/results/pipeline_summary.json` must therefore ACCUMULATE:

    run 1:  {"models": {"horizon_1": "random_forest"}}
    run 2:  {"models": {"horizon_5": "logistic"}}
    file :  {"models": {"horizon_1": "random_forest", "horizon_5": "logistic"}}

Rules
-----
1. Two dicts are merged key by key, recursively, at any depth.
2. A non-dict value replaces whatever was at that path (intentional overwrite — re-running a
   stage publishes its newest result).
3. A dict never silently replaces a scalar and vice versa: the incoming value wins, and the
   replacement is recorded in `merge_log` so the overwrite is visible rather than implicit.
4. Output is deterministic: keys are sorted, so the same set of invocations in any order that
   touches disjoint paths produces byte-identical JSON.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["deep_merge", "sort_keys", "merge_summary_file"]


def deep_merge(base: Dict[str, Any], incoming: Dict[str, Any],
               _path: str = "", _log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Recursively merge `incoming` into `base` and return a NEW dict (inputs untouched)."""
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        raise TypeError("deep_merge expects two dicts")
    out: Dict[str, Any] = {k: v for k, v in base.items()}
    for key, value in incoming.items():
        path = f"{_path}.{key}" if _path else key
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value, path, _log)
        else:
            if _log is not None and key in out and out[key] != value:
                _log.append(path)
            out[key] = value
    return out


def sort_keys(obj: Any) -> Any:
    """Recursively sort dict keys so the serialised summary is deterministic."""
    if isinstance(obj, dict):
        return {k: sort_keys(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, list):
        return [sort_keys(v) for v in obj]
    return obj


def merge_summary_file(path: Path, summary: Dict[str, Any],
                       invocation: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Merge `summary` into the stages of an existing summary file and write it back."""
    path = Path(path)
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}
    stages = existing.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    overwrites: List[str] = []
    merged = deep_merge(stages, summary, _log=overwrites)
    payload = {
        "stages": sort_keys(merged),
        "last_invocation": invocation or {},
        "overwritten_paths": sorted(overwrites),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload, overwrites
