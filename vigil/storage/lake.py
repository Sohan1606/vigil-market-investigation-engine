"""Partitioned Parquet data lake with two real storage modes.

VIGIL writes the analytical layer as Hive-partitioned Parquet (`zone/dataset/symbol=.../year=...`).
The identical layout is used in both storage modes:

  STORAGE_MODE=local (default) — POSIX filesystem under `data/lake/`
  STORAGE_MODE=hdfs            — genuine HDFS IO through pyarrow's Hadoop client

The mode is explicit. When `hdfs` is requested but no Hadoop client / NameNode is reachable the
lake raises `StorageUnavailable` with a remediation checklist instead of quietly writing locally,
and every surface (Observatory, health, audits) reports LOCAL DATA LAKE. See docs/HDFS.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from .backend import (HDFS, LOCAL, StorageStatus, StorageUnavailable,  # noqa: F401
                      get_backend, resolve_storage_mode, storage_status)

log = get_logger("vigil.storage.lake")

ZONES = ("raw", "curated", "features", "analytics")


class DataLake:
    """Partitioned columnar store with block-level metadata, backed by POSIX or HDFS."""

    def __init__(self, cfg: Optional[VigilConfig] = None, mode: Optional[str] = None) -> None:
        self.cfg = cfg or load_config()
        self.root = self.cfg.lake_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.hdfs_uri = self.cfg.hdfs_uri
        self.requested_mode = (mode or resolve_storage_mode(self.cfg)).upper()
        # Raises StorageUnavailable when hdfs is requested but not reachable (no silent fallback).
        self.backend = get_backend(self.cfg, self.requested_mode)
        self.storage_mode = self.backend.mode

    @property
    def is_hdfs(self) -> bool:
        return self.storage_mode == HDFS

    def status(self) -> StorageStatus:
        """Truthful storage status for the Observatory / audits (never raises)."""
        return storage_status(self.cfg)

    # ---------------- paths ----------------
    def _check_zone(self, zone: str) -> str:
        if zone not in ZONES:
            raise ValueError(f"unknown lake zone '{zone}' (expected one of {ZONES})")
        return zone

    def dataset_path(self, zone: str, dataset: str) -> Path:
        """POSIX path of a dataset. Local mode only — HDFS datasets are addressed by URI."""
        self._check_zone(zone)
        if self.is_hdfs:
            raise StorageUnavailable(
                "dataset_path() is a local-filesystem concept; in STORAGE_MODE=hdfs use "
                "lake.uri(zone, dataset) or the lake read/write API.")
        return self.root / zone / dataset

    def uri(self, zone: str, dataset: str) -> str:
        """The address actually used for IO in the active storage mode."""
        self._check_zone(zone)
        return self.backend.uri(zone, dataset)

    # ---------------- write ----------------
    def write(
        self,
        df: pd.DataFrame,
        zone: str,
        dataset: str,
        partition_cols: Iterable[str] = ("symbol", "year"),
        overwrite: bool = True,
    ) -> Path:
        if df.empty:
            raise ValueError(f"refusing to write empty dataframe to {zone}/{dataset}")
        frame = df.copy()
        partition_cols = [c for c in partition_cols if c in frame.columns or c == "year"]
        if "year" in partition_cols and "year" not in frame.columns:
            if "date" not in frame.columns:
                raise ValueError("partitioning by year requires a 'date' column")
            frame["year"] = pd.to_datetime(frame["date"]).dt.year
        self._check_zone(zone)
        if overwrite:
            self.backend.remove(zone, dataset)
        # Spark/Parquet interop: nanosecond timestamps are not a legal Parquet type for Spark,
        # so the lake standardises on microsecond precision.
        for col in frame.columns:
            if pd.api.types.is_datetime64_any_dtype(frame[col]):
                frame[col] = frame[col].astype("datetime64[us]")
        written = self.backend.write_parquet(frame, zone, dataset, list(partition_cols))
        self._write_manifest(zone, dataset, frame, partition_cols)
        log.info("lake write mode=%s zone=%s dataset=%s rows=%d partitions=%s",
                 self.storage_mode, zone, dataset, len(frame), partition_cols)
        return Path(written) if not self.is_hdfs else written

    def _write_manifest(self, zone: str, dataset: str, df: pd.DataFrame, partition_cols: List[str]) -> None:
        blocks = self.backend.list_blocks(zone, dataset)
        manifest = {
            "zone": zone,
            "dataset": dataset,
            "rows": int(len(df)),
            "columns": list(df.columns),
            "partition_cols": list(partition_cols),
            "block_count": len(blocks),
            "bytes_on_disk": int(sum(b["size"] for b in blocks)),
            "storage_mode": self.storage_mode,
            "uri": self.uri(zone, dataset),
        }
        self.backend.write_text(zone, dataset, "_vigil_manifest.json", json.dumps(manifest, indent=2), encoding="utf-8")

    # ---------------- read ----------------
    def read(self, zone: str, dataset: str, columns: Optional[List[str]] = None,
             filters: Optional[list] = None) -> pd.DataFrame:
        self._check_zone(zone)
        if not self.backend.exists(zone, dataset):
            raise FileNotFoundError(
                f"lake dataset missing: {zone}/{dataset} ({self.uri(zone, dataset)}) "
                f"[storage mode {self.storage_mode}]")
        df = self.backend.read_parquet(zone, dataset, columns=columns, filters=filters)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values(["symbol", "date"] if "symbol" in df.columns else ["date"])
        return df.reset_index(drop=True)

    def exists(self, zone: str, dataset: str) -> bool:
        self._check_zone(zone)
        return self.backend.exists(zone, dataset)

    def manifest(self, zone: str, dataset: str) -> dict:
        raw = self.backend.read_text(zone, dataset, "_vigil_manifest.json")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def inventory(self) -> List[dict]:
        """Block-level inventory — what the DATA OBSERVATORY renders as 'HDFS state'."""
        out: List[dict] = []
        for zone in ZONES:
            for name in self.backend.list_datasets(zone):
                man = self.manifest(zone, name)
                blocks = self.backend.list_blocks(zone, name)
                out.append({
                    "zone": zone,
                    "dataset": name,
                    "rows": man.get("rows"),
                    "blocks": len(blocks),
                    "bytes": int(sum(b["size"] for b in blocks)),
                    "partitions": man.get("partition_cols", []),
                    "storage_mode": self.storage_mode,
                    "uri": self.uri(zone, name),
                })
        return out
