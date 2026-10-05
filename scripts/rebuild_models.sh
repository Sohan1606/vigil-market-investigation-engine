#!/usr/bin/env bash
# Re-run the model-and-research half of the pipeline, ONE OS PROCESS PER STAGE.
# Rationale: the stages are memory-heavy (sequence models build 3-D tensors); running them in a
# single long-lived process on a 2 GB machine was killed by the OOM reaper. One process per stage
# returns all memory to the OS between stages and is otherwise identical in behaviour.
set -u
cd "$(dirname "$0")/.."
for stage in models ensemble calibration uncertainty drift backtest decision_quality forecasts failure_lab experiments report; do
  echo "=== $stage ==="
  python3 scripts/run_pipeline.py --stage "$stage" --horizons 1 --offline || echo "STAGE $stage FAILED rc=$?"
done
