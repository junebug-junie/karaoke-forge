from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from packages.karaoke_forge import vocal_visualizer
from packages.karaoke_forge.vocal_visualizer import (
    apply_vocal_visualizer_to_render_paths,
    apply_vocal_visualizer_to_video,
    build_vocal_overlay_filter,
    overlay_bar_height,
)


def test_overlay_bar_height_clamped() -> None:
    assert overlay_bar_height(480) == 60
    assert overlay_bar_height(1080) == 108
    assert overlay_bar_height(2160) == 120
    assert overlay_bar_height(0) == 80


def test_build_vocal_overlay_filter_uses_showfreqs_and_bottom_overlay() -> None:
    graph = build_vocal_overlay_filter(Path("/tmp/vocals.wav"), 1080)
    assert "showfreqs" in graph
    assert "overlay" in graph
    assert "y=H-h" in graph
    assert "x=(W-w)/2" in graph
    assert "108x" in graph or "s=640x108" in graph
    assert graph.endswith("[vout]")


def test_apply_vocal_visualizer_to_video_invokes_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_mp4 = tmp_path / "in.mp4"
    vocal_wav = tmp_path / "vocals.wav"
    output_mp4 = tmp_path / "out.mp4"
    input_mp4.write_bytes(b"video")
    vocal_wav.write_bytes(b"wav")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vocal_visualizer.subprocess, "run", fake_run)

    assert apply_vocal_visualizer_to_video(input_mp4, vocal_wav, output_mp4, video_height=720) is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "ffmpeg"
    assert str(input_mp4) in cmd
    assert str(vocal_wav) in cmd
    assert "-filter_complex" in cmd
    assert "showfreqs" in cmd[cmd.index("-filter_complex") + 1]


def test_apply_vocal_visualizer_to_render_paths_noop_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vocal_visualizer, "ENABLE_VOCAL_VISUALIZER", False)
    render_mp4 = tmp_path / "render.mp4"
    render_mp4.write_bytes(b"orig")

    ffmpeg_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ffmpeg_calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(vocal_visualizer.subprocess, "run", fake_run)

    class _Job:
        metadata = {"run_dir": str(tmp_path)}

    paths, debug = apply_vocal_visualizer_to_render_paths(_Job(), [str(render_mp4)])
    assert paths == [str(render_mp4)]
    assert debug["vocal_visualizer_enabled"] is False
    assert ffmpeg_calls == []


def test_apply_vocal_visualizer_to_render_paths_overlays_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vocal_visualizer, "ENABLE_VOCAL_VISUALIZER", True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    vocal_wav = run_dir / "Artist (Vocals).wav"
    vocal_wav.write_bytes(b"wav")
    render_mp4 = tmp_path / "render.mp4"
    render_mp4.write_bytes(b"orig")

    def fake_apply(input_mp4, vocal_path, output_mp4, **kwargs):
        assert input_mp4 == render_mp4
        assert vocal_path == vocal_wav
        output_mp4.write_bytes(b"overlayed")
        return True

    monkeypatch.setattr(vocal_visualizer, "apply_vocal_visualizer_to_video", fake_apply)

    class _Job:
        metadata = {"run_dir": str(run_dir)}

    paths, debug = apply_vocal_visualizer_to_render_paths(_Job(), [str(render_mp4)])
    assert paths == [str(render_mp4)]
    assert debug["vocal_visualizer_enabled"] is True
    assert debug["vocal_visualizer_applied"] is True
    assert render_mp4.read_bytes() == b"overlayed"


def test_runner_invokes_vocal_visualizer_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.karaoke_forge.runner import run_job
    from packages.karaoke_forge.store import create_job, get_job, init_db, update_job

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
    source_video = run_dir / "Artist - Title (Final Karaoke).mp4"
    source_video.write_bytes(b"fresh")

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

    viz_calls: list[tuple] = []

    def fake_apply_vocal(job_obj, paths):
        viz_calls.append((job_obj.id, list(paths)))
        return paths, {"vocal_visualizer_enabled": True, "vocal_visualizer_applied": True}

    monkeypatch.setattr("packages.karaoke_forge.runner.ENABLE_VOCAL_VISUALIZER", True)
    monkeypatch.setattr(
        "packages.karaoke_forge.runner.apply_vocal_visualizer_to_render_paths",
        fake_apply_vocal,
    )

    class FakeProc:
        returncode = 0

        def wait(self):
            log_path.write_text(
                "Video rendered successfully: " + str(source_video) + "\n",
                encoding="utf-8",
            )
            return 0

    monkeypatch.setattr("packages.karaoke_forge.runner.subprocess.Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr("packages.karaoke_forge.runner.build_karaoke_gen_command", lambda j: ["echo"])
    monkeypatch.setattr("packages.karaoke_forge.runner.kill_stale_review_server", lambda log: [])

    result = run_job(job.id)
    assert result.status == "done"
    assert len(viz_calls) == 1
    assert viz_calls[0][0] == job.id
    assert viz_calls[0][1]


def test_runner_skips_vocal_visualizer_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.karaoke_forge.runner import run_job
    from packages.karaoke_forge.store import create_job, init_db

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
    source_video = run_dir / "final.mp4"
    source_video.write_bytes(b"fresh")

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

    viz_calls: list = []

    def fake_apply_vocal(job_obj, paths):
        viz_calls.append(paths)
        return paths, {}

    monkeypatch.setattr("packages.karaoke_forge.runner.ENABLE_VOCAL_VISUALIZER", False)
    monkeypatch.setattr(
        "packages.karaoke_forge.runner.apply_vocal_visualizer_to_render_paths",
        fake_apply_vocal,
    )

    class FakeProc:
        returncode = 0

        def wait(self):
            log_path.write_text("final: " + str(source_video) + "\n", encoding="utf-8")
            return 0

    monkeypatch.setattr("packages.karaoke_forge.runner.subprocess.Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr("packages.karaoke_forge.runner.build_karaoke_gen_command", lambda j: ["echo"])
    monkeypatch.setattr("packages.karaoke_forge.runner.kill_stale_review_server", lambda log: [])

    result = run_job(job.id)
    assert result.status == "done"
    assert viz_calls == []
