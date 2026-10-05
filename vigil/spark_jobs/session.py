"""Spark session management with honest capability reporting and graceful degradation."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger

log = get_logger("vigil.spark.session")

_JDK_SEARCH = [
    os.getenv("JAVA_HOME", ""),
    "/usr/lib/jvm/java-17-openjdk-amd64",
    "/usr/lib/jvm/java-21-openjdk-amd64",
    str(Path.home() / ".cache" / "vigil-toolchain" / "jdk17"),
    "/opt/java/openjdk",
]


def _java_version(java_home: str) -> Optional[int]:
    exe = Path(java_home) / "bin" / "java"
    if not exe.exists():
        return None
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=20)
        text = (out.stderr or out.stdout).splitlines()[0]
        token = text.split('"')[1]
        major = int(token.split(".")[0]) if not token.startswith("1.") else int(token.split(".")[1])
        return major
    except Exception:
        return None


def resolve_java_home(min_major: int = 17) -> Tuple[Optional[str], str]:
    for candidate in _JDK_SEARCH:
        if not candidate:
            continue
        major = _java_version(candidate)
        if major and major >= min_major:
            return candidate, f"JDK {major} at {candidate}"
    sys_java = shutil.which("java")
    if sys_java:
        major = _java_version(str(Path(sys_java).parent.parent))
        if major and major >= min_major:
            return str(Path(sys_java).parent.parent), f"system JDK {major}"
        return None, f"system JDK {major} found but Spark 4 needs JDK >= {min_major}"
    return None, "no JDK found on PATH"


class SparkUnavailable(RuntimeError):
    pass


def get_spark(cfg: Optional[VigilConfig] = None, app: str = "vigil"):
    cfg = cfg or load_config()
    java_home, detail = resolve_java_home()
    if java_home is None:
        raise SparkUnavailable(f"Spark cannot start: {detail}")
    os.environ["JAVA_HOME"] = java_home
    os.environ["PATH"] = f"{java_home}/bin:" + os.environ.get("PATH", "")
    os.environ.setdefault("SPARK_LOCAL_DIRS", str(Path.home() / ".cache" / "sparktmp"))
    Path(os.environ["SPARK_LOCAL_DIRS"]).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYSPARK_PYTHON", os.sys.executable)
    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover
        raise SparkUnavailable(f"pyspark not installed ({exc})")
    spark = (SparkSession.builder
             .master(cfg.get("spark.master", "local[2]"))
             .appName(f"VIGIL::{app}")
             .config("spark.driver.memory", cfg.get("spark.driver_memory", "512m"))
             .config("spark.sql.shuffle.partitions", str(cfg.get("spark.shuffle_partitions", 4)))
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.ui.enabled", "false")
             .getOrCreate())
    spark.sparkContext.setLogLevel("ERROR")
    log.info("Spark %s started (%s)", spark.version, detail)
    return spark
