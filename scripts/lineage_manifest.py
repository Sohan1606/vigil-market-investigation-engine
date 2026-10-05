#!/usr/bin/env python3
"""Write a deterministic, CONTENT-based lineage manifest.

    python scripts/lineage_manifest.py

Hashes the bytes of every lake dataset, every result artefact and every module of the `vigil`
package (BLAKE2b, see vigil/mlops/fingerprints.py), producing
reports/results/lineage_manifest.json. Running it twice on an unchanged repo yields an identical
file; changing one byte of one file changes exactly one digest and the roll-up.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vigil.config import load_config  # noqa: E402
from vigil.mlops.fingerprints import manifest, tree_fingerprint  # noqa: E402
from vigil.storage.lake import ZONES, DataLake  # noqa: E402


def main() -> int:
    cfg = load_config()
    lake = DataLake(cfg)
    if lake.is_hdfs:
        print("lineage_manifest currently fingerprints the local lake only; "
              "run it with STORAGE_MODE=local", file=sys.stderr)
        return 2
    datasets = []
    for zone in ZONES:
        for name in lake.backend.list_datasets(zone):
            path = lake.dataset_path(zone, name)
            blocks = manifest(path, ("*.parquet",))
            datasets.append({
                "zone": zone, "dataset": name, "blocks": len(blocks),
                "bytes": sum(int(b["bytes"]) for b in blocks),
                "content_fingerprint": tree_fingerprint(path, ("*.parquet",)),
            })
    results_dir = cfg.reports_root / "results"
    artefacts = [a for a in manifest(results_dir, ("*.json",))
                 if a["path"] != "lineage_manifest.json"]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "BLAKE2b-64 over file CONTENT, manifests sorted by repo-relative path",
        "code_fingerprint": tree_fingerprint(ROOT / "vigil", ("*.py",)),
        "frontend_fingerprint": tree_fingerprint(ROOT / "apps" / "web", ("*.js", "*.html", "*.css")),
        "config_fingerprint": tree_fingerprint(ROOT / "config", ("*.yaml",)),
        "datasets": datasets,
        "result_artefacts": artefacts,
    }
    path = results_dir / "lineage_manifest.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"{len(datasets)} datasets, {len(artefacts)} result artefacts → "
          f"{path.relative_to(ROOT)}\ncode {out['code_fingerprint']}  "
          f"frontend {out['frontend_fingerprint']}  config {out['config_fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
