from __future__ import annotations

import subprocess
from pathlib import Path

from .store import Job, clear_state, get_job, get_state, list_jobs, set_state, update_job, utc_now

ACTIVE_JOB_ID_KEY = "active_job_id"
LIVE_WORKER_STATUSES = frozenset({"running", "reviewing"})
ORPHAN_ERROR = "Forge worker lost; job marked orphaned"


def get_active_job_id() -> str | None:
    return get_state(ACTIVE_JOB_ID_KEY)


def set_active_job_id(job_id: str | None) -> None:
    if job_id:
        set_state(ACTIVE_JOB_ID_KEY, job_id)
    else:
        clear_state(ACTIVE_JOB_ID_KEY)


def clear_active_job_id() -> None:
    set_active_job_id(None)


def list_karaoke_gen_processes() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["pgrep", "-af", "karaoke-gen"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []

    processes: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        if "pgrep" in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        cmdline = parts[1]
        if "karaoke-gen" not in cmdline:
            continue
        processes.append((int(parts[0]), cmdline))
    return processes


def job_matches_process(job: Job, cmdline: str) -> bool:
    source = str(job.source_audio_path)
    if source and source in cmdline:
        return True
    name = Path(source).name
    return bool(name and len(name) > 4 and name in cmdline)


def _fail_orphaned_job(job: Job) -> Job:
    metadata = dict(job.metadata or {})
    metadata["orphaned"] = True
    metadata["orphaned_at"] = utc_now()
    return update_job(
        job.id,
        status="failed",
        finished_at=utc_now(),
        error=ORPHAN_ERROR,
        metadata=metadata,
    )


def reconcile_orphaned_jobs(*, limit: int = 100) -> list[str]:
    """Mark running/reviewing rows failed when no live karaoke-gen owns them."""
    processes = list_karaoke_gen_processes()
    live_jobs = [job for job in list_jobs(limit) if job.status in LIVE_WORKER_STATUSES]
    reconciled: list[str] = []

    if not live_jobs:
        clear_active_job_id()
        return reconciled

    if not processes:
        for job in live_jobs:
            _fail_orphaned_job(job)
            reconciled.append(job.id)
        clear_active_job_id()
        return reconciled

    matched_job_ids: set[str] = set()
    for _pid, cmdline in processes:
        for job in live_jobs:
            if job.id in matched_job_ids:
                continue
            if job_matches_process(job, cmdline):
                matched_job_ids.add(job.id)
                break

    for job in live_jobs:
        if job.id not in matched_job_ids:
            _fail_orphaned_job(job)
            reconciled.append(job.id)

    if matched_job_ids:
        matched_jobs = [job for job in live_jobs if job.id in matched_job_ids]
        matched_jobs.sort(key=lambda job: job.started_at or job.updated_at or "", reverse=True)
        set_active_job_id(matched_jobs[0].id)
    else:
        clear_active_job_id()

    return reconciled


def find_blocking_active_job(*, limit: int = 50) -> Job | None:
    reconcile_orphaned_jobs(limit=limit)
    active_id = get_active_job_id()
    if active_id:
        job = get_job(active_id)
        if job is not None and job.status in LIVE_WORKER_STATUSES:
            return job

    candidates = [job for job in list_jobs(limit) if job.status in LIVE_WORKER_STATUSES]
    if not candidates:
        return None
    candidates.sort(key=lambda job: job.started_at or job.updated_at or "", reverse=True)
    return candidates[0]


def claim_active_job(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None:
        return False

    active_id = get_active_job_id()
    if active_id and active_id != job_id:
        existing = get_job(active_id)
        if existing is not None and existing.status in LIVE_WORKER_STATUSES:
            return False

    blocking = find_blocking_active_job()
    if blocking is not None and blocking.id != job_id:
        return False

    set_active_job_id(job_id)
    update_job(
        job_id,
        metadata={
            **(job.metadata or {}),
            "forge_active_job_id": job_id,
            "forge_active_claimed_at": utc_now(),
        },
    )
    return True


def release_active_job(job_id: str) -> None:
    if get_active_job_id() == job_id:
        clear_active_job_id()


def active_review_job(*, limit: int = 50) -> Job | None:
    active_id = get_active_job_id()
    if active_id:
        job = get_job(active_id)
        if job is not None and job.status in LIVE_WORKER_STATUSES:
            return job

    candidates = [job for job in list_jobs(limit) if job.status in LIVE_WORKER_STATUSES]
    if not candidates:
        return None

    candidates.sort(
        key=lambda job: (
            0 if job.status == "reviewing" else 1,
            job.started_at or "",
            job.updated_at or "",
        )
    )
    return candidates[0]
