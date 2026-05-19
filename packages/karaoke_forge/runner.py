from __future__ import annotations

import os
import shutil
import subprocess
from datetime import timezone, datetime
from pathlib import Path
from typing import Iterable

from .config import (
    ENABLE_LOCAL_WHISPER,
    KARAOKE_GEN_BIN,
    WHISPER_DEVICE,
    WHISPER_MODEL_SIZE,
)
from .store import Job, update_job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_render_candidates(job_dir: Path) -> Iterable[Path]:
    for pattern in ("*.mp4", "*.mkv", "*.mov", "*.webm", "*.avi"):
        yield from job_dir.rglob(pattern)


def _copy_render_outputs(job: Job) -> list[str]:
    job_dir = Path(job.job_dir)
    output_dir = Path(job.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for candidate in _iter_render_candidates(job_dir):
        if output_dir in candidate.parents:
            continue
        target = output_dir / candidate.name
        if target.exists():
            target = output_dir / f"{candidate.stem}-{candidate.stat().st_mtime_ns}{candidate.suffix}"
        shutil.copy2(candidate, target)
        copied.append(str(target))
    return copied


def build_karaoke_gen_command(job: Job) -> list[str]:
    cmd = [
        KARAOKE_GEN_BIN,
        job.source_audio_path,
        job.artist,
        job.title,
        "--log_level",
        "debug",
    ]
    if job.lyrics_path:
        cmd.extend(["--lyrics_file", job.lyrics_path])
    return cmd


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("WHISPER_MODEL_SIZE", WHISPER_MODEL_SIZE)
    env.setdefault("WHISPER_DEVICE", WHISPER_DEVICE)
    env.setdefault("ENABLE_LOCAL_WHISPER", ENABLE_LOCAL_WHISPER)
    return env


def run_job(job_id: str) -> Job:
    from .store import get_job

    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    job_dir = Path(job.job_dir)
    log_path = Path(job.log_path)
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_karaoke_gen_command(job)
    update_job(
        job.id,
        status="running",
        started_at=_now(),
        metadata={**job.metadata, "command": cmd},
    )

    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write("$ " + " ".join(cmd) + "\n\n")
            log.flush()
            proc = subprocess.run(
                cmd,
                cwd=job_dir,
                env=build_environment(),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            log.write(f"\n\n[exit_code] {proc.returncode}\n")

        latest = get_job(job.id)
        metadata = latest.metadata if latest else job.metadata
        copied_outputs = _copy_render_outputs(latest or job)
        metadata = {**metadata, "returncode": proc.returncode, "render_outputs": copied_outputs}

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
