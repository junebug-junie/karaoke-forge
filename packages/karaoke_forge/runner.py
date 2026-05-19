from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import (
    DEFAULT_INSTRUMENTAL_SELECTION,
    DEFAULT_SUBTITLE_OFFSET_MS,
    ENABLE_LOCAL_WHISPER,
    KARAOKE_GEN_BIN,
    ROOT_DIR,
    SPACY_MODEL,
    WHISPER_DEVICE,
    WHISPER_MODEL_SIZE,
)
from .store import Job, update_job

PROMPT_DEFAULT_ACCEPTS = "\n" * 40
REVIEW_SERVER_PORT = int(os.getenv("KARAOKE_REVIEW_SERVER_PORT", "8000"))
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_render_candidates(source_dir: Path) -> Iterable[Path]:
    for pattern in ("*.mp4", "*.mkv", "*.mov", "*.webm", "*.avi"):
        yield from source_dir.rglob(pattern)


def _copy_render_outputs(job: Job, source_dir: Path) -> list[str]:
    output_dir = Path(job.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # The UI-facing render directory is a copy/cache for the current job. Clear
    # old copied videos before copying this run's outputs so stale attempts do
    # not appear as fresh final renders.
    for old in output_dir.iterdir():
        if old.is_file() and old.suffix.lower() in VIDEO_SUFFIXES:
            old.unlink()

    copied: list[str] = []
    for candidate in _iter_render_candidates(source_dir):
        if output_dir in candidate.parents:
            continue
        target = output_dir / candidate.name
        if target.exists():
            target = output_dir / f"{candidate.stem}-{candidate.stat().st_mtime_ns}{candidate.suffix}"
        shutil.copy2(candidate, target)
        copied.append(str(target))
    return copied


def _pids_listening_on_port(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return sorted(set(pids))


def kill_stale_review_server(log) -> list[int]:
    pids = _pids_listening_on_port(REVIEW_SERVER_PORT)
    if not pids:
        log.write(f"[review-port] port {REVIEW_SERVER_PORT} is free\n")
        log.flush()
        return []

    log.write(f"[review-port] killing stale listener(s) on port {REVIEW_SERVER_PORT}: {pids}\n")
    log.flush()

    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.time() + 5
    while time.time() < deadline:
        remaining = _pids_listening_on_port(REVIEW_SERVER_PORT)
        if not remaining:
            return pids
        time.sleep(0.25)

    remaining = _pids_listening_on_port(REVIEW_SERVER_PORT)
    for pid in remaining:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids


def build_karaoke_gen_command(job: Job) -> list[str]:
    cmd = [
        KARAOKE_GEN_BIN,
        "-y",
        job.source_audio_path,
        job.artist,
        job.title,
        "--log_level",
        "debug",
        "--subtitle_offset_ms",
        str(DEFAULT_SUBTITLE_OFFSET_MS),
    ]
    if job.lyrics_path:
        cmd.extend(["--lyrics_file", job.lyrics_path])
    return cmd


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("WHISPER_MODEL_SIZE", WHISPER_MODEL_SIZE)
    env.setdefault("WHISPER_DEVICE", WHISPER_DEVICE)
    env.setdefault("ENABLE_LOCAL_WHISPER", ENABLE_LOCAL_WHISPER)
    env.setdefault("SPACY_MODEL", SPACY_MODEL)
    env.setdefault("KARAOKE_DEFAULT_INSTRUMENTAL_SELECTION", DEFAULT_INSTRUMENTAL_SELECTION)
    env.setdefault("KARAOKE_DEFAULT_SUBTITLE_OFFSET_MS", str(DEFAULT_SUBTITLE_OFFSET_MS))
    env.setdefault("KARAOKE_FORGE_PATCH_KARAOKE_GEN", "1")

    # Make sitecustomize.py in the Forge repo visible to the karaoke-gen
    # subprocess even though each job runs from its own library/jobs/... cwd.
    existing_pythonpath = env.get("PYTHONPATH", "")
    root = str(ROOT_DIR)
    env["PYTHONPATH"] = root if not existing_pythonpath else f"{root}:{existing_pythonpath}"
    return env


def run_job(job_id: str) -> Job:
    from .store import get_job

    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    job_dir = Path(job.job_dir)
    log_path = Path(job.log_path)
    run_dir = Path(job.metadata.get("run_dir") or log_path.parent)
    job_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_karaoke_gen_command(job)
    update_job(
        job.id,
        status="running",
        started_at=_now(),
        metadata={**job.metadata, "command": cmd, "run_log_path": str(log_path), "run_dir": str(run_dir)},
    )

    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write("$ " + " ".join(cmd) + "\n")
            log.write(f"[run-log] {log_path}\n")
            log.write("[mode] karaoke-gen -y / non-interactive yes mode enabled\n")
            log.write(f"[models] whisper_model_size={WHISPER_MODEL_SIZE} spacy_model={SPACY_MODEL}\n")
            log.write(f"[defaults] instrumental_selection={DEFAULT_INSTRUMENTAL_SELECTION} subtitle_offset_ms={DEFAULT_SUBTITLE_OFFSET_MS}\n")
            log.write(f"[patch] karaoke_gen_output_config=enabled pythonpath_root={ROOT_DIR}\n")
            log.write(f"[run-dir] {run_dir}\n")
            killed = kill_stale_review_server(log)
            if killed:
                log.write(f"[review-port] killed stale pid(s): {killed}\n")
            log.write("\n")
            log.flush()

            proc = subprocess.run(
                cmd,
                cwd=run_dir,
                env=build_environment(),
                input=PROMPT_DEFAULT_ACCEPTS,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            log.write(f"\n\n[exit_code] {proc.returncode}\n")

        latest = get_job(job.id)
        metadata = latest.metadata if latest else job.metadata
        render_source_dir = Path(metadata.get("run_dir") or run_dir)
        copied_outputs = _copy_render_outputs(latest or job, render_source_dir)
        metadata = {
            **metadata,
            "returncode": proc.returncode,
            "render_outputs": copied_outputs,
            "render_source_dir": str(render_source_dir),
        }

        if proc.returncode != 0:
            return update_job(
                job.id,
                status="failed",
                finished_at=_now(),
                error=f"karaoke-gen exited with {proc.returncode}; see log",
                metadata=metadata,
            )

        return update_job(
            job.id,
            status="done",
            finished_at=_now(),
            error=None,
            metadata=metadata,
        )
    except Exception as exc:
        return update_job(
            job.id,
            status="failed",
            finished_at=_now(),
            error=str(exc),
        )