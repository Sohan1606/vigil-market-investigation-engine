"""Content-based fingerprints for code and data lineage.

A fingerprint must change when the *content* changes and must not change when something
irrelevant (a filename, a timestamp, the order the filesystem happened to list files in) changes.
The previous implementation hashed file NAMES and SIZES, so editing a line without changing the
byte count produced an identical fingerprint — a lineage record that could not detect the very
thing it existed to detect.

Everything here hashes the bytes:

    file_digest(path)      BLAKE2b over the file's contents
    tree_fingerprint(root) BLAKE2b over (repo-relative path, content digest) for every matched
                           file, sorted by path → deterministic across machines and checkouts
    manifest(root)         the full per-file table behind a tree fingerprint

Paths are recorded repo-relative so the same checkout fingerprints identically in any directory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DIGEST_SIZE = 8
CHUNK = 1 << 20
EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv", ".ruff_cache"}


def file_digest(path: Path, digest_size: int = DIGEST_SIZE) -> str:
    """BLAKE2b of the file's bytes — content, not metadata."""
    h = hashlib.blake2b(digest_size=digest_size)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def bytes_digest(data: bytes, digest_size: int = DIGEST_SIZE) -> str:
    return hashlib.blake2b(data, digest_size=digest_size).hexdigest()


def iter_files(root: Path, patterns: Sequence[str] = ("*.py",)) -> List[Path]:
    """Every matching file under `root`, excluding caches, sorted by repo-relative path."""
    root = Path(root)
    found: List[Path] = []
    for pattern in patterns:
        for p in root.rglob(pattern):
            if not p.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in p.parts):
                continue
            found.append(p)
    return sorted(set(found), key=lambda p: str(p.relative_to(root)).replace("\\", "/"))


def manifest(root: Path, patterns: Sequence[str] = ("*.py",),
             digest_size: int = DIGEST_SIZE) -> List[Dict[str, object]]:
    """Deterministic per-file content manifest: [{path, sha, bytes}, ...] sorted by path."""
    root = Path(root)
    return [{"path": str(p.relative_to(root)).replace("\\", "/"),
             "sha": file_digest(p, digest_size),
             "bytes": p.stat().st_size}
            for p in iter_files(root, patterns)]


def tree_fingerprint(root: Path, patterns: Sequence[str] = ("*.py",),
                     digest_size: int = DIGEST_SIZE) -> str:
    """One fingerprint over the CONTENT of every matched file beneath `root`."""
    h = hashlib.blake2b(digest_size=digest_size)
    for entry in manifest(root, patterns, digest_size):
        h.update(str(entry["path"]).encode())
        h.update(b"\0")
        h.update(str(entry["sha"]).encode())
        h.update(b"\n")
    return h.hexdigest()


def artefact_fingerprints(paths: Iterable[Path], root: Optional[Path] = None,
                          digest_size: int = DIGEST_SIZE) -> List[Dict[str, object]]:
    """Content digests for a specific list of artefacts (results JSON, parquet manifests, ...)."""
    out: List[Dict[str, object]] = []
    for p in sorted(Path(x) for x in paths):
        if not p.is_file():
            continue
        name = str(p.relative_to(root)).replace("\\", "/") if root else p.name
        out.append({"path": name, "sha": file_digest(p, digest_size), "bytes": p.stat().st_size})
    return out
