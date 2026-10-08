#!/usr/bin/env python3
"""Validate the configured VIGIL storage mode.

    python scripts/storage_check.py            # uses STORAGE_MODE (default local)
    STORAGE_MODE=hdfs HDFS_URI=hdfs://localhost:9000 python scripts/storage_check.py

For `hdfs` this performs a genuine round trip against the cluster: NameNode connect → mkdir →
write a Parquet file → read it back → delete the probe directory. Exit 0 only when the active
mode equals the requested mode and every check passed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil.config import load_config  # noqa: E402
from vigil.storage.backend import storage_status  # noqa: E402


def main() -> int:
    cfg = load_config()
    st = storage_status(cfg)
    print(f"VIGIL STORAGE CHECK")
    print(f"  requested mode : {st.requested_mode}")
    print(f"  active mode    : {st.active_mode}  ({st.label})")
    print(f"  uri            : {st.uri}")
    print(f"  detail         : {st.detail}")
    for c in st.checks:
        print(f"    [{c['state']}] {c['check']}: {c['detail']}")
    ok = st.available and st.active_mode == st.requested_mode
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("The requested storage mode is NOT active. VIGIL does not silently fall back; "
              "fix the checklist above or set STORAGE_MODE=local.")
    out = cfg.reports_root / "results" / "storage_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(st.to_dict(), indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
