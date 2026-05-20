from __future__ import annotations

from . import web as web_module
from .review_gate import fail_if_render_without_review
from .runner import run_job as original_run_job
from .store import Job

app = web_module.app


def run_job(job_id: str) -> Job:
    return fail_if_render_without_review(original_run_job(job_id))


web_module.run_job = run_job
