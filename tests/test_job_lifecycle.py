from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from packages.karaoke_forge.job_lifecycle import (
    ORPHAN_ERROR,
    active_review_job,
    claim_active_job,
    find_blocking_active_job,
    reconcile_orphaned_jobs,
    release_active_job,
)
from packages.karaoke_forge.store import create_job, get_job, get_state, init_db, set_state, update_job
from packages.karaoke_forge.web import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("KARAOKE_FORGE_BASE_PATH", "/karaoke-forge")
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    init_db()
    return TestClient(app)


def _make_job(tmp_path: Path, *, artist: str = "Artist", title: str = "Title", stem: str = "job"):
    job_dir = tmp_path / stem
    output_dir = tmp_path / "out" / stem
    log_path = job_dir / "run.log"
    source = tmp_path / f"{stem}.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return create_job(
        artist=artist,
        title=title,
        source_audio_path=source,
        lyrics_path=None,
        job_dir=job_dir,
        output_dir=output_dir,
        log_path=log_path,
        metadata={"command": ["karaoke-gen", str(source), artist, title]},
    )


def test_reconcile_marks_unowned_running_jobs_orphaned(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", tmp_path / "forge.sqlite3")
    init_db()
    orphan = _make_job(tmp_path, stem="orphan")
    update_job(orphan.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])

    reconciled = reconcile_orphaned_jobs()

    assert orphan.id in reconciled
    updated = get_job(orphan.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.error == ORPHAN_ERROR
    assert get_state("active_job_id") is None


def test_reconcile_keeps_job_matching_live_process(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", tmp_path / "forge.sqlite3")
    init_db()
    live = _make_job(tmp_path, stem="live")
    update_job(live.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    stale = _make_job(tmp_path, stem="stale", artist="Other", title="Song")
    update_job(stale.id, status="running", started_at="2026-05-19T07:00:00+00:00")

    cmdline = f"karaoke-gen -y {live.source_audio_path} Artist Title"
    monkeypatch.setattr(
        "packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes",
        lambda: [(12345, cmdline)],
    )

    reconciled = reconcile_orphaned_jobs()

    assert stale.id in reconciled
    assert live.id not in reconciled
    assert get_state("active_job_id") == live.id


def test_find_blocking_active_job_after_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", tmp_path / "forge.sqlite3")
    init_db()
    live = _make_job(tmp_path, stem="live")
    update_job(live.id, status="reviewing", started_at="2026-05-19T08:00:00+00:00")
    set_state("active_job_id", live.id)
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])

    blocking = find_blocking_active_job()

    assert blocking is None


def test_claim_and_release_active_job(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", tmp_path / "forge.sqlite3")
    init_db()
    job = _make_job(tmp_path)
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])

    assert claim_active_job(job.id) is True
    assert get_state("active_job_id") == job.id

    other = _make_job(tmp_path, stem="other", artist="B", title="B")
    update_job(other.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    set_state("active_job_id", job.id)
    update_job(job.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    monkeypatch.setattr(
        "packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes",
        lambda: [(1, f"karaoke-gen -y {job.source_audio_path} Artist Title")],
    )
    assert claim_active_job(other.id) is False

    release_active_job(job.id)
    update_job(job.id, status="failed", finished_at="2026-05-19T08:01:00+00:00", error="done")
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])
    assert get_state("active_job_id") is None
    assert claim_active_job(other.id) is True


def test_active_review_job_uses_explicit_active_job_id(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", tmp_path / "forge.sqlite3")
    init_db()
    older = _make_job(tmp_path, stem="older")
    update_job(older.id, status="running", started_at="2026-05-19T07:00:00+00:00")
    current = _make_job(tmp_path, stem="current", artist="Current", title="Live")
    update_job(current.id, status="reviewing", started_at="2026-05-19T08:00:00+00:00")
    set_state("active_job_id", current.id)

    cmdline = f"karaoke-gen -y {current.source_audio_path} Current Live"
    monkeypatch.setattr(
        "packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes",
        lambda: [(999, cmdline)],
    )

    selected = active_review_job()

    assert selected is not None
    assert selected.id == current.id


def test_submit_redirects_when_active_job_exists(client, tmp_path, monkeypatch):
    job = _make_job(tmp_path, stem="active")
    update_job(job.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    set_state("active_job_id", job.id)
    monkeypatch.setattr(
        "packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes",
        lambda: [(1, f"karaoke-gen -y {job.source_audio_path} Artist Title")],
    )

    response = client.post(
        "/karaoke-forge/jobs",
        data={"artist": "New", "title": "Song", "lyrics": "line"},
        files={"audio": ("test.mp3", b"abc", "audio/mpeg")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/jobs/{job.id}?blocked=active")
