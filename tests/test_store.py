from __future__ import annotations

from pathlib import Path

from packages.karaoke_forge import store


def test_create_and_update_job(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(store, "DB_PATH", db_path)

    job_dir = tmp_path / "jobs" / "one"
    output_dir = tmp_path / "renders" / "one"
    log_path = job_dir / "karaoke-gen.log"
    source = tmp_path / "songs" / "input.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not real audio")

    job = store.create_job(
        artist="Artist",
        title="Title",
        source_audio_path=source,
        lyrics_path=None,
        job_dir=job_dir,
        output_dir=output_dir,
        log_path=log_path,
        metadata={"hello": "world"},
    )

    assert job.status == "queued"
    assert job.metadata == {"hello": "world"}

    updated = store.update_job(job.id, status="done", metadata={"render_outputs": ["x.mp4"]})
    assert updated.status == "done"
    assert updated.metadata["render_outputs"] == ["x.mp4"]

    listed = store.list_jobs()
    assert [item.id for item in listed] == [job.id]
