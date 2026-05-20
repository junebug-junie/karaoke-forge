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
    ENABLE_VOCAL_TIMING_REFINE,
    ENABLE_VOCAL_VISUALIZER,
    KARAOKE_GEN_BIN,
    ROOT_DIR,
    SPACY_MODEL,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_SIZE,
)
from .store import Job, update_job
from .review_contract import review_gate_decision
from .vocal_visualizer import apply_vocal_visualizer_to_render_paths

REVIEW_SERVER_PORT = int(os.getenv("KARAOKE_REVIEW_SERVER_PORT", "8000"))
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
COPY_ALL_RENDER_OUTPUTS = os.getenv("KARAOKE_FORGE_COPY_ALL_RENDER_OUTPUTS", "").strip().lower() in {"1", "true", "yes", "on"}
AUTO_ADVANCE_STDIN = os.getenv("KARAOKE_FORGE_AUTO_ADVANCE_STDIN", "").strip().lower() in {"1", "true", "yes", "on"}
PROMPT_DEFAULT_ACCEPTS = "\n" * 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_render_candidates(source_dir: Path) -> Iterable[Path]:
    for pattern in ("*.mp4", "*.mkv", "*.mov", "*.webm", "*.avi"):
        yield from source_dir.rglob(pattern)


def _existing_video_path(raw: str, source_dir: Path) -> Path | None:
    cleaned = raw.strip().strip("'\"`[](),")
    if not cleaned:
        return None
    path = Path(cleaned)
    if not path.is_absolute():
        path = source_dir / cleaned
    try:
        path = path.resolve()
    except OSError:
        return None
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return None
    return path if path.exists() else None


def _logged_video_paths_from_line(line: str, source_dir: Path) -> list[Path]:
    found: list[Path] = []
    lower = line.lower()
    source_text = str(source_dir)
    for suffix in VIDEO_SUFFIXES:
        start_at = 0
        while True:
            suffix_idx = lower.find(suffix, start_at)
            if suffix_idx == -1:
                break
            end_idx = suffix_idx + len(suffix)

            # Prefer a full path rooted in the current run directory. This keeps
            # spaces in filenames intact and avoids parsing log prefixes as paths.
            start_idx = line.find(source_text, 0, end_idx)
            if start_idx == -1:
                # Fallback for absolute paths outside source_dir.
                start_idx = line.rfind(" /", 0, end_idx)
                if start_idx != -1:
                    start_idx += 1
                else:
                    start_idx = line.find("/", 0, end_idx)
            if start_idx != -1:
                path = _existing_video_path(line[start_idx:end_idx], source_dir)
                if path is not None and path not in found:
                    found.append(path)
            start_at = end_idx
    return found


def _extract_final_video_paths_from_log(log_path: Path, source_dir: Path) -> list[Path]:
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    final_markers = [
        idx
        for idx, line in enumerate(lines)
        if "final videos" in line.lower() or "final video" in line.lower()
    ]
    if final_markers:
        scan_lines = lines[final_markers[-1] :]
    else:
        scan_lines = [
            line
            for line in lines
            if "video rendered successfully" in line.lower() or "karaoke finalisation complete" in line.lower()
        ]

    paths: list[Path] = []
    for line in scan_lines:
        if line.startswith("[exit_code]"):
            break
        for path in _logged_video_paths_from_line(line, source_dir):
            if path not in paths:
                paths.append(path)
    return paths


def _rank_primary_render(path: Path) -> tuple[int, int, float, int]:
    name = path.name.lower()
    finalish = int("final" in name or "karaoke" in name)
    with_vocals = int("with vocal" in name or "with_vocals" in name or "vocals" in name)
    try:
        stat = path.stat()
        return finalish, with_vocals, stat.st_mtime, stat.st_size
    except OSError:
        return finalish, with_vocals, 0.0, 0


def _select_render_candidates(source_dir: Path, log_path: Path) -> tuple[list[Path], dict[str, object]]:
    logged_final_paths = _extract_final_video_paths_from_log(log_path, source_dir)
    if logged_final_paths:
        candidates = logged_final_paths
        discovery_source = "log_final_videos"
    else:
        all_candidates = list(_iter_render_candidates(source_dir))
        finalish_candidates = [
            path
            for path in all_candidates
            if "karaoke_finalise" in {part.lower() for part in path.parts}
            or "final" in path.name.lower()
            or "karaoke" in path.name.lower()
        ]
        candidates = finalish_candidates or all_candidates
        discovery_source = "filesystem_finalish_fallback" if finalish_candidates else "filesystem_all_videos_fallback"

    candidates = sorted(dict.fromkeys(candidates), key=lambda path: str(path))
    selected = candidates if COPY_ALL_RENDER_OUTPUTS else ([max(candidates, key=_rank_primary_render)] if candidates else [])
    return selected, {
        "discovery_source": discovery_source,
        "copy_all_render_outputs": COPY_ALL_RENDER_OUTPUTS,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "candidate_paths": [str(path) for path in candidates],
        "selected_paths": [str(path) for path in selected],
    }


def _copy_render_outputs(job: Job, source_dir: Path, log_path: Path) -> tuple[list[str], dict[str, object]]:
    output_dir = Path(job.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # The UI-facing render directory is a copy/cache for the current job. Clear
    # old copied videos before copying this run's selected outputs so stale
    # attempts do not appear as fresh final renders.
    for old in output_dir.iterdir():
        if old.is_file() and old.suffix.lower() in VIDEO_SUFFIXES:
            old.unlink()

    selected, debug = _select_render_candidates(source_dir, log_path)
    copied: list[str] = []
    for candidate in selected:
        if output_dir in candidate.parents:
            continue
        target = output_dir / candidate.name
        if target.exists():
            target = output_dir / f"{candidate.stem}-{candidate.stat().st_mtime_ns}{candidate.suffix}"
        shutil.copy2(candidate, target)
        copied.append(str(target))

    debug = {**debug, "copied_paths": copied, "output_dir": str(output_dir)}
    return copied, debug


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
    env.setdefault("WHISPER_LANGUAGE", WHISPER_LANGUAGE)
    env.setdefault("ENABLE_LOCAL_WHISPER", ENABLE_LOCAL_WHISPER)
    env.setdefault("SPACY_MODEL", SPACY_MODEL)
    env.setdefault("KARAOKE_DEFAULT_INSTRUMENTAL_SELECTION", DEFAULT_INSTRUMENTAL_SELECTION)
    env.setdefault("KARAOKE_DEFAULT_SUBTITLE_OFFSET_MS", str(DEFAULT_SUBTITLE_OFFSET_MS))
    env.setdefault("KARAOKE_FORGE_PATCH_KARAOKE_GEN", "1")

    # Prefer vendor/karaoke-gen (Forge patches) over pip site-packages, and load
    # sitecustomize.py from the Forge repo root in karaoke-gen subprocesses.
    existing_pythonpath = env.get("PYTHONPATH", "")
    root = str(ROOT_DIR)
    vendor_kg = str(ROOT_DIR / "vendor" / "karaoke-gen")
    prefix = f"{vendor_kg}:{root}"
    env["PYTHONPATH"] = prefix if not existing_pythonpath else f"{prefix}:{existing_pythonpath}"
    return env


def run_job(job_id: str) -> Job:
    from .job_lifecycle import claim_active_job, reconcile_orphaned_jobs, release_active_job
    from .store import get_job

    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    reconcile_orphaned_jobs()
    if not claim_active_job(job.id):
        return update_job(
            job.id,
            status="failed",
            finished_at=_now(),
            error="Another Forge job is already active",
            metadata={**(job.metadata or {}), "blocked_by_active_job": True},
        )

    try:
        return _run_job_body(job)
    finally:
        release_active_job(job.id)


def _run_job_body(job: Job) -> Job:
    from .store import get_job

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
            log.write("[mode] karaoke-gen -y mode enabled; Forge holds stdin open for review completion\n")
            log.write(f"[models] whisper_model_size={WHISPER_MODEL_SIZE} whisper_language={WHISPER_LANGUAGE} spacy_model={SPACY_MODEL}\n")
            log.write(f"[defaults] instrumental_selection={DEFAULT_INSTRUMENTAL_SELECTION} subtitle_offset_ms={DEFAULT_SUBTITLE_OFFSET_MS}\n")
            log.write(
                f"[patch] vendor_karaoke_gen={ROOT_DIR / 'vendor' / 'karaoke-gen'} "
                f"vocal_timing={ENABLE_VOCAL_TIMING_REFINE} vocal_visualizer={ENABLE_VOCAL_VISUALIZER}\n"
            )
            log.write(f"[run-dir] {run_dir}\n")
            log.write(f"[stdin] auto_advance={AUTO_ADVANCE_STDIN}\n")
            log.write(f"[renders] copy_all_render_outputs={COPY_ALL_RENDER_OUTPUTS}\n")
            killed = kill_stale_review_server(log)
            if killed:
                log.write(f"[review-port] killed stale pid(s): {killed}\n")
            log.write("\n")
            log.flush()

            if AUTO_ADVANCE_STDIN:
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
                returncode = proc.returncode
            else:
                # Do not feed blank lines into karaoke-gen. Feeding newlines here
                # can accept the review prompt/default correction state before
                # the Forge review UI has a chance to mutate corrected_segments.
                proc = subprocess.Popen(
                    cmd,
                    cwd=run_dir,
                    env=build_environment(),
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                returncode = proc.wait()

            log.write(f"\n\n[exit_code] {returncode}\n")

        latest = get_job(job.id)
        metadata = latest.metadata if latest else job.metadata
        render_source_dir = Path(metadata.get("run_dir") or run_dir)

        allowed, gate_error = review_gate_decision(
            returncode=returncode,
            review_seen=bool(metadata.get("review_completed_seen")),
            require_review_payload=True,
        )

        copied_outputs: list[str] = []
        render_debug: dict[str, object] = {}
        if returncode == 0 and not allowed:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    "[renders] karaoke-gen exited before Forge observed review completion; "
                    "refusing to collect default renders\n"
                )
            metadata = {
                **metadata,
                "returncode": returncode,
                "render_outputs": [],
                "render_source_dir": str(render_source_dir),
                "review_gate_blocked_render": True,
                "auto_advance_stdin": AUTO_ADVANCE_STDIN,
            }
            return update_job(
                job.id,
                status="failed",
                finished_at=_now(),
                error=gate_error,
                metadata=metadata,
            )

        copied_outputs, render_debug = _copy_render_outputs(latest or job, render_source_dir, log_path)
        if ENABLE_VOCAL_VISUALIZER and copied_outputs:
            copied_outputs, vocal_viz_debug = apply_vocal_visualizer_to_render_paths(
                latest or job,
                copied_outputs,
            )
            render_debug = {**render_debug, **vocal_viz_debug}
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"[vocal-visualizer] enabled applied={vocal_viz_debug.get('vocal_visualizer_applied')} "
                    f"paths={vocal_viz_debug.get('vocal_visualizer_paths', [])}\n"
                )
        metadata = {
            **metadata,
            "returncode": returncode,
            "render_outputs": copied_outputs,
            "render_source_dir": str(render_source_dir),
            "render_discovery": render_debug,
            "auto_advance_stdin": AUTO_ADVANCE_STDIN,
        }

        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"[renders] discovery_source={render_debug.get('discovery_source')} "
                f"candidate_count={render_debug.get('candidate_count')} "
                f"selected_count={render_debug.get('selected_count')}\n"
            )
            for copied in copied_outputs:
                log.write(f"[renders] copied {copied}\n")

        if returncode != 0:
            return update_job(
                job.id,
                status="failed",
                finished_at=_now(),
                error=f"karaoke-gen exited with {returncode}; see log",
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