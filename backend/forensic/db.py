"""SQLite persistence for cases, evidence, filter chains, analyses and the audit log.

SQLite keeps the MVP a single-process install for one examiner; the schema maps
directly onto Postgres for the multi-user deployment in the plan.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    examiner TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    md5 TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    probe_json TEXT NOT NULL,
    ingested_by TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chains (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    name TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    output_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    artifact_path TEXT,
    output_hash TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    params_json TEXT NOT NULL,
    output_hash TEXT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_case ON audit(case_id, seq);
CREATE TABLE IF NOT EXISTS camera_references (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    name TEXT NOT NULL,
    camera TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    version TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    chain_id TEXT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    frame INTEGER NOT NULL DEFAULT 0,
    inputs_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    level TEXT NOT NULL,
    result_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    level TEXT NOT NULL,
    result_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release; applied to existing databases on open.
MIGRATIONS = [
    ("evidence", "derived_from", "TEXT"),
    ("evidence", "conversion_json", "TEXT"),
    # Set when an examiner removes an item from the case; the row and the stored file are kept.
    ("evidence", "removed_json", "TEXT"),
    ("chains", "removed_json", "TEXT"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def canonical_json(obj: Any) -> str:
    """JSON with sorted keys and no whitespace, used everywhere a value is hashed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError as e:
                    # Another connection opening the same database added it first.
                    if "duplicate column" not in str(e):
                        raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def transaction(self):
        return _Tx(self)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False
