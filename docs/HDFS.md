# Storage modes: local lake vs real HDFS

VIGIL stores every analytical dataset as Hive-partitioned Parquet
(`<zone>/<dataset>/symbol=.../year=...`). The layout is identical in both modes; only the
filesystem changes.

| | `STORAGE_MODE=local` (default) | `STORAGE_MODE=hdfs` |
|---|---|---|
| Backend | `vigil/storage/backend.py::LocalBackend` (POSIX, pandas/pyarrow) | `HdfsBackend` → `pyarrow.fs.HadoopFileSystem` + `pyarrow.parquet.write_to_dataset(filesystem=...)` |
| Root | `data/lake` | `$HDFS_URI` + `storage.hdfs_prefix` (default `/vigil`) |
| Requirements | none — fully offline | Hadoop client, `JAVA_HOME`, `CLASSPATH=$(hadoop classpath --glob)`, reachable NameNode |
| Observatory label | `LOCAL DATA LAKE` | `HDFS DATA LAKE` |

**There is no silent fallback.** If `STORAGE_MODE=hdfs` is set and the Hadoop filesystem cannot be
opened, `DataLake(...)` raises `StorageUnavailable` with a remediation checklist. Read-only status
surfaces (`/api/health`, `/api/observatory`, the audits) call `storage_status()`, which does not
raise and reports `LOCAL DATA LAKE` plus the reason HDFS was unavailable. No surface may print
"HDFS operational" while the data is on POSIX.

## Setup

```bash
export STORAGE_MODE=hdfs
export HDFS_URI=hdfs://localhost:9000      # or hdfs://namenode-host:8020
export HDFS_USER=hadoop                    # optional
export CLASSPATH=$(hadoop classpath --glob)
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))
```

See `hadoop/README.md` for provisioning a single-node cluster.

## Validation

```bash
python scripts/storage_check.py
```

The checker reports the requested mode, the active mode and, for HDFS, a genuine round trip:
NameNode connect → `mkdir` → write a Parquet file → read it back → delete the probe directory.
Exit code `0` means the active mode matches the requested mode and all checks passed; `1` means
the requested mode is unavailable (and the message says exactly which check failed).

```bash
# rebuild the lake on HDFS once connectivity passes
STORAGE_MODE=hdfs HDFS_URI=hdfs://localhost:9000 python scripts/run_pipeline.py --stages ingest,quality,features,dataset
```

## Manifest URIs are portable

Every dataset carries `_vigil_manifest.json`. Its `uri` field is the **repo-relative POSIX path**
in local mode (`data/lake/curated/ohlcv`) and a genuine `hdfs://host:port/prefix/...` URI in HDFS
mode. v1.0.0 shipped absolute build-machine paths here; v1.0.1 fixed the writer, normalised the
13 shipped manifests and extended the release audit to scan generated metadata under `data/`
(binary Parquet is skipped by suffix, JSON metadata is not).

## Windows

Both modes work the same way on native Windows. The local document store no longer imports the
POSIX-only `fcntl` module — `vigil/storage/filelock.py` selects `fcntl` or `msvcrt.locking` at
import time — so `import vigil.api.app` succeeds on Windows with `STORAGE_MODE=local` and no
`MONGODB_URI`. HDFS mode on Windows additionally needs a working Hadoop client, `JAVA_HOME` and
`CLASSPATH`, exactly as on Linux.

## Known limitation

The published VIGIL artefacts in `data/lake/` were produced in `STORAGE_MODE=local`. The HDFS code
path is exercised by `tests/test_storage_modes.py` (mode resolution, URI construction, refusal to
downgrade, truthful status) but **has not been executed against a live NameNode in this
environment** — no Hadoop client is installed here. That is stated, not hidden.
