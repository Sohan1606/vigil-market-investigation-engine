"""Hadoop Streaming tests (v1.0 defect #2).

No cluster is required. What is verified here is exactly what can be verified without one:
the mappers/reducers really implement the streaming contract, the submission command is
constructed correctly, and the engine status never claims HADOOP when Hadoop is absent.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from vigil.mapreduce.hadoop_runner import (HADOOP, JOBS, LOCAL, build_streaming_command,
                                           command_string, detect_hadoop, mapreduce_status,
                                           resolve_engine, validate_job_scripts)


def _pipe(job, records):
    """cat records | mapper | sort | reducer — the Unix equivalent of a streaming job."""
    mapped = subprocess.run([sys.executable, str(job.mapper_path())],
                            input="\n".join(records) + "\n", capture_output=True,
                            text=True, check=True).stdout
    shuffled = "\n".join(sorted(l for l in mapped.splitlines() if l.strip())) + "\n"
    return subprocess.run([sys.executable, str(job.reducer_path())], input=shuffled,
                          capture_output=True, text=True, check=True).stdout


def test_every_streaming_script_validates():
    for job in JOBS.values():
        res = validate_job_scripts(job)
        assert res["state"] == "PASS", res
        assert "NOT A HADOOP RUN" in res["note"]


def test_symbol_year_reducer_aggregates_correctly():
    job = JOBS["symbol_year_profile"]
    out = _pipe(job, ["2024-01-01\tTCS.NS\t100\t110\t95\t100\t1000",
                      "2024-01-02\tTCS.NS\t100\t130\t90\t200\t3000"])
    rows = [l.split("\t") for l in out.splitlines()]
    assert len(rows) == 1
    symbol, year, sessions, avg_close, total_volume, hi, lo, _rng = rows[0]
    assert symbol == "TCS.NS" and year == "2024" and sessions == "2"
    assert float(avg_close) == pytest.approx(150.0)
    assert float(total_volume) == pytest.approx(4000.0)
    assert float(hi) == pytest.approx(130.0) and float(lo) == pytest.approx(90.0)


def test_breadth_reducer_counts_advancers_and_decliners():
    job = JOBS["market_breadth"]
    out = _pipe(job, ["2024-01-01\tA\t100\t0\t0\t105\t10",
                      "2024-01-01\tB\t100\t0\t0\t95\t20",
                      "2024-01-01\tC\t100\t0\t0\t100\t30"])
    date, adv, dec, unch, breadth, vol = out.strip().split("\t")
    assert (adv, dec, unch) == ("1", "1", "1")
    assert float(breadth) == pytest.approx(33.33, abs=0.01)
    assert float(vol) == pytest.approx(60.0)


def test_inverted_index_groups_terms_across_symbols():
    job = JOBS["news_inverted_index"]
    out = _pipe(job, ["n1\tTCS.NS\tdeal wins in Europe", "n2\tINFY.NS\tdeal signed in Europe"])
    index = {l.split("\t")[0]: l.split("\t") for l in out.splitlines()}
    assert "deal" in index
    _term, df, symbol_count, symbols = index["deal"]
    assert df == "2" and symbol_count == "2"
    assert set(symbols.split(",")) == {"TCS.NS", "INFY.NS"}
    assert "the" not in index, "stop words must not be indexed"


def test_submission_command_is_well_formed():
    job = JOBS["symbol_year_profile"]
    env = detect_hadoop()
    cmd = build_streaming_command(job, "hdfs://nn:9000/in", "hdfs://nn:9000/out", env)
    assert cmd[1] == "jar"
    for flag in ("-input", "-output", "-mapper", "-reducer", "-files", "-combiner"):
        assert flag in cmd, flag
    assert cmd[cmd.index("-input") + 1] == "hdfs://nn:9000/in"
    assert cmd[cmd.index("-output") + 1] == "hdfs://nn:9000/out"
    rendered = command_string(job, "hdfs://nn:9000/in", "hdfs://nn:9000/out", env)
    assert "hadoop/mapper_symbol_year.py" in rendered
    assert "/home/" not in rendered, "no machine-specific path may appear in the command"


def test_engine_default_is_local(monkeypatch):
    monkeypatch.delenv("MAPREDUCE_ENGINE", raising=False)
    assert resolve_engine() == "local"


def test_status_never_claims_hadoop_without_hadoop(monkeypatch):
    monkeypatch.setenv("MAPREDUCE_ENGINE", "hadoop")
    st = mapreduce_status()
    env = detect_hadoop()
    if env.available:
        assert st["engine"] == HADOOP
    else:
        assert st["engine"] == LOCAL
        assert st["label"] == "MapReduce Engine: LOCAL FALLBACK"
        assert "not a hadoop run" in st["detail"].lower()


def test_hadoop_suite_refuses_to_fabricate_a_run(monkeypatch):
    monkeypatch.setenv("MAPREDUCE_ENGINE", "hadoop")
    if detect_hadoop().available:
        pytest.skip("a real Hadoop client is present; the refusal path cannot be exercised")
    from vigil.mapreduce.jobs import run_mapreduce_suite

    with pytest.raises(RuntimeError) as exc:
        run_mapreduce_suite()
    assert "will not fabricate" in str(exc.value).lower()
