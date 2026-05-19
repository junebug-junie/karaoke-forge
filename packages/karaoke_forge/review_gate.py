from __future__ import annotations

from typing import Any

from .review_contract import review_gate_decision
from .store import Job, list_jobs, update_job


def active_review_job() -> Job | None:
    for job in list_jobs(limit=20):
        if job.status in {"queued", "running", "reviewing"}:
            return job
    jobs = list_jobs(limit=1)
    return jobs[0] if jobs else None


def mark_review_complete(payload: dict[str, Any] | None = None) -> Job | None:
    job = active_review_job()
    if job is None:
        return None
    metadata = dict(job.metadata or {})
    metadata.update(
        {
            "review_completed_seen": True,
            "review_completed_source": "forge_native_complete",
            "review_completion_payload": payload or {},
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
