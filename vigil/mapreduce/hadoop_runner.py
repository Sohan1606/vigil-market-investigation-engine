"""Genuine Hadoop Streaming execution path for the VIGIL MapReduce jobs.

VIGIL has two MapReduce engines and always says which one produced a result:

  MAPREDUCE_ENGINE=local  (default)  vigil/mapreduce/framework.py — real map/shuffle/reduce in
                                     separate OS processes over the Parquet blocks of the lake
  MAPREDUCE_ENGINE=hadoop            `hadoop jar hadoop-streaming.jar` submitting the mappers and
                                     reducers in hadoop/ to a real cluster

When `hadoop` is requested but no Hadoop installation is reachable, nothing is faked: the engine
status becomes LOCAL FALLBACK, the constructed `hadoop jar ...` command line is reported verbatim,
and the mapper/reducer scripts are validated *statically and locally* (compile + `cat | mapper |
sort | reducer` on real sample records). That validation is reported as
SCRIPTS VALIDATED LOCALLY — NOT A HADOOP RUN, never as a successful Hadoop job.

See hadoop/README.md for the cluster setup.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger

log = get_logger("vigil.mapreduce.hadoop")

HADOOP_DIR = Path(__file__).resolve().parents[2] / "hadoop"
LOCAL = "LOCAL FALLBACK"
HADOOP = "HADOOP"


@dataclass
class StreamingJob:
    """Declarative description of one Hadoop Streaming job."""

    name: str
    mapper: str
    reducer: str
    combiner: Optional[str] = None
    num_reducers: int = 2
    input_dataset: str = "curated/ohlcv"
    sample: List[str] = field(default_factory=list)

    def mapper_path(self) -> Path:
        return HADOOP_DIR / self.mapper

    def reducer_path(self) -> Path:
        return HADOOP_DIR / self.reducer


JOBS: Dict[str, StreamingJob] = {
    "symbol_year_profile": StreamingJob(
        name="symbol_year_profile",
        mapper="mapper_symbol_year.py",
        reducer="reducer_symbol_year.py",
        combiner="reducer_symbol_year.py",
        input_dataset="curated/ohlcv",
        sample=["2024-01-01\tTCS.NS\t100\t110\t95\t105\t1000",
                "2024-01-02\tTCS.NS\t105\t120\t101\t118\t2000",
                "2023-05-02\tINFY.NS\t50\t52\t49\t51\t500"],
    ),
    "market_breadth": StreamingJob(
        name="market_breadth",
        mapper="mapper_breadth.py",
        reducer="reducer_breadth.py",
        input_dataset="curated/ohlcv",
        sample=["2024-01-01\tTCS.NS\t100\t110\t95\t105\t1000",
                "2024-01-01\tINFY.NS\t50\t52\t49\t48\t500",
                "2024-01-02\tTCS.NS\t105\t120\t101\t118\t2000"],
    ),
    "news_inverted_index": StreamingJob(
        name="news_inverted_index",
        mapper="mapper_news_terms.py",
        reducer="reducer_news_terms.py",
        input_dataset="curated/news_text",
        sample=["n1\tTCS.NS\tTCS wins large deal in Europe",
                "n2\tINFY.NS\tInfosys raises guidance after deal wins"],
    ),
}


# --------------------------------------------------------------------------- environment
@dataclass
class HadoopEnvironment:
    available: bool
    hadoop_bin: Optional[str]
    streaming_jar: Optional[str]
    java_home: Optional[str]
    version: Optional[str]
    hdfs_uri: Optional[str]
    detail: str
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available, "hadoop_bin": self.hadoop_bin,
            "streaming_jar": self.streaming_jar, "java_home": self.java_home,
            "version": self.version, "hdfs_uri": self.hdfs_uri, "detail": self.detail,
            "checks": self.checks,
        }


def find_streaming_jar() -> Optional[str]:
    """HADOOP_STREAMING_JAR wins; otherwise search the standard tools directory."""
    env = os.getenv("HADOOP_STREAMING_JAR")
    if env and Path(env).exists():
        return env
    home = os.getenv("HADOOP_HOME")
    roots = [Path(home)] if home else []
    roots += [Path("/usr/lib/hadoop-mapreduce"), Path("/opt/hadoop"), Path("/usr/local/hadoop")]
    for root in roots:
        if not root.exists():
            continue
        for jar in sorted(root.rglob("hadoop-streaming*.jar")):
            return str(jar)
    return None


def detect_hadoop(cfg: Optional[VigilConfig] = None) -> HadoopEnvironment:
    """Probe the machine for a usable Hadoop client. Never raises, never guesses."""
    cfg = cfg or load_config()
    checks: List[Dict[str, Any]] = []
    hadoop_bin = shutil.which("hadoop") or (
        str(Path(os.environ["HADOOP_HOME"]) / "bin" / "hadoop")
        if os.getenv("HADOOP_HOME") and (Path(os.environ["HADOOP_HOME"]) / "bin" / "hadoop").exists()
        else None)
    checks.append({"check": "hadoop binary on PATH/HADOOP_HOME",
                   "state": "PASS" if hadoop_bin else "FAIL",
                   "detail": hadoop_bin or "not found"})
    version = None
    if hadoop_bin:
        try:
            out = subprocess.run([hadoop_bin, "version"], capture_output=True, text=True, timeout=60)
            version = (out.stdout.strip().splitlines() or [""])[0] or None
            checks.append({"check": "`hadoop version` runs", "state": "PASS" if version else "FAIL",
                           "detail": version or out.stderr.strip()[:160]})
        except Exception as exc:
            checks.append({"check": "`hadoop version` runs", "state": "FAIL",
                           "detail": f"{type(exc).__name__}: {exc}"})
    jar = find_streaming_jar()
    checks.append({"check": "hadoop-streaming jar located",
                   "state": "PASS" if jar else "FAIL",
                   "detail": jar or "set HADOOP_STREAMING_JAR=/path/to/hadoop-streaming-<ver>.jar"})
    java_home = os.getenv("JAVA_HOME") or (str(Path(shutil.which("java")).resolve().parents[1])
                                           if shutil.which("java") else None)
    checks.append({"check": "JAVA_HOME / java available", "state": "PASS" if java_home else "FAIL",
                   "detail": java_home or "no JVM found"})
    hdfs_uri = cfg.hdfs_uri
    checks.append({"check": "HDFS_URI configured", "state": "PASS" if hdfs_uri else "FAIL",
                   "detail": hdfs_uri or "HDFS_URI not set (required to stage streaming input)"})
    available = bool(hadoop_bin and version and jar and java_home and hdfs_uri)
    detail = ("Hadoop client usable" if available else
              "Hadoop client not usable on this machine — " +
              ", ".join(c["check"] for c in checks if c["state"] == "FAIL"))
    return HadoopEnvironment(available=available, hadoop_bin=hadoop_bin, streaming_jar=jar,
                             java_home=java_home, version=version, hdfs_uri=hdfs_uri,
                             detail=detail, checks=checks)


def resolve_engine() -> str:
    """MAPREDUCE_ENGINE=local|hadoop, default local."""
    raw = (os.getenv("MAPREDUCE_ENGINE") or "local").strip().lower()
    if raw not in ("local", "hadoop"):
        raise ValueError(f"MAPREDUCE_ENGINE='{raw}' is not valid (use local or hadoop)")
    return raw


# --------------------------------------------------------------------------- command building
def build_streaming_command(job: StreamingJob, input_path: str, output_path: str,
                            env: HadoopEnvironment) -> List[str]:
    """The exact `hadoop jar ... hadoop-streaming.jar ...` argv used for a real submission."""
    cmd = [env.hadoop_bin or "hadoop", "jar", env.streaming_jar or "$HADOOP_STREAMING_JAR",
           "-D", f"mapreduce.job.name=VIGIL::{job.name}",
           "-D", f"mapreduce.job.reduces={job.num_reducers}",
           "-D", "mapreduce.output.fileoutputformat.compress=false",
           "-files", f"hadoop/{job.mapper},hadoop/{job.reducer}",
           "-input", input_path,
           "-output", output_path,
           "-mapper", f"python3 {job.mapper_path().name}",
           "-reducer", f"python3 {job.reducer_path().name}"]
    if job.combiner:
        cmd += ["-combiner", f"python3 {Path(job.combiner).name}"]
    return cmd


def command_string(job: StreamingJob, input_path: str, output_path: str,
                   env: HadoopEnvironment) -> str:
    return " ".join(shlex.quote(p) for p in build_streaming_command(job, input_path, output_path, env))


# --------------------------------------------------------------------------- static validation
def validate_job_scripts(job: StreamingJob) -> Dict[str, Any]:
    """Compile the scripts and run `sample | mapper | sort | reducer` in-process.

    This proves the streaming contract (stdin TSV → stdout TSV, sorted-key grouping) without a
    cluster. It is explicitly NOT a Hadoop execution and is reported as such.
    """
    checks: List[Dict[str, Any]] = []
    for role, path in (("mapper", job.mapper_path()), ("reducer", job.reducer_path())):
        exists = path.exists()
        # repo-relative, so the recorded evidence is portable across machines
        checks.append({"check": f"{role} script present", "state": "PASS" if exists else "FAIL",
                       "detail": f"hadoop/{path.name}"})
        if not exists:
            return {"job": job.name, "state": "FAIL", "checks": checks}
        try:
            subprocess.run([sys.executable, "-m", "py_compile", str(path)], check=True,
                           capture_output=True, timeout=60)
            checks.append({"check": f"{role} compiles", "state": "PASS", "detail": path.name})
        except subprocess.CalledProcessError as exc:
            checks.append({"check": f"{role} compiles", "state": "FAIL",
                           "detail": exc.stderr.decode()[:200]})
            return {"job": job.name, "state": "FAIL", "checks": checks}
    sample = "\n".join(job.sample) + "\n"
    try:
        mapped = subprocess.run([sys.executable, str(job.mapper_path())], input=sample,
                                capture_output=True, text=True, timeout=60, check=True).stdout
        lines = [l for l in mapped.splitlines() if l.strip()]
        checks.append({"check": "mapper emits key<TAB>value pairs",
                       "state": "PASS" if lines and all("\t" in l for l in lines) else "FAIL",
                       "detail": f"{len(lines)} pairs from {len(job.sample)} sample records"})
        shuffled = "\n".join(sorted(lines)) + "\n"
        reduced = subprocess.run([sys.executable, str(job.reducer_path())], input=shuffled,
                                 capture_output=True, text=True, timeout=60, check=True).stdout
        rlines = [l for l in reduced.splitlines() if l.strip()]
        checks.append({"check": "reducer consumes sorted stream and emits records",
                       "state": "PASS" if rlines else "FAIL",
                       "detail": f"{len(rlines)} output records"})
    except subprocess.CalledProcessError as exc:
        checks.append({"check": "mapper|sort|reducer pipeline", "state": "FAIL",
                       "detail": (exc.stderr or "")[:200]})
    state = "PASS" if all(c["state"] == "PASS" for c in checks) else "FAIL"
    return {"job": job.name, "state": state, "checks": checks,
            "note": "SCRIPTS VALIDATED LOCALLY — NOT A HADOOP RUN"}


# --------------------------------------------------------------------------- input staging
def export_streaming_input(cfg: VigilConfig, job: StreamingJob, out_dir: Path) -> Path:
    """Materialise the job input as the TSV that Hadoop Streaming consumes."""
    import pandas as pd

    from ..storage.lake import DataLake

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{job.name}.tsv"
    if job.input_dataset == "curated/news_text":
        from ..storage.docstore import DocumentStore

        docs = DocumentStore(cfg).find("news")
        rows = [(d.get("news_id", ""), d.get("symbol", ""),
                 str(d.get("headline", "")).replace("\t", " ").replace("\n", " "))
                for d in docs]
        pd.DataFrame(rows, columns=["news_id", "symbol", "headline"]).to_csv(
            target, sep="\t", index=False, header=False)
        return target
    lake = DataLake(cfg)
    zone, dataset = job.input_dataset.split("/")
    df = lake.read(zone, dataset, columns=["date", "symbol", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(target, sep="\t", index=False, header=False)
    return target


# --------------------------------------------------------------------------- execution
def run_streaming_job(job: StreamingJob, cfg: Optional[VigilConfig] = None,
                      env: Optional[HadoopEnvironment] = None) -> Dict[str, Any]:
    """Submit one job to a real cluster. Raises if Hadoop is not genuinely available."""
    cfg = cfg or load_config()
    env = env or detect_hadoop(cfg)
    if not env.available:
        raise RuntimeError(
            f"MAPREDUCE_ENGINE=hadoop requested but no usable Hadoop client was found: {env.detail}. "
            f"VIGIL does not simulate Hadoop — use MAPREDUCE_ENGINE=local for the local engine, "
            f"or see hadoop/README.md to provision a cluster.")
    base = f"{str(env.hdfs_uri).rstrip('/')}{cfg.get('storage.hdfs_prefix', '/vigil')}/mapreduce"
    hdfs_input = f"{base}/input/{job.name}"
    hdfs_output = f"{base}/output/{job.name}"
    staged = export_streaming_input(cfg, job, cfg.processed_root / "hadoop_input")
    subprocess.run([env.hadoop_bin, "fs", "-rm", "-r", "-f", hdfs_output], check=False,
                   capture_output=True, timeout=300)
    subprocess.run([env.hadoop_bin, "fs", "-mkdir", "-p", hdfs_input], check=True,
                   capture_output=True, timeout=300)
    subprocess.run([env.hadoop_bin, "fs", "-put", "-f", str(staged), hdfs_input], check=True,
                   capture_output=True, timeout=1800)
    cmd = build_streaming_command(job, hdfs_input, hdfs_output, env)
    log.info("submitting Hadoop Streaming job: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    result = {
        "job": job.name, "engine": HADOOP, "command": command_string(job, hdfs_input, hdfs_output, env),
        "returncode": proc.returncode, "hdfs_input": hdfs_input, "hdfs_output": hdfs_output,
        "stderr_tail": proc.stderr.strip().splitlines()[-25:],
    }
    if proc.returncode != 0:
        result["state"] = "FAIL"
        return result
    counters = [l.strip() for l in proc.stderr.splitlines()
                if "Map input records" in l or "Reduce output records" in l
                or "Map output records" in l or "Combine output records" in l]
    result["counters"] = counters
    cat = subprocess.run([env.hadoop_bin, "fs", "-cat", f"{hdfs_output}/part-*"],
                         capture_output=True, text=True, timeout=1800)
    rows = [l for l in cat.stdout.splitlines() if l.strip()]
    result["output_records"] = len(rows)
    result["sample_output"] = rows[:5]
    result["state"] = "PASS"
    return result


def run_hadoop_suite(cfg: Optional[VigilConfig] = None) -> Dict[str, Any]:
    """Run every streaming job on a real cluster (engine=hadoop) and persist honest status."""
    cfg = cfg or load_config()
    env = detect_hadoop(cfg)
    out = {"engine": HADOOP if env.available else LOCAL, "environment": env.to_dict(), "jobs": []}
    if not env.available:
        raise RuntimeError(f"no usable Hadoop client: {env.detail}")
    for job in JOBS.values():
        out["jobs"].append(run_streaming_job(job, cfg, env))
    (cfg.reports_root / "results" / "hadoop_jobs.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- status surface
def mapreduce_status(cfg: Optional[VigilConfig] = None) -> Dict[str, Any]:
    """Truthful engine status for the Observatory, health, self-audit and release audit."""
    cfg = cfg or load_config()
    try:
        requested = resolve_engine()
    except ValueError as exc:
        requested = "local"
        log.warning("%s", exc)
    env = detect_hadoop(cfg)
    active = HADOOP if (requested == "hadoop" and env.available) else LOCAL
    validations = [validate_job_scripts(job) for job in JOBS.values()]
    scripts_ok = all(v["state"] == "PASS" for v in validations)
    if active == HADOOP:
        detail = f"Hadoop Streaming on {env.version} via {env.streaming_jar}"
    elif requested == "hadoop":
        detail = (f"MAPREDUCE_ENGINE=hadoop requested but unavailable ({env.detail}); "
                  f"running the local engine. Streaming scripts validated locally — not a Hadoop run.")
    else:
        detail = ("Local MapReduce engine (vigil/mapreduce/framework.py): real splits, shuffle and "
                  "reduce in separate OS processes. Hadoop Streaming scripts are present and "
                  "validated locally; set MAPREDUCE_ENGINE=hadoop with a cluster to submit them.")
    sample_job = JOBS["symbol_year_profile"]
    return {
        "requested_engine": requested.upper(),
        "engine": active,
        "label": f"MapReduce Engine: {active}",
        "hadoop_available": env.available,
        "hadoop_detail": env.detail,
        "hadoop_checks": env.checks,
        "streaming_scripts_validated": scripts_ok,
        "streaming_validation": validations,
        "example_command": command_string(
            sample_job,
            f"{str(env.hdfs_uri or 'hdfs://<namenode>:9000').rstrip('/')}/vigil/mapreduce/input/{sample_job.name}",
            f"{str(env.hdfs_uri or 'hdfs://<namenode>:9000').rstrip('/')}/vigil/mapreduce/output/{sample_job.name}",
            env),
        "detail": detail,
    }
