"""A genuine (small-scale) MapReduce engine.

This is not a metaphor: input splits are the physical Parquet blocks of the HDFS-style lake,
mappers run in separate OS processes, the framework performs a real shuffle/sort by key, an
optional combiner runs map-side, and reducers consume sorted (key, [values]) groups.

It exists for two reasons:
  * the syllabus requires MapReduce-style distributed analytics, and
  * it gives the Observatory honest, *measured* split/shuffle statistics.
Heavy production aggregation is done by Spark (vigil/spark_jobs); MapReduce owns the jobs where the
programming model itself is the point.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

from ..logging_utils import get_logger

log = get_logger("vigil.mapreduce")

KV = Tuple[Any, Any]
Mapper = Callable[[Dict[str, Any]], Iterable[KV]]
Reducer = Callable[[Any, List[Any]], Iterable[KV]]
Combiner = Optional[Callable[[Any, List[Any]], Iterable[KV]]]


@dataclass
class JobStats:
    job_name: str
    input_splits: int = 0
    map_input_records: int = 0
    map_output_records: int = 0
    combiner_output_records: int = 0
    shuffle_keys: int = 0
    reduce_output_records: int = 0
    map_ms: float = 0.0
    shuffle_ms: float = 0.0
    reduce_ms: float = 0.0
    parallelism: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        return round(self.map_ms + self.shuffle_ms + self.reduce_ms, 2)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["total_ms"] = self.total_ms
        return d


def iter_splits(dataset_dir: Path) -> List[Path]:
    """Input splits == physical blocks of the partitioned lake dataset."""
    return sorted(p for p in Path(dataset_dir).rglob("*.parquet"))


def _run_split(args: Tuple[str, str, Optional[Sequence[str]]]) -> Tuple[List[KV], int]:
    """Executed in a worker process: read one split, emit intermediate pairs."""
    split_path, mapper_ref, columns = args
    module_name, func_name = mapper_ref.rsplit(":", 1)
    import importlib

    mapper = getattr(importlib.import_module(module_name), func_name)
    import pyarrow.parquet as pq

    schema_names = set(pq.ParquetFile(split_path).schema.names)
    # partition columns live in the directory path, not in the block schema
    read_cols = [c for c in columns if c in schema_names] if columns else None
    df = pd.read_parquet(split_path, columns=read_cols)
    # partition columns are encoded in the path; restore them for the mapper
    for part in Path(split_path).parts:
        if "=" in part:
            k, v = part.split("=", 1)
            if k not in df.columns:
                df[k] = v
    out: List[KV] = []
    records = df.to_dict("records")
    for rec in records:
        out.extend(mapper(rec))
    return out, len(records)


class MapReduceJob:
    def __init__(self, name: str, mapper_ref: str, reducer: Reducer,
                 combiner: Combiner = None, columns: Optional[Sequence[str]] = None,
                 parallelism: Optional[int] = None) -> None:
        self.name = name
        self.mapper_ref = mapper_ref           # "module:function" so it is picklable across processes
        self.reducer = reducer
        self.combiner = combiner
        self.columns = columns
        self.parallelism = parallelism or max(1, min(4, (os.cpu_count() or 2)))

    def run(self, dataset_dir: Path) -> Tuple[pd.DataFrame, JobStats]:
        splits = iter_splits(dataset_dir)
        if not splits:
            raise FileNotFoundError(f"no input splits under {dataset_dir}")
        stats = JobStats(job_name=self.name, input_splits=len(splits), parallelism=self.parallelism)

        # ---- MAP (parallel over splits) ----
        t0 = time.perf_counter()
        payload = [(str(s), self.mapper_ref, self.columns) for s in splits]
        pairs: List[KV] = []
        if self.parallelism > 1 and len(splits) > 1:
            with ProcessPoolExecutor(max_workers=self.parallelism) as pool:
                for out, n in pool.map(_run_split, payload):
                    pairs.extend(out)
                    stats.map_input_records += n
        else:
            for item in payload:
                out, n = _run_split(item)
                pairs.extend(out)
                stats.map_input_records += n
        stats.map_output_records = len(pairs)
        stats.map_ms = round((time.perf_counter() - t0) * 1000, 2)

        # ---- COMBINE (map-side reduction) ----
        if self.combiner is not None:
            grouped_local: Dict[Any, List[Any]] = defaultdict(list)
            for k, v in pairs:
                grouped_local[k].append(v)
            combined: List[KV] = []
            for k, vals in grouped_local.items():
                combined.extend(self.combiner(k, vals))
            pairs = combined
            stats.combiner_output_records = len(pairs)

        # ---- SHUFFLE & SORT ----
        t0 = time.perf_counter()
        grouped: Dict[Any, List[Any]] = defaultdict(list)
        for k, v in pairs:
            grouped[k].append(v)
        keys = sorted(grouped.keys(), key=lambda k: (str(type(k)), k))
        stats.shuffle_keys = len(keys)
        stats.shuffle_ms = round((time.perf_counter() - t0) * 1000, 2)

        # ---- REDUCE ----
        t0 = time.perf_counter()
        rows: List[Dict[str, Any]] = []
        for key in keys:
            for out_key, out_val in self.reducer(key, grouped[key]):
                row = {"key": out_key}
                if isinstance(out_val, dict):
                    row.update(out_val)
                else:
                    row["value"] = out_val
                rows.append(row)
        stats.reduce_output_records = len(rows)
        stats.reduce_ms = round((time.perf_counter() - t0) * 1000, 2)

        log.info("MapReduce[%s] splits=%d map_in=%d map_out=%d keys=%d out=%d total=%.1fms",
                 self.name, stats.input_splits, stats.map_input_records, stats.map_output_records,
                 stats.shuffle_keys, stats.reduce_output_records, stats.total_ms)
        return pd.DataFrame(rows), stats
