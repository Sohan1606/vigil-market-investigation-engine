"""Storage-mode tests (v1.0 defect #1): local vs real HDFS, with no silent downgrade.

These tests do not need a Hadoop cluster. They verify the contract that matters: the mode is
explicit, the local default works, an HDFS request without a cluster RAISES rather than quietly
writing to POSIX, and every status surface reports what is actually active.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from vigil.storage.backend import (HDFS, LOCAL, HdfsBackend, LocalBackend, StorageUnavailable,
                                   get_backend, parse_hdfs_uri, resolve_storage_mode,
                                   storage_status)
from vigil.storage.lake import DataLake


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("HDFS_URI", raising=False)
    return monkeypatch


def test_default_mode_is_local(cfg, clean_env):
    assert resolve_storage_mode(cfg) == LOCAL
    assert isinstance(get_backend(cfg), LocalBackend)


def test_invalid_mode_is_rejected_loudly(cfg, clean_env):
    clean_env.setenv("STORAGE_MODE", "s3")
    with pytest.raises(StorageUnavailable) as exc:
        resolve_storage_mode(cfg)
    assert "local" in str(exc.value) and "hdfs" in str(exc.value)


def test_hdfs_requires_a_uri(cfg, clean_env):
    clean_env.setenv("STORAGE_MODE", "hdfs")
    with pytest.raises(StorageUnavailable) as exc:
        get_backend(cfg)
    assert "HDFS_URI" in str(exc.value)


def test_hdfs_without_a_cluster_raises_instead_of_falling_back(cfg, clean_env):
    """The critical property: VIGIL must never pretend a local write was an HDFS write."""
    clean_env.setenv("STORAGE_MODE", "hdfs")
    clean_env.setenv("HDFS_URI", "hdfs://127.0.0.1:19000")
    try:
        lake = DataLake(cfg)
    except StorageUnavailable as exc:
        message = str(exc)
        assert "docs/HDFS.md" in message
        assert "will not silently fall back" in message
        return
    # If a cluster really is reachable here, then the mode must genuinely be HDFS.
    assert lake.storage_mode == HDFS
    assert lake.uri("curated", "ohlcv").startswith("hdfs://")


def test_status_surface_never_raises_and_tells_the_truth(cfg, clean_env):
    clean_env.setenv("STORAGE_MODE", "hdfs")
    clean_env.setenv("HDFS_URI", "hdfs://127.0.0.1:19000")
    st = storage_status(cfg)
    assert st.requested_mode == HDFS
    if not st.available:
        assert st.active_mode == LOCAL
        assert st.label == "LOCAL DATA LAKE"
        assert "unavailable" in st.detail.lower()
    else:
        assert st.label == "HDFS DATA LAKE"


def test_local_mode_reports_a_local_label(cfg, clean_env):
    st = storage_status(cfg)
    assert st.active_mode == LOCAL and st.label == "LOCAL DATA LAKE"
    assert st.available is True
    assert not str(st.uri).startswith("/home"), "status must not leak a machine-specific path"


def test_hdfs_uri_parsing():
    assert parse_hdfs_uri("hdfs://namenode:8020/data") == {"host": "namenode", "port": 8020,
                                                           "path": "/data"}
    assert parse_hdfs_uri("hdfs://localhost:9000")["port"] == 9000
    assert parse_hdfs_uri(None)["host"] is None


def test_hdfs_backend_builds_cluster_paths_without_connecting():
    """Path/URI construction is pure and can be verified with no NameNode."""
    backend = HdfsBackend.__new__(HdfsBackend)       # bypass __init__/connect on purpose
    backend.host, backend.port, backend.prefix = "namenode", 8020, "/vigil"
    assert backend.path("curated", "ohlcv") == "/vigil/curated/ohlcv"
    assert backend.uri("features", "model_matrix") == "hdfs://namenode:8020/vigil/features/model_matrix"


def test_local_backend_round_trip(tmp_path):
    backend = LocalBackend(tmp_path)
    frame = pd.DataFrame({"symbol": ["A", "A", "B"], "year": [2024, 2024, 2025],
                          "value": [1.0, 2.0, 3.0]})
    backend.write_parquet(frame, "curated", "probe", ["symbol", "year"])
    assert backend.exists("curated", "probe")
    back = backend.read_parquet("curated", "probe")
    assert len(back) == 3 and set(back["value"]) == {1.0, 2.0, 3.0}
    assert backend.list_blocks("curated", "probe"), "parquet blocks should be enumerable"
    backend.remove("curated", "probe")
    assert not backend.exists("curated", "probe")


def test_lake_reports_its_mode_in_the_manifest(lake):
    man = lake.manifest("curated", "ohlcv")
    assert man, "curated/ohlcv manifest missing"
    assert man.get("uri")
    assert lake.storage_mode in (LOCAL, HDFS)
