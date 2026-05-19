from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from packages.karaoke_forge.review_contract import (
    find_corrected_segments_summary,
    review_gate_decision,
    review_payload_summary,
    segments_preview_texts,
    segments_text_digest,
)
from packages.karaoke_forge.review_proxy import (
    _apply_segment_edits_to_corrected_segments,
    _delete_corrected_segment_indexes,
    _find_corrected_segments,
    _resync_segment_words_to_text,
)
from packages.karaoke_forge.runner import run_job
from packages.karaoke_forge.store import create_job, get_job, init_db, update_job
from packages.karaoke_forge.web import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("KARAOKE_FORGE_BASE_PATH", "/karaoke-forge")
    init_db()
    return TestClient(app)


def test_finds_top_level_corrected_segments():
    payload = {"corrected_segments": [{"text": "a"}, {"text": "b"}]}
    assert find_corrected_segments_summary(payload) == (2, "$.corrected_segments")


def test_finds_nested_corrected_segments():
    payload = {"data": {"review": {"corrected_segments": [{"text": "a"}]}}}
    assert find_corrected_segments_summary(payload) == (1, "$.data.review.corrected_segments")


def test_missing_corrected_segments_is_not_ready():
    payload = {"segments": [{"text": "whisper only"}]}
    summary = review_payload_summary(payload)
    assert summary["ready"] is False
    assert summary["corrected_segments_count"] == 0
    assert summary["corrected_segments_path"] is None


def test_review_gate_blocks_success_without_review():
    allowed, error = review_gate_decision(returncode=0, review_seen=False, require_review_payload=True)
    assert allowed is False
    assert "refusing to collect default renders" in error


def test_review_gate_allows_success_after_review():
    allowed, error = review_gate_decision(returncode=0, review_seen=True, require_review_payload=True)
    assert allowed is True
    assert error is None


def test_review_gate_preserves_nonzero_exit_failure():
    allowed, error = review_gate_decision(returncode=7, review_seen=True, require_review_payload=True)
    assert allowed is False
    assert "exited with 7" in error


def test_segments_digest_and_preview():
    segments = [{"text": "one"}, {"text": "two"}, {"text": "three"}, {"text": "four"}]
    assert segments_text_digest(segments)
    preview = segments_preview_texts(segments, count=2)
    assert preview["first"] == ["one", "two"]
    assert preview["last"] == ["three", "four"]


def test_text_edit_resyncs_words_for_final_render():
    segment = {
        "text": "old line",
        "start_time": 1.0,
        "end_time": 2.0,
        "words": [
            {"id": "w0", "text": "old", "start_time": 1.0, "end_time": 1.5},
            {"id": "w1", "text": "line", "start_time": 1.5, "end_time": 2.0},
        ],
    }
    payload = {"corrected_segments": [segment]}
    _apply_segment_edits_to_corrected_segments(
        payload,
        [{"index": 0, "text": "new line", "start": "1", "end": "2"}],
    )
    assert segment["text"] == "new line"
    assert segment["words"][0]["text"] == "new line"
    assert segment["words"][0]["start_time"] == 1.0
    assert segment["words"][0]["end_time"] == 2.0


def test_delete_tail_segment_indexes():
    payload = {
        "corrected_segments": [
            {"text": f"line {idx}", "start_time": float(idx), "end_time": float(idx + 1)}
            for idx in range(5)
        ],
    }
    debug = _delete_corrected_segment_indexes(payload, [3, 4])
    corrected = _find_corrected_segments(payload)[0]
    assert debug["tail_trimmed"] == 2
    assert len(corrected) == 3
    assert corrected[-1]["text"] == "line 2"


def test_apply_edits_mutates_corrected_segments_and_mirrors_segments():
    payload = {
        "corrected_segments": [
            {"text": "a", "start_time": 1.0, "end_time": 2.0},
            {"text": "b", "start_time": 3.0, "end_time": 4.0},
        ],
        "segments": [{"text": "old"}],
    }
    debug = _apply_segment_edits_to_corrected_segments(
        payload,
        [{"index": 0, "text": "canonical A", "start": "1", "end": "2"}, {"index": 1, "delete": True}],
    )
    corrected = _find_corrected_segments(payload)[0]
    assert debug["text_edit_count"] == 1
    assert debug["after_corrected_segments_count"] == 1
    assert corrected[0]["text"] == "canonical A"
    assert payload["segments"] is not corrected
    payload["segments"] = corrected
    assert payload["segments"][0]["text"] == "canonical A"


def test_review_page_inline_script_is_valid_javascript(client):
    response = client.get("/karaoke-forge/review")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store, max-age=0"
    html = response.text
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    split_match = re.search(r"canonicalLyricsEl\.value\.split\(([^)]+)\)", script)
    assert split_match is not None
    assert "\n" not in split_match.group(0)
    assert split_match.group(1) == "/\\r?\\n/"
    assert "time-part" in html
    assert "review-table" in html


def test_native_data_requires_corrected_segments(client):
    with patch("packages.karaoke_forge.review_proxy._upstream_json", new=AsyncMock(return_value=(200, {"segments": [{"text": "x"}]}))):
        response = client.get("/karaoke-forge/review/native/data")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_native_data_ready_with_corrected_segments(client):
    payload = {
        "corrected_segments": [{"text": "line", "start_time": 1.0, "end_time": 2.0}],
        "original_segments": [{"text": "whisper"}],
    }
    with patch("packages.karaoke_forge.review_proxy._upstream_json", new=AsyncMock(return_value=(200, payload))):
        response = client.get("/karaoke-forge/review/native/data")
    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is True
    assert body["segments"][0]["text"] == "line"
    assert body["original_segment_texts"] == ["whisper"]


def test_native_data_includes_canonical_lyrics_from_active_job(client, tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    init_db()
    lyrics_path = tmp_path / "lyrics.txt"
    lyrics_path.write_text("Behind the red door\nOn the dirty floor\n", encoding="utf-8")
    job = create_job(
        artist="A.A. Bondy",
        title="Surfer King",
        source_audio_path=tmp_path / "audio.wav",
        lyrics_path=lyrics_path,
        job_dir=tmp_path / "job",
        output_dir=tmp_path / "out",
        log_path=tmp_path / "job.log",
        metadata={"run_dir": str(tmp_path / "run")},
    )
    from packages.karaoke_forge.job_lifecycle import set_active_job_id

    set_active_job_id(job.id)
    payload = {
        "corrected_segments": [{"text": "line", "start_time": 1.0, "end_time": 2.0}],
        "original_segments": [{"text": "whisper"}],
    }
    with patch("packages.karaoke_forge.review_proxy._upstream_json", new=AsyncMock(return_value=(200, payload))):
        response = client.get("/karaoke-forge/review/native/data")
    body = response.json()
    assert response.status_code == 200
    assert body["canonical_lyrics_lines"] == ["Behind the red door", "On the dirty floor"]
    assert body["canonical_lyrics_source"] == str(lyrics_path)
    assert body["canonical_lyrics_job_id"] == job.id


def test_native_complete_persists_review_metadata(client, tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    init_db()
    job = create_job(
        artist="A",
        title="B",
        source_audio_path=tmp_path / "audio.wav",
        lyrics_path=None,
        job_dir=tmp_path / "job",
        output_dir=tmp_path / "out",
        log_path=tmp_path / "job.log",
        metadata={"run_dir": str(tmp_path / "run")},
    )
    update_job(job.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    from packages.karaoke_forge.job_lifecycle import set_active_job_id

    set_active_job_id(job.id)
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])
    correction = {
        "corrected_segments": [{"text": "before", "start_time": 1.0, "end_time": 2.0}],
        "original_segments": [{"text": "before"}],
    }

    async def fake_upstream(path, *, method="GET", json_body=None):
        if path == "/api/correction-data":
            return 200, correction
        if path == "/api/complete" and method == "POST":
            assert json_body["corrected_segments"][0]["text"] == "after"
            assert json_body["segments"] == json_body["corrected_segments"]
            return 200, {"status": "ok"}
        raise AssertionError(path)

    with patch("packages.karaoke_forge.review_proxy._upstream_json", new=AsyncMock(side_effect=fake_upstream)):
        response = client.post(
            "/karaoke-forge/review/native/complete",
            json={"segment_edits": [{"index": 0, "text": "after", "start": "1", "end": "2"}]},
        )

    assert response.status_code == 200
    updated = get_job(job.id)
    assert updated is not None
    assert updated.metadata["review_completed_seen"] is True
    assert updated.metadata["review_completed_source"] == "forge_native_complete"
    assert updated.metadata["resolved_corrected_segments_count"] == 1


def test_runner_refuses_renders_without_review_completion(tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    library = tmp_path / "library"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(library))
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])
    init_db()

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_path = run_dir / "karaoke-gen.log"
    output_dir = library / "renders" / "job"
    output_dir.mkdir(parents=True)
    stale = output_dir / "stale.mp4"
    stale.write_bytes(b"stale")

    job = create_job(
        artist="A",
        title="B",
        source_audio_path=tmp_path / "audio.wav",
        lyrics_path=None,
        job_dir=tmp_path / "job",
        output_dir=output_dir,
        log_path=log_path,
        metadata={"run_dir": str(run_dir)},
    )

    source_video = run_dir / "final.mp4"
    source_video.write_bytes(b"fresh")

    class FakeProc:
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(*args, **kwargs):
        log_path.write_text("[stdin] auto_advance=False\n", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr("packages.karaoke_forge.runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("packages.karaoke_forge.runner.build_karaoke_gen_command", lambda job: ["echo", "test"])
    monkeypatch.setattr("packages.karaoke_forge.runner.kill_stale_review_server", lambda log: [])

    result = run_job(job.id)
    assert result.status == "failed"
    assert "refusing to collect default renders" in (result.error or "")
    assert result.metadata.get("render_outputs") == []
    assert not (output_dir / "final.mp4").exists()
    assert stale.exists()


def test_active_review_job_prefers_running_without_completion(tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    init_db()
    create_job(
        artist="Queued",
        title="Old",
        source_audio_path=tmp_path / "a.wav",
        lyrics_path=None,
        job_dir=tmp_path / "queued",
        output_dir=tmp_path / "out-q",
        log_path=tmp_path / "queued.log",
        metadata={"review_completed_seen": True},
    )
    running = create_job(
        artist="Running",
        title="Live",
        source_audio_path=tmp_path / "b.wav",
        lyrics_path=None,
        job_dir=tmp_path / "running",
        output_dir=tmp_path / "out-r",
        log_path=tmp_path / "running.log",
        metadata={"command": ["karaoke-gen", str(tmp_path / "b.wav"), "Running", "Live"]},
    )
    from packages.karaoke_forge.job_lifecycle import active_review_job, set_active_job_id

    update_job(running.id, status="running", started_at="2026-05-19T08:00:00+00:00")
    set_active_job_id(running.id)
    monkeypatch.setattr(
        "packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes",
        lambda: [(1, f"karaoke-gen -y {tmp_path / 'b.wav'} Running Live")],
    )
    selected = active_review_job()
    assert selected is not None
    assert selected.id == running.id


def test_runner_collects_render_after_review_completion(tmp_path, monkeypatch):
    db_path = tmp_path / "forge.sqlite3"
    library = tmp_path / "library"
    monkeypatch.setenv("KARAOKE_FORGE_DB", str(db_path))
    monkeypatch.setenv("KARAOKE_FORGE_LIBRARY", str(library))
    monkeypatch.setattr("packages.karaoke_forge.store.DB_PATH", db_path)
    monkeypatch.setattr("packages.karaoke_forge.job_lifecycle.list_karaoke_gen_processes", lambda: [])
    init_db()

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_path = run_dir / "karaoke-gen.log"
    output_dir = library / "renders" / "job"
    output_dir.mkdir(parents=True)

    job = create_job(
        artist="A",
        title="B",
        source_audio_path=tmp_path / "audio.wav",
        lyrics_path=None,
        job_dir=tmp_path / "job",
        output_dir=output_dir,
        log_path=log_path,
        metadata={"run_dir": str(run_dir), "review_completed_seen": True},
    )

    source_video = run_dir / "Artist - Title (Final Karaoke).mp4"
    source_video.write_bytes(b"fresh")

    class FakeProc:
        returncode = 0

        def wait(self):
            log_path.write_text(
                "Karaoke finalisation complete\nVideo rendered successfully: "
                + str(source_video)
                + "\n",
                encoding="utf-8",
            )
            return 0

    def fake_popen(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("packages.karaoke_forge.runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("packages.karaoke_forge.runner.build_karaoke_gen_command", lambda job: ["echo", "test"])
    monkeypatch.setattr("packages.karaoke_forge.runner.kill_stale_review_server", lambda log: [])

    result = run_job(job.id)
    assert result.status == "done"
    assert result.metadata.get("render_outputs")
    copied = Path(result.metadata["render_outputs"][0])
    assert copied.exists()
    assert copied.read_bytes() == b"fresh"
