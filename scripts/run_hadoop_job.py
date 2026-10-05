#!/usr/bin/env python3
"""Hadoop Streaming entry point.

    python scripts/run_hadoop_job.py --status          # which engine is actually active
    python scripts/run_hadoop_job.py --validate        # compile + local mapper|sort|reducer test
    python scripts/run_hadoop_job.py --print-command   # the exact hadoop jar ... submission
    python scripts/run_hadoop_job.py --run             # submit to a real cluster (requires Hadoop)

`--run` refuses to do anything when no Hadoop client is reachable; it never simulates a cluster.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil.config import load_config  # noqa: E402
from vigil.mapreduce.hadoop_runner import (JOBS, command_string, detect_hadoop,  # noqa: E402
                                           mapreduce_status, run_hadoop_suite,
                                           validate_job_scripts)


def main() -> int:
    ap = argparse.ArgumentParser(description="VIGIL Hadoop Streaming MapReduce")
    ap.add_argument("--status", action="store_true", help="report the active MapReduce engine")
    ap.add_argument("--validate", action="store_true", help="validate the streaming scripts locally")
    ap.add_argument("--print-command", action="store_true", help="print the hadoop jar command")
    ap.add_argument("--run", action="store_true", help="submit the jobs to a real Hadoop cluster")
    args = ap.parse_args()
    if not any((args.status, args.validate, args.print_command, args.run)):
        args.status = True

    cfg = load_config()
    env = detect_hadoop(cfg)

    if args.status:
        st = mapreduce_status(cfg)
        print(f"{st['label']}  (requested: {st['requested_engine']})")
        print(f"  hadoop available : {st['hadoop_available']} — {st['hadoop_detail']}")
        print(f"  streaming scripts: "
              f"{'VALIDATED LOCALLY (not a Hadoop run)' if st['streaming_scripts_validated'] else 'INVALID'}")
        for c in st["hadoop_checks"]:
            print(f"    [{c['state']}] {c['check']}: {c['detail']}")

    if args.validate:
        ok = True
        for job in JOBS.values():
            res = validate_job_scripts(job)
            ok &= res["state"] == "PASS"
            print(f"[{res['state']}] {res['job']} — {res.get('note', '')}")
            for c in res["checks"]:
                print(f"    [{c['state']}] {c['check']}: {c['detail']}")
        if not ok:
            return 1

    if args.print_command:
        base = (env.hdfs_uri or "hdfs://<namenode>:9000").rstrip("/") + "/vigil/mapreduce"
        for job in JOBS.values():
            print(f"\n# {job.name}")
            print(command_string(job, f"{base}/input/{job.name}", f"{base}/output/{job.name}", env))

    if args.run:
        if not env.available:
            print(f"REFUSING TO RUN — no usable Hadoop client: {env.detail}", file=sys.stderr)
            print("VIGIL does not simulate Hadoop. See hadoop/README.md.", file=sys.stderr)
            return 2
        print(json.dumps(run_hadoop_suite(cfg), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
