"""Content-fingerprint tests (v1.0 defect #10).

The previous implementation hashed file NAMES and SIZES: a same-length edit was invisible.
"""
from __future__ import annotations

from pathlib import Path

from vigil.mlops.fingerprints import (artefact_fingerprints, bytes_digest, file_digest, manifest,
                                      tree_fingerprint)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
    return root


def test_same_length_edit_changes_the_fingerprint(tmp_path):
    root = _tree(tmp_path)
    before = tree_fingerprint(root)
    (root / "a.py").write_text("x = 9\n", encoding="utf-8")          # identical byte count
    after = tree_fingerprint(root)
    assert before != after


def test_rename_changes_the_tree_but_not_the_content_digest(tmp_path):
    root = _tree(tmp_path)
    digest = file_digest(root / "a.py")
    (root / "a.py").rename(root / "renamed.py")
    assert file_digest(root / "renamed.py") == digest


def test_fingerprint_is_deterministic_and_order_independent(tmp_path):
    root = _tree(tmp_path)
    first = tree_fingerprint(root)
    (root / "sub" / "c.py").write_text("z = 3\n", encoding="utf-8")
    (root / "sub" / "c.py").unlink()
    assert tree_fingerprint(root) == first


def test_manifest_is_sorted_and_repo_relative(tmp_path):
    root = _tree(tmp_path)
    entries = manifest(root)
    paths = [e["path"] for e in entries]
    assert paths == sorted(paths)
    assert paths == ["a.py", "sub/b.py"]
    assert all(not str(p).startswith("/") for p in paths)


def test_caches_are_excluded(tmp_path):
    root = _tree(tmp_path)
    before = tree_fingerprint(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.py").write_text("garbage\n", encoding="utf-8")
    assert tree_fingerprint(root) == before


def test_artefact_fingerprints_are_content_based(tmp_path):
    f = tmp_path / "r.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    one = artefact_fingerprints([f], root=tmp_path)
    f.write_text('{"a": 2}', encoding="utf-8")
    two = artefact_fingerprints([f], root=tmp_path)
    assert one[0]["path"] == two[0]["path"] == "r.json"
    assert one[0]["sha"] != two[0]["sha"]


def test_bytes_and_file_digests_agree(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"vigil")
    assert file_digest(f) == bytes_digest(b"vigil")


def test_forecast_contract_uses_a_content_fingerprint(cfg):
    from vigil.decision.forecast_service import ForecastService

    svc = ForecastService(cfg)
    fp = svc._code_fingerprint()
    assert fp == tree_fingerprint(Path(cfg.repo_root) / "vigil" if hasattr(cfg, "repo_root")
                                  else Path(__file__).resolve().parents[1] / "vigil", ("*.py",))
