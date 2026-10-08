"""Cross-platform and portability tests (v1.0.1 defects #1, #2, #3, #7, #8).

v1.0.0 imported `fcntl` at module scope in the document store, which made
`import vigil.api.app` fail on native Windows, and it shipped lake manifests containing the build
machine's absolute path. These tests pin both fixes, plus the document-store health rule.
"""
from __future__ import annotations

import ast
import os
import builtins
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

POSIX_ONLY_MODULES = {"fcntl", "termios", "pwd", "grp", "resource", "syslog", "posix"}
# Built from fragments so this file does not itself contain a literal machine path for the
# release scanner to flag.
MACHINE_PATH_MARKERS = ("/" + "home/", "/" + "Users/", "C:" + chr(92) + "Users", "/" + "root/")


# --------------------------------------------------------------------------- #1 / #8 Windows
def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_module_in_the_default_api_path_imports_a_posix_only_module():
    """The whole `import vigil.api.app` surface must be platform-neutral."""
    # vigil/storage/filelock.py is the ONE place allowed to touch fcntl, inside a try/except
    # ImportError that falls back to msvcrt. Everything else must go through it.
    allowed = {"vigil/storage/filelock.py"}
    offenders = []
    for path in sorted((ROOT / "vigil").rglob("*.py")):
        if path.relative_to(ROOT).as_posix() in allowed:
            continue
        posix_only = _imported_modules(path) & POSIX_ONLY_MODULES
        if posix_only:
            offenders.append(f"{path.relative_to(ROOT)} imports {sorted(posix_only)}")
    assert not offenders, (
        "POSIX-only imports break native Windows; use vigil/storage/filelock.py instead: "
        + "; ".join(offenders))


def test_docstore_imports_without_fcntl(monkeypatch):
    """Simulate Windows: hide fcntl, then import the store and the lock from scratch."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl' (simulated Windows)")
        return real_import(name, *args, **kwargs)

    for mod in ("vigil.storage.filelock", "vigil.storage.docstore"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)
    filelock = importlib.import_module("vigil.storage.filelock")
    docstore = importlib.import_module("vigil.storage.docstore")
    assert filelock.LOCK_BACKEND in ("msvcrt", "none"), filelock.LOCK_BACKEND
    assert hasattr(docstore, "DocumentStore")
    # restore the real modules for the rest of the session
    monkeypatch.setattr(builtins, "__import__", real_import)
    for mod in ("vigil.storage.filelock", "vigil.storage.docstore"):
        del sys.modules[mod]
    importlib.import_module("vigil.storage.docstore")


def test_api_app_imports_in_a_subprocess_with_local_defaults():
    """`STORAGE_MODE=local`, no MONGODB_URI — the documented Windows first-run configuration."""
    code = ("import os, sys;"
            "sys.path.insert(0, r'%s');"
            "import vigil.api.app as app;"
            "import vigil.storage.docstore as ds;"
            "print('OK', 'fcntl' if 'fcntl' in sys.modules else 'no-fcntl')" % ROOT)
    env = os.environ.copy()
    env.update({
        "STORAGE_MODE": "local",
        "PYTHONPATH": str(ROOT),
    })
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout


def test_lock_interface_is_platform_independent(tmp_path):
    from vigil.storage.filelock import LOCK_BACKEND, file_lock, locking_available

    assert LOCK_BACKEND in ("fcntl", "msvcrt", "none")
    assert locking_available() == (LOCK_BACKEND in ("fcntl", "msvcrt"))
    target = tmp_path / "x.lock"
    with file_lock(target):
        assert target.exists()
    with file_lock(target, exclusive=False):
        pass


def test_lock_is_released_after_an_exception(tmp_path):
    from vigil.storage.filelock import file_lock

    target = tmp_path / "x.lock"
    with pytest.raises(ValueError):
        with file_lock(target):
            raise ValueError("boom")
    with file_lock(target):      # would block/raise if the first lock leaked
        pass


def test_lock_is_reentrant_within_a_thread(tmp_path):
    """A read issued inside a write transaction must not deadlock the process against itself."""
    from vigil.storage.filelock import file_lock

    target = tmp_path / "x.lock"
    with file_lock(target, exclusive=True):
        with file_lock(target, exclusive=False):
            pass


def test_second_process_waits_for_the_lock(tmp_path):
    """Cross-process exclusion, proved by ordering rather than by inspection."""
    from vigil.storage.filelock import file_lock, locking_available

    if not locking_available():
        pytest.skip("no OS-level advisory lock on this platform")
    lock = tmp_path / "x.lock"
    out = tmp_path / "order.txt"
    child_code = (
        "import sys, time, pathlib;"
        f"sys.path.insert(0, r'{ROOT}');"
        "from vigil.storage.filelock import file_lock;"
        f"p=pathlib.Path(r'{out}');"
        f"\nwith file_lock(r'{lock}'):\n    p.write_text(p.read_text(encoding='utf-8')+'child\\n')\n")
    out.write_text("", encoding="utf-8")
    with file_lock(lock):
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        import time

        time.sleep(1.5)
        out.write_text(out.read_text(encoding='utf-8') + "parent\n", encoding="utf-8")
    proc.wait(timeout=30)
    assert out.read_text(encoding='utf-8').splitlines() == ["parent", "child"], "the child did not wait"


def test_mongodb_mode_does_not_take_the_local_lock(cfg, monkeypatch):
    from vigil.storage.docstore import DocumentStore

    s = DocumentStore(cfg)
    monkeypatch.setattr(s, "backend", "mongodb")
    with s._write_lock():        # must be a no-op, not an attempt to lock a file
        pass


# --------------------------------------------------------------------------- #2 docstore health
def test_status_reports_degraded_when_the_probe_read_fails(cfg, monkeypatch):
    from vigil.storage.docstore import DocumentStore

    s = DocumentStore(cfg)
    monkeypatch.setattr(s, "count", lambda *a, **k: -1)
    st = s.status()
    assert st.probe_count == -1
    assert st.healthy is False, "a failed probe read must never be reported as healthy"
    assert st.state == "DEGRADED"
    assert st.detail


def test_status_is_healthy_when_the_probe_succeeds(cfg):
    from vigil.storage.docstore import DocumentStore

    st = DocumentStore(cfg).status()
    assert st.probe_count >= 0 and st.healthy is True and st.state == "OK"


def test_api_health_mirrors_the_docstore_state(monkeypatch):
    from fastapi.testclient import TestClient

    import vigil.api.app as app_module

    client = TestClient(app_module.app)
    real_store = app_module.store()

    class Unreadable:
        backend = real_store.backend
        detail = "simulated unreadable store"

        def status(self):
            from vigil.storage.docstore import DocStoreStatus

            return DocStoreStatus(self.backend, "local://x", False, self.detail, "DEGRADED", -1)

        def count(self, *a, **k):
            return -1

        def stats(self):
            return {"forecasts": -1}

        def find(self, *a, **k):
            return []

        def find_one(self, *a, **k):
            return None

    monkeypatch.setattr(app_module, "store", lambda: Unreadable())
    body = client.get("/api/health").json()
    docstore = next(c for c in body["components"] if c["component"] == "DOCUMENT STORE")
    assert docstore["state"] == "DEGRADED"
    assert body["vigil_health"] != "HEALTHY"


# --------------------------------------------------------------------------- #3 / #7 portability
def test_shipped_lake_manifests_carry_portable_uris():
    manifests = sorted((ROOT / "data" / "lake").rglob("_vigil_manifest.json"))
    assert manifests, "no lake manifests found — the package should ship a built lake"
    for man in manifests:
        payload = json.loads(man.read_text(encoding='utf-8'))
        uri = payload.get("uri", "")
        assert uri, f"{man} has no uri"
        assert not any(m in uri for m in MACHINE_PATH_MARKERS), f"{man} leaks a machine path: {uri}"
        if payload.get("storage_mode", "LOCAL").upper() == "LOCAL":
            assert uri.startswith("data/lake/"), uri
            assert not Path(uri).is_absolute(), uri
        else:
            assert uri.startswith("hdfs://"), uri


def test_local_backend_uri_is_repo_relative_and_hdfs_uri_is_explicit(cfg, tmp_path):
    from vigil.storage.backend import HdfsBackend, LocalBackend

    local = LocalBackend(cfg.lake_root)
    uri = local.uri("features", "model_matrix")
    assert uri == "data/lake/features/model_matrix"
    assert not any(m in uri for m in MACHINE_PATH_MARKERS)

    # URI construction only — no NameNode is contacted.
    hdfs = HdfsBackend.__new__(HdfsBackend)
    hdfs.host, hdfs.port, hdfs.prefix = "namenode", 8020, "/vigil"
    assert hdfs.uri("features", "model_matrix") == "hdfs://namenode:8020/vigil/features/model_matrix"


def test_a_freshly_written_manifest_is_portable(cfg, tmp_path, monkeypatch):
    import pandas as pd

    from vigil.storage.lake import DataLake

    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setattr(type(cfg), "lake_root", property(lambda _self: tmp_path / "lake"))
    lake = DataLake(cfg)
    frame = pd.DataFrame({"symbol": ["A", "B"], "year": [2024, 2024], "value": [1.0, 2.0]})
    lake.write(frame, "analytics", "portability_probe", partition_cols=["symbol"])
    man = json.loads((tmp_path / "lake" / "analytics" / "portability_probe"
                      / "_vigil_manifest.json").read_text(encoding='utf-8'))
    assert not any(m in man["uri"] for m in MACHINE_PATH_MARKERS), man["uri"]


def test_audit_scanner_inspects_generated_metadata_under_data():
    sys.path.insert(0, str(ROOT / "scripts"))
    from _audit_common import BINARY_SUFFIXES, scan_files

    scanned = {p.relative_to(ROOT).as_posix() for p in scan_files()}
    assert any(p.startswith("data/") and p.endswith("_vigil_manifest.json") for p in scanned), (
        "the audit must inspect generated lake manifests, not skip the whole data/ directory")
    assert not any(p.endswith(".parquet") for p in scanned), "binary datasets must not be scanned"
    assert ".parquet" in BINARY_SUFFIXES


def test_no_machine_specific_path_in_any_scanned_release_file():
    sys.path.insert(0, str(ROOT / "scripts"))
    from _audit_common import find_machine_paths

    hits = find_machine_paths()
    assert not hits, f"machine-specific paths in the release: {hits[:5]}"


# --------------------------------------------------------------------------- #4 windows script
def test_windows_rebuild_script_mirrors_the_shell_script():
    ps1 = (ROOT / "scripts" / "rebuild_models.ps1").read_text(encoding='utf-8')
    sh = (ROOT / "scripts" / "rebuild_models.sh").read_text(encoding='utf-8')
    stages = ["models", "ensemble", "calibration", "uncertainty", "drift", "backtest",
              "decision_quality", "forecasts", "failure_lab", "experiments", "report"]
    for stage in stages:
        assert f"'{stage}'" in ps1, f"{stage} missing from the PowerShell rebuild script"
        assert stage in sh
    assert "run_pipeline.py" in ps1
    assert "--horizons" in ps1 and "--offline" in ps1
    import re as _re

    code = _re.sub(r"<#.*?#>", "", ps1, flags=_re.S)                     # comment-based help
    body = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    for shell in ("bash", "wsl", "sh -c"):
        assert shell not in body.lower(), f"the PowerShell script must not depend on {shell}"
    assert "$MyInvocation.MyCommand.Path" in ps1, "the script must resolve its own repo root"
    assert "exit 0" in ps1 and "$LASTEXITCODE" in ps1, "exit codes must be preserved"

    # Cross-platform runner and UTF-8 encoding validation for Windows
    runner = (ROOT / "tests" / "run_ui_smoke.mjs").read_text(encoding="utf-8")
    assert "process.platform === 'win32'" in runner, "tests/run_ui_smoke.mjs must inspect process.platform"
    assert "VIRTUAL_ENV" in runner, "tests/run_ui_smoke.mjs must resolve VIRTUAL_ENV"
    assert "Scripts" in runner and "python.exe" in runner, "tests/run_ui_smoke.mjs must support Windows venv Scripts/python.exe" 

    import ast
    violations = []
    for p in ROOT.rglob("*.py"):
        if any(part in {".git", "node_modules", ".venv", "__pycache__"} for part in p.parts):
            continue
        code = p.read_text(encoding="utf-8", errors="ignore")
        if "write_text" not in code:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
                kw_names = [kw.arg for kw in node.keywords]
                if "encoding" not in kw_names:
                    violations.append(f"{p.relative_to(ROOT)}:{node.lineno}")
    assert not violations, f"write_text calls missing explicit encoding (risking cp1252 crash on Windows): {violations}" 


def test_readme_documents_both_platforms():
    readme = (ROOT / "README.md").read_text(encoding='utf-8')
    assert ".venv\\Scripts\\Activate.ps1" in readme
    assert "Copy-Item .env.example .env" in readme
    assert "rebuild_models.ps1" in readme and "rebuild_models.sh" in readme


# --------------------------------------------------------------------------- concurrent access
def test_two_stores_in_one_process_share_a_client(cfg):
    """Two MongitaClientDisk clients on one directory read back an incoherent (empty) view.

    That is how `/api/recommend` came to answer "no forecasts available" while an audit held the
    store open, so the client is cached per directory per process.
    """
    from vigil.storage.docstore import DocumentStore

    a, b = DocumentStore(cfg), DocumentStore(cfg)
    if a.backend != "mongita-local":
        pytest.skip("server-backed document store does its own concurrency control")
    assert a._client is b._client
    assert a.count("forecasts") == b.count("forecasts")


def test_a_concurrent_reader_process_does_not_empty_a_collection(tmp_path):
    """Open + read from a second process while this one holds the store; data must survive."""
    store_dir = tmp_path / "docstore"
    seed = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from mongita import MongitaClientDisk\n"
        "db = MongitaClientDisk(r'%s')['vigil']\n"
        "print(db['probe'].count_documents({}))\n" % (ROOT, store_dir)
    )
    from mongita import MongitaClientDisk

    db = MongitaClientDisk(str(store_dir))["vigil"]
    db["probe"].insert_many([{"i": i} for i in range(25)])
    assert db["probe"].count_documents({}) == 25
    procs = [subprocess.Popen([sys.executable, "-c", seed], stdout=subprocess.PIPE, text=True)
             for _ in range(3)]
    for proc in procs:
        out, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0
    assert db["probe"].count_documents({}) == 25, "a concurrent reader emptied the collection"
