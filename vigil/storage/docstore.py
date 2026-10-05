"""Document-oriented (NoSQL) layer.

Collections: forecasts, cases, events, experiments, models, news, audits, decisions, patterns,
outcomes, human_votes. Uses real MongoDB (pymongo) when MONGODB_URI is set; otherwise a local
MongoDB-API-compatible engine (mongita) so the project runs with zero external services.
The calling code is identical in both paths — only the client differs.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from .filelock import LOCK_BACKEND, file_lock, locking_available

log = get_logger("vigil.storage.docstore")

COLLECTIONS = {
    "forecasts": ["forecast_id", "symbol", "as_of"],
    "decisions": ["forecast_id", "symbol", "as_of"],
    "cases": ["case_id", "status", "opened_at"],
    "events": ["event_id", "symbol", "ts"],
    "news": ["news_id", "symbol", "published_at"],
    "experiments": ["experiment_id"],
    "models": ["model_id", "model_version"],
    "audits": ["audit_id", "forecast_id"],
    "outcomes": ["forecast_id"],
    "patterns": ["pattern_id"],
    "human_votes": ["forecast_id"],
    "cemetery": ["forecast_id"],
}

# Minimal schema contracts enforced on write (document validation).
SCHEMAS: Dict[str, Dict[str, type]] = {
    "forecasts": {"forecast_id": str, "symbol": str, "as_of": str, "horizon": int,
                  "probability_up": float, "model_version": str, "feature_version": str},
    "decisions": {"forecast_id": str, "verdict": str},
    "cases": {"case_id": str, "title": str, "status": str},
    "events": {"event_id": str, "symbol": str, "ts": str, "event_type": str},
}


class SchemaViolation(ValueError):
    pass


# One mongita client per directory PER PROCESS. Opening a second MongitaClientDisk on a directory
# another client already holds yields an incoherent view (collections read back as empty), which
# made `/api/recommend` answer "no forecasts available" whenever an audit held the store open
# while the API was serving. Clients are therefore cached and shared inside the process; the
# cross-process case is handled by the advisory lock in vigil/storage/filelock.py.
_LOCAL_CLIENTS: Dict[str, Any] = {}


def to_native(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars so documents stay JSON-serialisable."""
    import datetime as _dt

    import numpy as _np

    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, _np.generic):
        return obj.item()
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    try:
        import pandas as _pd

        if isinstance(obj, _pd.Timestamp):
            return obj.isoformat()
        if obj is _pd.NaT:
            return None
    except Exception:
        pass
    return obj


@dataclass
class DocStoreStatus:
    backend: str
    uri_label: str
    healthy: bool
    detail: str
    state: str = "OK"          # OK | DEGRADED
    probe_count: int = 0       # documents seen by the probe; -1 means the read failed

    def to_dict(self) -> Dict[str, Any]:
        return {"backend": self.backend, "uri_label": self.uri_label, "healthy": self.healthy,
                "state": self.state, "detail": self.detail, "probe_count": self.probe_count}


class DocumentStore:
    def __init__(self, cfg: Optional[VigilConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.backend = "unavailable"
        self.detail = ""
        self._client = None
        self._db = None
        self._connect()

    # ---------------- connection ----------------
    def _connect(self) -> None:
        uri = self.cfg.mongodb_uri
        dbname = self.cfg.get("storage.docstore.database", "vigil")
        if uri:
            try:
                from pymongo import MongoClient  # type: ignore

                client = MongoClient(uri, serverSelectionTimeoutMS=2500)
                client.admin.command("ping")
                self._client, self._db, self.backend = client, client[dbname], "mongodb"
                self.detail = "connected to MongoDB server"
            except Exception as exc:  # graceful degradation, never a blank failure
                log.warning("MongoDB unavailable (%s) — falling back to local document engine", type(exc).__name__)
                self.detail = f"MongoDB unreachable ({type(exc).__name__}); local engine active"
        if self._db is None:
            from mongita import MongitaClientDisk  # type: ignore

            path = self.cfg.path("storage.docstore.local_path", "data/processed/docstore")
            path.mkdir(parents=True, exist_ok=True)
            self._lock_path = path.parent / f"{path.name}.writelock"
            key = str(path.resolve())
            client = _LOCAL_CLIENTS.get(key)
            if client is None:
                # mongita rewrites $.metadata when a client opens, which is outside any read or
                # write call — so the open itself is serialised too.
                with file_lock(self._lock_path, exclusive=True):
                    client = _LOCAL_CLIENTS[key] = MongitaClientDisk(str(path))
            self._client = client
            self._db = self._client[dbname]
            self.backend = self.backend if self.backend == "mongodb" else "mongita-local"
            self.detail = self.detail or "local MongoDB-API document engine (no server required)"
        self._ensure_indexes()

    # ---------------- cross-process access lock ----------------
    # mongita keeps one BSON file per document and has no locking: two processes writing the same
    # collection at once can leave it empty (observed during the v1.0 hardening, when an audit
    # held the store open while a second server wrote to it), and a reader can observe a
    # half-rewritten collection. Every mutation takes an exclusive advisory lock and every read
    # takes a shared one, so a concurrent writer WAITS instead of corrupting. The lock is
    # cross-platform (fcntl on POSIX, msvcrt on Windows — see vigil/storage/filelock.py).
    # A real MongoDB server does its own locking, so this is skipped there.
    @contextmanager
    def _access_lock(self, exclusive: bool):
        path = getattr(self, "_lock_path", None)
        if self.backend != "mongita-local" or path is None:
            yield
            return
        with file_lock(path, exclusive=exclusive):
            yield

    def _write_lock(self):
        return self._access_lock(exclusive=True)

    def _read_lock(self):
        return self._access_lock(exclusive=False)

    def _ensure_indexes(self) -> None:
        for name, keys in COLLECTIONS.items():
            col = self._db[name]
            for key in keys:
                try:
                    col.create_index(key)
                except Exception:  # pragma: no cover - index support varies by backend
                    pass

    # ---------------- validation ----------------
    @staticmethod
    def validate(collection: str, doc: Dict[str, Any]) -> None:
        schema = SCHEMAS.get(collection)
        if not schema:
            return
        for field, typ in schema.items():
            if field not in doc:
                raise SchemaViolation(f"{collection}: missing required field '{field}'")
            value = doc[field]
            if typ is float and isinstance(value, int):
                continue
            if not isinstance(value, typ):
                raise SchemaViolation(
                    f"{collection}.{field}: expected {typ.__name__}, got {type(value).__name__}")

    # ---------------- CRUD ----------------
    def insert(self, collection: str, doc: Dict[str, Any]) -> None:
        doc = to_native(doc)
        self.validate(collection, doc)
        with self._write_lock():
            self._db[collection].insert_one(dict(doc))

    def insert_many(self, collection: str, docs: Iterable[Dict[str, Any]]) -> int:
        docs = [to_native(dict(d)) for d in docs]
        for d in docs:
            self.validate(collection, d)
        if not docs:
            return 0
        with self._write_lock():
            self._db[collection].insert_many(docs)
        return len(docs)

    def replace(self, collection: str, key: Dict[str, Any], doc: Dict[str, Any]) -> None:
        doc = to_native(doc)
        self.validate(collection, doc)
        with self._write_lock():
            self._db[collection].replace_one(key, dict(doc), upsert=True)

    def _retry(self, what: str, fn, default):
        """Run a read, retrying once.

        The local fallback (mongita) keeps one BSON file per document and is a single-writer
        store: a read issued while the pipeline is rewriting a collection can raise
        InvalidBSON('bad eoo'). Retrying once resolves the transient case; if it persists we
        degrade loudly (the caller surfaces a DEGRADED state) instead of returning a 500.
        Set MONGODB_URI to use a real MongoDB when concurrent readers and writers are expected.
        """
        for attempt in (1, 2):
            try:
                with self._read_lock():
                    return fn()
            except Exception as exc:
                if attempt == 2:
                    self.detail = (f"{what} failed on the {self.backend} backend: "
                                   f"{type(exc).__name__} — the store was most likely being "
                                   f"rewritten by a pipeline run")
                    log.warning(self.detail)
                    return default
                time.sleep(0.25)
        return default

    def find(self, collection: str, query: Optional[Dict[str, Any]] = None,
             limit: int = 0, sort: Optional[List[tuple]] = None) -> List[Dict[str, Any]]:
        cur = self._retry(f"find({collection})", lambda: list(self._db[collection].find(query or {})), [])
        docs = [self._clean(d) for d in cur]
        if sort:
            for key, direction in reversed(sort):
                docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
        return docs[:limit] if limit else docs

    def find_one(self, collection: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc = self._retry(f"find_one({collection})", lambda: self._db[collection].find_one(query), None)
        return self._clean(doc) if doc else None

    def count(self, collection: str, query: Optional[Dict[str, Any]] = None) -> int:
        return int(self._retry(f"count({collection})",
                               lambda: self._db[collection].count_documents(query or {}), -1))

    def drop(self, collection: str) -> None:
        """Empty a collection.

        mongita's `drop()` is a no-op for collections created in the current session, which
        silently produced duplicate documents on repeated pipeline runs. Deleting every document
        is the behaviour callers actually depend on, so do that first and verify it.
        """
        with self._write_lock():
            try:
                self._db[collection].delete_many({})
            except Exception as exc:  # pragma: no cover - backend specific
                log.warning("delete_many failed on %s: %s", collection, exc)
            try:
                if self._db[collection].count_documents({}):
                    self._db[collection].drop()
            except Exception as exc:  # pragma: no cover
                log.warning("drop failed on %s: %s", collection, exc)
            remaining = self._db[collection].count_documents({})
        if remaining:
            raise RuntimeError(f"collection {collection} still holds {remaining} documents after drop")

    def reset(self) -> None:
        for name in COLLECTIONS:
            self.drop(name)
        self._ensure_indexes()

    @staticmethod
    def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
        doc = dict(doc)
        doc.pop("_id", None)
        return doc

    # ---------------- health / export ----------------
    def status(self) -> DocStoreStatus:
        """Probe the store and report what the probe actually found.

        `count()` returns -1 when a read fails after its retry (the local engine is single-writer
        and can be mid-rewrite). v1.0.0 treated "no exception raised" as healthy, so the API could
        answer `healthy: true` while the store was unreadable. A failed probe is now DEGRADED.
        """
        probe = -1
        try:
            probe = self.count("forecasts")
        except Exception as exc:  # pragma: no cover - defensive; count() already swallows
            self.detail = f"document store error: {type(exc).__name__}: {exc}"
        healthy = probe >= 0
        if not healthy and not self.detail:
            self.detail = ("probe read of the 'forecasts' collection failed — the store is "
                           "unreadable right now (most likely a concurrent rewrite)")
        state = "OK" if healthy else "DEGRADED"
        detail = self.detail or (
            f"{self.backend} backend readable; {probe} forecast document(s) visible")
        label = "mongodb://***" if self.backend == "mongodb" else "local://data/processed/docstore"
        return DocStoreStatus(self.backend, label, healthy, detail, state, probe)

    def stats(self) -> Dict[str, int]:
        """Document counts. A count of -1 means that collection could not be read (see detail)."""
        return {name: self.count(name) for name in COLLECTIONS}

    def export_json(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in COLLECTIONS:
            docs = self.find(name)
            (out_dir / f"{name}.json").write_text(json.dumps(docs, indent=2, default=str))
        return out_dir
