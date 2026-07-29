"""SQLite persistence for mapping profiles and import history.

Kept deliberately tiny — a couple of tables and thin helpers. No ORM.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    payload     TEXT NOT NULL,               -- JSON: column map + constants + scope
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT,                        -- create_only / update_only / upsert
    dry_run      INTEGER NOT NULL DEFAULT 1,
    total        INTEGER NOT NULL DEFAULT 0,
    succeeded    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    summary      TEXT                         -- JSON per-row results
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    path = get_settings().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


# --------------------------------------------------------------------- #
# Mapping profiles                                                      #
# --------------------------------------------------------------------- #
def save_profile(name: str, payload: dict) -> None:
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mapping_profiles (name, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (name, json.dumps(payload), now, now),
        )


def list_profiles() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, updated_at FROM mapping_profiles ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_profile(name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM mapping_profiles WHERE name = ?", (name,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def delete_profile(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM mapping_profiles WHERE name = ?", (name,))


# --------------------------------------------------------------------- #
# Import runs                                                           #
# --------------------------------------------------------------------- #
def create_run(mode: str, dry_run: bool, total: int,
               profile_name: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO import_runs
                (started_at, mode, dry_run, total, profile_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now(), mode, int(dry_run), total, profile_name),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, succeeded: int, failed: int, summary: list) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_runs
            SET finished_at = ?, succeeded = ?, failed = ?, summary = ?
            WHERE id = ?
            """,
            (_now(), succeeded, failed, json.dumps(summary), run_id),
        )


def list_runs(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, mode, dry_run, total, "
            "succeeded, failed, profile_name FROM import_runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
