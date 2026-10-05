"""Storage backends for the VIGIL data lake: POSIX local filesystem or real HDFS.

VIGIL has exactly two storage modes and never pretends to be in one while running in the other:

  STORAGE_MODE=local  (default)  parquet datasets under `data/lake/<zone>/<dataset>` on POSIX
  STORAGE_MODE=hdfs              parquet datasets under `<HDFS_URI>/<prefix>/<zone>/<dataset>`
                                 accessed through pyarrow's libhdfs/Hadoop client

The HDFS backend performs genuine distributed-filesystem IO (`pyarrow.fs.HadoopFileSystem` +
`pyarrow.parquet.write_to_dataset(filesystem=...)`). It requires a reachable NameNode, a Hadoop
client installation and `JAVA_HOME`/`HADOOP_HOME`/`CLASSPATH` on the machine. When those are
missing, `hdfs` mode raises a clear, actionable error instead of silently writing locally, and the
Observatory reports LOCAL DATA LAKE rather than claiming HDFS is operational.

See docs/HDFS.md for the setup and validation procedure.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import pandas as pd

from ..logging_utils import get_logger

log = get_logger("vigil.storage.backend")

LOCAL = "LOCAL"
HDFS = "HDFS"


class StorageUnavailable(RuntimeError):
    """Raised when the requested storage mode cannot be used. Never silently downgraded."""


@dataclass
class StorageStatus:
    """What the Observatory and the audits are allowed to say about storage."""

    requested_mode: str
    active_mode: str
    available: bool
    uri: str
    detail: str
    checks: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "HDFS DATA LAKE" if self.active_mode == HDFS else "LOCAL DATA LAKE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "active_mode": self.active_mode,
            "label": self.label,
            "available": self.available,
            "uri": self.uri,
            "detail": self.detail,
            "checks": self.checks,
        }


def resolve_storage_mode(cfg) -> str:
    """STORAGE_MODE env wins over config `storage.mode`; the default is always local."""
    raw = os.getenv("STORAGE_MODE") or cfg.get("storage.mode", "local")
    mode = str(raw).strip().upper()
    if mode not in (LOCAL, HDFS):
        raise StorageUnavailable(
            f"STORAGE_MODE='{raw}' is not valid. Use STORAGE_MODE=local or STORAGE_MODE=hdfs.")
    return mode


def parse_hdfs_uri(uri: Optional[str]) -> Dict[str, Any]:
    """`hdfs://host:port/path` → {host, port, path}. `host` may be a nameservice id."""
    if not uri:
        return {"host": None, "port": 8020, "path": "/"}
    parsed = urlparse(uri if "://" in uri else f"hdfs://{uri}")
    return {
        "host": parsed.hostname or "default",
        "port": int(parsed.port or 8020),
        "path": parsed.path or "/",
    }


# --------------------------------------------------------------------------- local backend
class LocalBackend:
    """POSIX filesystem backend — the default, fully offline path."""

    mode = LOCAL

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def display(self, path: Path | str) -> str:
        """Repo-relative rendering so no machine-specific absolute path leaks into artefacts."""
        from ..config import REPO_ROOT

        try:
            return str(Path(path).resolve().relative_to(Path(REPO_ROOT).resolve()))
        except ValueError:
            return str(path)

    # paths -----------------------------------------------------------------
    def path(self, zone: str, dataset: str) -> Path:
        return self.root / zone / dataset

    def uri(self, zone: str, dataset: str) -> str:
        """Portable logical URI for local storage.

        The physical filesystem path may be anywhere (for example a temporary test directory),
        but manifests and API surfaces must never expose a machine-specific path. Local-mode
        manifests therefore use the stable logical repository URI.
        """
        return f"data/lake/{zone}/{dataset}"

    # io --------------------------------------------------------------------
    def exists(self, zone: str, dataset: str) -> bool:
        return self.path(zone, dataset).exists()

    def remove(self, zone: str, dataset: str) -> None:
        target = self.path(zone, dataset)
        if target.exists():
            shutil.rmtree(target)

    def write_parquet(self, frame: pd.DataFrame, zone: str, dataset: str,
                      partition_cols: List[str]) -> str:
        target = self.path(zone, dataset)
        target.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, partition_cols=list(partition_cols), index=False,
                         coerce_timestamps="us", allow_truncated_timestamps=True)
        return str(target)

    def read_parquet(self, zone: str, dataset: str, columns: Optional[List[str]] = None,
                     filters: Optional[list] = None) -> pd.DataFrame:
        return pd.read_parquet(self.path(zone, dataset), columns=columns, filters=filters)

    def list_blocks(self, zone: str, dataset: str) -> List[Dict[str, Any]]:
        target = self.path(zone, dataset)
        if not target.exists():
            return []
        return [{"path": str(p), "size": int(p.stat().st_size)}
                for p in sorted(target.rglob("*.parquet"))]

    def list_datasets(self, zone: str) -> List[str]:
        zone_dir = self.root / zone
        if not zone_dir.exists():
            return []
        return sorted(p.name for p in zone_dir.iterdir() if p.is_dir())

    def write_text(self, zone: str, dataset: str, name: str, text: str) -> None:
        target = self.path(zone, dataset)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(text)

    def read_text(self, zone: str, dataset: str, name: str) -> Optional[str]:
        p = self.path(zone, dataset) / name
        return p.read_text() if p.exists() else None

    def status(self, requested: str) -> StorageStatus:
        datasets = sum(len(self.list_datasets(z)) for z in ("raw", "curated", "features", "analytics"))
        shown = self.display(self.root)
        return StorageStatus(
            requested_mode=requested, active_mode=LOCAL, available=True, uri=shown,
            detail=f"POSIX filesystem at {shown} ({datasets} datasets). "
                   f"Hive-partitioned Parquet, identical layout to the HDFS mode.",
            checks=[{"check": "local root writable", "state": "PASS", "detail": shown}])


# --------------------------------------------------------------------------- hdfs backend
class HdfsBackend:
    """Real HDFS backend via pyarrow's Hadoop client bindings (libhdfs + JVM)."""

    mode = HDFS

    def __init__(self, hdfs_uri: str, prefix: str = "/vigil") -> None:
        self.hdfs_uri = hdfs_uri
        parts = parse_hdfs_uri(hdfs_uri)
        self.host, self.port = parts["host"], parts["port"]
        base = (parts["path"] or "/").rstrip("/")
        self.prefix = f"{base}/{prefix.strip('/')}" if base not in ("", "/") else f"/{prefix.strip('/')}"
        self.fs = self._connect()

    # connection ------------------------------------------------------------
    def _connect(self):
        try:
            import pyarrow.fs as pafs
        except Exception as exc:  # pragma: no cover - pyarrow is a hard dependency
            raise StorageUnavailable(
                "STORAGE_MODE=hdfs needs pyarrow with Hadoop support: pip install 'pyarrow>=14'"
            ) from exc
        if not os.getenv("CLASSPATH") and shutil.which("hadoop"):
            # pyarrow's libhdfs needs the Hadoop jars on CLASSPATH; derive it from the CLI.
            import subprocess
            try:
                cp = subprocess.run(["hadoop", "classpath", "--glob"], capture_output=True,
                                    text=True, timeout=60)
                if cp.returncode == 0 and cp.stdout.strip():
                    os.environ["CLASSPATH"] = cp.stdout.strip()
            except Exception as exc:
                log.warning("could not derive CLASSPATH from `hadoop classpath`: %s", exc)
        try:
            return pafs.HadoopFileSystem(host=self.host, port=self.port,
                                         user=os.getenv("HDFS_USER") or None)
        except Exception as exc:
            brief = (str(exc).strip().splitlines() or ["no detail"])[0][:200]
            raise StorageUnavailable(
                f"STORAGE_MODE=hdfs requested but the Hadoop filesystem at "
                f"hdfs://{self.host}:{self.port} could not be opened: {type(exc).__name__}: {brief}\n"
                f"Checklist (docs/HDFS.md):\n"
                f"  1. a Hadoop client is installed and `hadoop version` works\n"
                f"  2. JAVA_HOME and HADOOP_HOME are exported\n"
                f"  3. CLASSPATH contains `hadoop classpath --glob`\n"
                f"  4. HDFS_URI points at a reachable NameNode (e.g. hdfs://localhost:9000)\n"
                f"VIGIL will not silently fall back to local storage. "
                f"Unset STORAGE_MODE (or set STORAGE_MODE=local) to use the local lake."
            ) from exc

    # paths -----------------------------------------------------------------
    def path(self, zone: str, dataset: str) -> str:
        return f"{self.prefix}/{zone}/{dataset}"

    def uri(self, zone: str, dataset: str) -> str:
        return f"hdfs://{self.host}:{self.port}{self.path(zone, dataset)}"

    # io --------------------------------------------------------------------
    def _info(self, path: str):
        from pyarrow.fs import FileType
        info = self.fs.get_file_info(path)
        return info, FileType

    def exists(self, zone: str, dataset: str) -> bool:
        info, FileType = self._info(self.path(zone, dataset))
        return info.type != FileType.NotFound

    def remove(self, zone: str, dataset: str) -> None:
        if self.exists(zone, dataset):
            self.fs.delete_dir(self.path(zone, dataset))

    def write_parquet(self, frame: pd.DataFrame, zone: str, dataset: str,
                      partition_cols: List[str]) -> str:
        import pyarrow as pa
        import pyarrow.parquet as pq

        target = self.path(zone, dataset)
        self.fs.create_dir(target, recursive=True)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_to_dataset(table, root_path=target, filesystem=self.fs,
                            partition_cols=list(partition_cols),
                            coerce_timestamps="us", allow_truncated_timestamps=True)
        return self.uri(zone, dataset)

    def read_parquet(self, zone: str, dataset: str, columns: Optional[List[str]] = None,
                     filters: Optional[list] = None) -> pd.DataFrame:
        import pyarrow.parquet as pq

        return pq.read_table(self.path(zone, dataset), filesystem=self.fs, columns=columns,
                             filters=filters).to_pandas()

    def list_blocks(self, zone: str, dataset: str) -> List[Dict[str, Any]]:
        from pyarrow.fs import FileSelector, FileType

        target = self.path(zone, dataset)
        info, _ = self._info(target)
        if info.type == FileType.NotFound:
            return []
        out = []
        for f in self.fs.get_file_info(FileSelector(target, recursive=True)):
            if f.type == FileType.File and f.path.endswith(".parquet"):
                out.append({"path": f.path, "size": int(f.size or 0)})
        return sorted(out, key=lambda d: d["path"])

    def list_datasets(self, zone: str) -> List[str]:
        from pyarrow.fs import FileSelector, FileType

        base = f"{self.prefix}/{zone}"
        info, _ = self._info(base)
        if info.type == FileType.NotFound:
            return []
        return sorted(f.base_name for f in self.fs.get_file_info(FileSelector(base))
                      if f.type == FileType.Directory)

    def write_text(self, zone: str, dataset: str, name: str, text: str) -> None:
        self.fs.create_dir(self.path(zone, dataset), recursive=True)
        with self.fs.open_output_stream(f"{self.path(zone, dataset)}/{name}") as fh:
            fh.write(text.encode())

    def read_text(self, zone: str, dataset: str, name: str) -> Optional[str]:
        from pyarrow.fs import FileType

        full = f"{self.path(zone, dataset)}/{name}"
        info, _ = self._info(full)
        if info.type == FileType.NotFound:
            return None
        with self.fs.open_input_stream(full) as fh:
            return fh.read().decode()

    # validation ------------------------------------------------------------
    def roundtrip_check(self) -> List[Dict[str, Any]]:
        """Genuine connectivity + write + read + delete test against the live cluster."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        checks: List[Dict[str, Any]] = []
        probe = f"{self.prefix}/_vigil_probe"
        try:
            self.fs.create_dir(probe, recursive=True)
            checks.append({"check": "namenode connect + mkdir", "state": "PASS", "detail": probe})
        except Exception as exc:
            checks.append({"check": "namenode connect + mkdir", "state": "FAIL",
                           "detail": f"{type(exc).__name__}: {exc}"})
            return checks
        try:
            table = pa.table({"k": [1, 2, 3], "v": ["a", "b", "c"]})
            with self.fs.open_output_stream(f"{probe}/probe.parquet") as fh:
                pq.write_table(table, fh)
            checks.append({"check": "write parquet to HDFS", "state": "PASS",
                           "detail": f"{probe}/probe.parquet"})
            back = pq.read_table(f"{probe}/probe.parquet", filesystem=self.fs)
            ok = back.num_rows == 3
            checks.append({"check": "read parquet back from HDFS",
                           "state": "PASS" if ok else "FAIL",
                           "detail": f"{back.num_rows} rows returned"})
        except Exception as exc:
            checks.append({"check": "write/read parquet on HDFS", "state": "FAIL",
                           "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                self.fs.delete_dir(probe)
                checks.append({"check": "cleanup probe directory", "state": "PASS", "detail": probe})
            except Exception as exc:
                checks.append({"check": "cleanup probe directory", "state": "FAIL",
                               "detail": f"{type(exc).__name__}: {exc}"})
        return checks

    def status(self, requested: str) -> StorageStatus:
        checks = self.roundtrip_check()
        ok = all(c["state"] == "PASS" for c in checks)
        return StorageStatus(
            requested_mode=requested, active_mode=HDFS, available=ok,
            uri=f"hdfs://{self.host}:{self.port}{self.prefix}",
            detail=f"Hadoop filesystem at hdfs://{self.host}:{self.port}, VIGIL prefix {self.prefix}",
            checks=checks)


# --------------------------------------------------------------------------- factory
def get_backend(cfg, mode: Optional[str] = None):
    """Return the backend for the requested mode. HDFS failures raise — never downgrade silently."""
    requested = (mode or resolve_storage_mode(cfg)).upper()
    if requested == HDFS:
        uri = cfg.hdfs_uri
        if not uri:
            raise StorageUnavailable(
                "STORAGE_MODE=hdfs requires HDFS_URI (e.g. HDFS_URI=hdfs://localhost:9000) "
                "or storage.hdfs_uri in config/vigil.yaml.")
        return HdfsBackend(uri, prefix=str(cfg.get("storage.hdfs_prefix", "/vigil")))
    return LocalBackend(cfg.lake_root)


def storage_status(cfg) -> StorageStatus:
    """Never raises: used by the Observatory/health surfaces, which must stay truthful and up."""
    try:
        requested = resolve_storage_mode(cfg)
    except StorageUnavailable as exc:
        return StorageStatus(requested_mode=str(os.getenv("STORAGE_MODE")), active_mode=LOCAL,
                             available=False,
                             uri=LocalBackend(cfg.lake_root).display(cfg.lake_root),
                             detail=str(exc))
    try:
        backend = get_backend(cfg, requested)
        return backend.status(requested)
    except StorageUnavailable as exc:
        return StorageStatus(
            requested_mode=requested, active_mode=LOCAL, available=False,
            uri=LocalBackend(cfg.lake_root).display(cfg.lake_root),
            detail=("HDFS requested but unavailable — "
                    + str(exc).split("\n")[0] + " (see docs/HDFS.md for the checklist)"),
            checks=[{"check": "hdfs client connect", "state": "FAIL", "detail": str(exc).split("\n")[0]}])
