from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .review_contract import review_gate_decision
from .store import Job, list_jobs, update_job


def active_review_job() -> Job | None:
    jobs = list_jobs(limit=50)
    if not jobs:
        return None

    priority = {"reviewing": 0, "running": 1, "queued": 2}
    candidates = [job for job in jobs if job.status in priority]
    if not candidates:
        return None

    incomplete = [job for job in candidates if not (job.metadata or {}).get("review_completed_seen")]
    pool = incomplete or candidates

    def sort_key(job: Job) -> tuple[int, str, str]:
        return (
            priority.get(job.status, 9),
            job.started_at or "",
            job.updated_at or "",
        )

    return sorted(pool, key=sort_key)[-1]


def mark_review_complete(payload: dict[str, Any] | None = None) -> Job | None:
    job = active_review_job()
    if job is None:
        return None
    metadata = dict(job.metadata or {})
    completion_payload = payload or {}
    metadata.update(
        {
            "review_completed_seen": True,
            "review_completed_source": "forge_native_complete",
            "review_completed_at": datetime.now(timezone.utc).isoformat(),
            "review_completed_debug": completion_payload.get("review_completed_debug") or completion_payload,
            "resolved_corrected_segments_count": completion_payload.get("resolved_corrected_segments_count"),
            "resolved_segments_digest": completion_payload.get("resolved_segments_digest"),
            "resolved_segments_preview": completion_payload.get("resolved_segments_preview"),
        }
    )
    status = "reviewing" if job.status == "running" else job.status
    return update_job(job.id, status=status, metadata=metadata)


def fail_if_render_without_review(result: Job) -> Job:
    metadata = dict(result.metadata or {})
    allowed, error = review_gate_decision(
        returncode=int(metadata.get("returncode") or 0),
        review_seen=bool(metadata.get("review_completed_seen")),
        require_review_payload=True,
    )
    if result.status == "done" and not allowed:
        metadata["review_gate_blocked_render"] = True
        return update_job(result.id, status="failed", error=error, metadata=metadata)
    return result
