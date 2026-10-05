"""Cross-platform advisory file lock (standard library only).

VIGIL's default document store is mongita, which keeps one BSON file per document and has no
locking of its own: two processes writing the same collection can leave it empty, and a reader
can observe a half-rewritten collection. `DocumentStore` therefore serialises access through this
lock.

Why not `fcntl` directly: `fcntl` is POSIX-only. Importing it at module scope made
`vigil.storage.docstore` — and therefore `vigil.api.app` — unimportable on native Windows, which
invalidated the documented Windows path. This module exposes one interface and picks the
platform implementation at import time:

    POSIX    fcntl.flock          (LOCK_EX / LOCK_SH, released with LOCK_UN)
    Windows  msvcrt.locking       (LK_LOCK / LK_UNLCK on a byte range of the lock file)
    other    no-op                (lock file still created; behaviour degrades to "no locking",
                                   which is reported through `LOCK_BACKEND` rather than hidden)

Both implementations are advisory, process-wide and released on normal exit *and* on exception.
Windows' `msvcrt.locking` has no shared mode, so a shared request is satisfied with an exclusive
lock there — stricter than asked for, never weaker.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

__all__ = ["LOCK_BACKEND", "file_lock", "locking_available"]

_IS_WINDOWS = sys.platform.startswith("win")

try:  # POSIX
    import fcntl  # type: ignore

    LOCK_BACKEND = "fcntl"
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore
    try:
        import msvcrt  # type: ignore

        LOCK_BACKEND = "msvcrt"
    except ImportError:  # pragma: no cover - neither POSIX nor Windows
        msvcrt = None  # type: ignore
        LOCK_BACKEND = "none"
else:  # pragma: no cover - import bookkeeping
    msvcrt = None  # type: ignore

# Threads inside one process share the OS lock, so guard them separately.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_REGISTRY_GUARD = threading.Lock()
# Depth of locks this thread already holds per path. flock()/msvcrt locks are per open file
# description, so a nested acquisition on a second handle would block the process against
# itself; the OS lock is therefore taken only at depth 0.
_HELD = threading.local()

_WINDOWS_LOCK_BYTES = 1
_WINDOWS_RETRY_SECONDS = 0.1
_WINDOWS_MAX_WAIT_SECONDS = 30.0


def locking_available() -> bool:
    """True when this platform provides a real advisory lock."""
    return LOCK_BACKEND in ("fcntl", "msvcrt")


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _REGISTRY_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.RLock()
        return lock


def _acquire(handle: IO[str], exclusive: bool) -> None:
    if LOCK_BACKEND == "fcntl":
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return
    if LOCK_BACKEND == "msvcrt":  # pragma: no cover - Windows only
        # msvcrt locks a byte range from the current position; there is no shared mode, so a
        # shared request is served by an exclusive lock (stricter, never weaker).
        handle.seek(0)
        deadline = time.monotonic() + _WINDOWS_MAX_WAIT_SECONDS
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _WINDOWS_LOCK_BYTES)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_WINDOWS_RETRY_SECONDS)
    # LOCK_BACKEND == "none": no OS lock available; the thread lock still applies.


def _release(handle: IO[str]) -> None:
    if LOCK_BACKEND == "fcntl":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if LOCK_BACKEND == "msvcrt":  # pragma: no cover - Windows only
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _WINDOWS_LOCK_BYTES)
        except OSError:
            pass


@contextmanager
def file_lock(path: Path | str, exclusive: bool = True) -> Iterator[None]:
    """Hold an advisory lock on `path` for the duration of the block.

    Always released — on normal exit and on exception. Re-entrant within a thread.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    held = getattr(_HELD, "depth", None)
    if held is None:
        held = _HELD.depth = {}
    key = str(path.resolve() if path.exists() else path)
    if held.get(key):
        # Already held by this thread (e.g. a read issued inside a write transaction).
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    with _thread_lock(path):
        handle = open(path, "a+")
        try:
            if os.fstat(handle.fileno()).st_size == 0 and LOCK_BACKEND == "msvcrt":
                # msvcrt needs at least one byte in the file to lock a range.
                handle.write("vigil lock\n")
                handle.flush()
            _acquire(handle, exclusive)
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0
                _release(handle)
        finally:
            handle.close()
