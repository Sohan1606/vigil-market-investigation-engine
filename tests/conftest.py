"""Shared fixtures. Tests run against the REAL artefacts built by scripts/run_pipeline.py.

Tests that need an artefact which has not been built are skipped with an explicit reason rather
than silently passing — an un-run pipeline must never look like a green test suite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "reports" / "results"


@pytest.fixture(scope="session")
def cfg():
    from vigil.config import load_config
    return load_config()


@pytest.fixture(scope="session")
def lake(cfg):
    from vigil.storage.lake import DataLake
    return DataLake(cfg)


@pytest.fixture(scope="session")
def matrix(lake):
    if not lake.exists("features", "model_matrix"):
        pytest.skip("model matrix not built — run scripts/run_pipeline.py --all")
    return lake.read("features", "model_matrix")


def result(name: str):
    p = RESULTS / name
    if not p.exists():
        pytest.skip(f"{name} not built — run scripts/run_pipeline.py --all")
    return json.loads(p.read_text(encoding='utf-8'))
