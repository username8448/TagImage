from typing import Any, Iterable, Optional

from ..config import JOB_STATE_RUNNING, JOB_TYPE_RESCAN
from ..contracts.rescan import build_rescan_payload
from ..repo.db import enqueue_job, list_jobs, serialize_job


def enqueue_rescan_job(root_path: str, *, max_attempts: Optional[int] = None) -> dict[str, Any]:
    payload = build_rescan_payload(root_path)
    if max_attempts is None:
        return enqueue_job(JOB_TYPE_RESCAN, payload)
    return enqueue_job(
        JOB_TYPE_RESCAN,
        payload,
        max_attempts=max_attempts,
    )


def enqueue_rescan_for_roots(root_paths: Iterable[str], *, max_attempts: Optional[int] = None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for root_path in root_paths:
        jobs.append(enqueue_rescan_job(str(root_path), max_attempts=max_attempts))
    return jobs


def get_running_rescan_job() -> Optional[dict[str, Any]]:
    running = list_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_RUNNING, limit=1)
    if not running:
        return None
    return serialize_job(running[0])


def build_rescan_response(
    *,
    running_job: Optional[dict[str, Any]] = None,
    queued_jobs: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    if running_job is not None:
        return {"ok": False, "message": "Scan already running", "job_id": running_job.get("id")}

    jobs = queued_jobs or []
    return {
        "ok": True,
        "job_id": jobs[0]["id"] if jobs else None,
        "job_ids": [job["id"] for job in jobs],
    }
