from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_library_dirs


TERMINAL_STATUSES = {"done", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Job:
    id: str
    artist: str
    title: str
    status: str
    source_audio_path: str
    lyrics_path: str | None
    job_dir: str
    output_dir: str
    log_path: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        data = dict(row)
        data["metadata"] = json.loads(data.get("metadata") or "{}")
        return cls(**data)


def connect() -> sqlite3.Connection:
    ensure_library_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                source_audio_path TEXT NOT NULL,
                lyrics_path TEXT,
                job_dir TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                log_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forge_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def get_state(key: str) -> str | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT value FROM forge_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_state(key: str, value: str) -> None:
    init_db()
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO forge_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def clear_state(key: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM forge_state WHERE key = ?", (key,))


def create_job(
    *,
    artist: str,
    title: str,
    source_audio_path: Path,
    lyrics_path: Path | None,
    job_dir: Path,
    output_dir: Path,
    log_path: Path,
    metadata: dict[str, Any] | None = None,
) -> Job:
    init_db()
    job_id = str(uuid.uuid4())
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, artist, title, status, source_audio_path, lyrics_path,
                job_dir, output_dir, log_path, created_at, updated_at,
                started_at, finished_at, error, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                artist,
                title,
                "queued",
                str(source_audio_path),
                str(lyrics_path) if lyrics_path else None,
                str(job_dir),
                str(output_dir),
                str(log_path),
                now,
                now,
                None,
                None,
                None,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
    job = get_job(job_id)
    if job is None:
        raise RuntimeError("job was not persisted")
    return job


def get_job(job_id: str) -> Job | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def list_jobs(limit: int = 100) -> list[Job]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [Job.from_row(row) for row in rows]


def update_job(job_id: str, **fields: Any) -> Job:
    init_db()
    if not fields:
        job = get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    fields["updated_at"] = utc_now()
    if "metadata" in fields and isinstance(fields["metadata"], dict):
        fields["metadata"] = json.dumps(fields["metadata"], sort_keys=True)

    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)

    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    return job
