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
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    lane TEXT NOT NULL,
    case_id TEXT,
    target TEXT,
    params_json TEXT NOT NULL,
    owner TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
-- v0.4: evidence management, multi-camera, intelligence, redaction, batch conversion ----------
CREATE TABLE IF NOT EXISTS custody (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT REFERENCES evidence(id),
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_party TEXT NOT NULL DEFAULT '',
    to_party TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS custody_evidence ON custody(evidence_id, ts);
CREATE TABLE IF NOT EXISTS evidence_tags (
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (evidence_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON evidence_tags(tag);
CREATE TABLE IF NOT EXISTS bookmarks (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    frame INTEGER NOT NULL DEFAULT 0,
    end_frame INTEGER,
    label TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'bookmark',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_evidence ON bookmarks(evidence_id, frame);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    purpose TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    nodes_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    label TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_records (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    reference_evidence_id TEXT,
    offset_s REAL NOT NULL,
    drift_ppm REAL NOT NULL DEFAULT 0,
    uncertainty_s REAL,
    method TEXT NOT NULL,
    details_json TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 1,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_evidence ON sync_records(evidence_id, current);
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    doc_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS detection_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL,
    models_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES detection_runs(id),
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    frame INTEGER NOT NULL,
    time REAL,
    cls TEXT NOT NULL,
    score REAL NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    track_id TEXT
);
CREATE INDEX IF NOT EXISTS detections_lookup ON detections(case_id, cls, evidence_id, frame);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES detection_runs(id),
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    cls TEXT NOT NULL,
    start_frame INTEGER NOT NULL,
    end_frame INTEGER NOT NULL,
    start_time REAL,
    end_time REAL,
    mean_score REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'machine',
    merged_into TEXT,
    boxes_json TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS tracks_lookup ON tracks(case_id, cls, evidence_id);
CREATE TABLE IF NOT EXISTS plates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES detection_runs(id),
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    frame INTEGER NOT NULL,
    time REAL,
    corners_json TEXT NOT NULL,
    score REAL NOT NULL,
    crop_sha256 TEXT NOT NULL,
    rectified_sha256 TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    best_text TEXT NOT NULL DEFAULT '',
    best_confidence REAL NOT NULL DEFAULT 0,
    format_match TEXT,
    vehicle_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unreviewed',
    reviewed_text TEXT,
    reviewer TEXT,
    reviewed_at TEXT,
    track_id TEXT
);
CREATE INDEX IF NOT EXISTS plates_lookup ON plates(case_id, status, best_text);
CREATE TABLE IF NOT EXISTS batch_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    case_id TEXT,
    profile_json TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    manifest_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batch_jobs(id),
    seq INTEGER NOT NULL,
    input_path TEXT NOT NULL,
    input_sha256 TEXT,
    input_size INTEGER,
    detected_json TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    mode TEXT,
    outputs_json TEXT,
    checks_json TEXT,
    log TEXT NOT NULL DEFAULT '',
    error_class TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS batch_items_batch ON batch_items(batch_id, seq);
CREATE TABLE IF NOT EXISTS case_members (
    case_id TEXT NOT NULL REFERENCES cases(id),
    username TEXT NOT NULL REFERENCES users(username),
    role TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (case_id, username)
);
-- v0.5: still-image traffic detection ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS traffic_config (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    utc_offset_hours REAL NOT NULL DEFAULT 0,
    regions_json TEXT NOT NULL DEFAULT '[]',
    settings_json TEXT NOT NULL DEFAULT '{}',
    static_lights_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_images (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    camera_id TEXT,
    image_name TEXT NOT NULL,
    capture_utc TEXT,
    time_source TEXT NOT NULL DEFAULT 'unknown',
    lighting TEXT,
    lighting_override TEXT,
    scene_json TEXT NOT NULL DEFAULT '{}',
    regions_json TEXT,
    detection_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    run_id TEXT,
    approved_by TEXT,
    approved_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traffic_images_case ON traffic_images(case_id, camera_id, status);
CREATE TABLE IF NOT EXISTS traffic_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    camera_id TEXT,
    profile_id TEXT,
    params_json TEXT NOT NULL,
    models_json TEXT NOT NULL,
    scene_json TEXT NOT NULL,
    members_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_objects (
    run_id TEXT NOT NULL REFERENCES traffic_runs(id),
    idx INTEGER NOT NULL,
    cls TEXT NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    fused_score REAL NOT NULL,
    score REAL NOT NULL,
    members_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    in_region INTEGER NOT NULL,
    kept INTEGER NOT NULL,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS traffic_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    run_id TEXT,
    object_idx INTEGER,
    cls TEXT NOT NULL,
    score REAL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    origin TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    members_json TEXT NOT NULL DEFAULT '{}',
    original_json TEXT,
    note TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traffic_detections_ev ON traffic_detections(evidence_id, state);
CREATE TABLE IF NOT EXISTS traffic_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    camera_id TEXT,
    evidence_id TEXT NOT NULL,
    detection_id INTEGER,
    kind TEXT NOT NULL,
    cls TEXT,
    box_json TEXT,
    before_json TEXT,
    crop_sha256 TEXT,
    lighting TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traffic_feedback_cam ON traffic_feedback(case_id, camera_id, created_at);
CREATE TABLE IF NOT EXISTS traffic_backgrounds (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    lighting TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    lights_sha256 TEXT,
    stats_json TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 1,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_profiles (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    profile_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    training_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    read_at TEXT
);
"""

# Columns added after the first release; applied to existing databases on open.
MIGRATIONS = [
    ("evidence", "derived_from", "TEXT"),
    ("evidence", "conversion_json", "TEXT"),
    # Set when an examiner removes an item from the case; the row and the stored file are kept.
    ("evidence", "removed_json", "TEXT"),
    ("chains", "removed_json", "TEXT"),
    # v0.4: case management
    ("cases", "case_number", "TEXT NOT NULL DEFAULT ''"),
    ("cases", "legal_authority", "TEXT NOT NULL DEFAULT ''"),
    ("cases", "description", "TEXT NOT NULL DEFAULT ''"),
    ("cases", "parent_id", "TEXT"),
    ("cases", "status", "TEXT NOT NULL DEFAULT 'open'"),
    ("cases", "legal_hold_json", "TEXT"),
    ("cases", "retention_until", "TEXT"),
    ("cases", "disposed_json", "TEXT"),
    ("evidence", "sha512", "TEXT"),
    ("evidence", "source_json", "TEXT"),
    ("evidence", "phash", "TEXT"),
    ("evidence", "camera_id", "TEXT"),
    ("evidence", "integrity_json", "TEXT"),
    ("chains", "parent_chain_id", "TEXT"),
    ("users", "failed_logins", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "locked_until", "TEXT"),
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
