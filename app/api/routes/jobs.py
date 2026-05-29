from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ...config import RESCAN_MAX_ATTEMPTS
from ...repo.db import get_job, list_jobs, serialize_job
from ...services.app_state import require_roots
from ...services.rescan_jobs_service import (
    build_rescan_response,
    enqueue_rescan_for_roots,
    get_running_rescan_job,
)

router = APIRouter()


@router.post("/api/rescan")
async def rescan():
    roots = require_roots()
    running = get_running_rescan_job()
    if running is not None:
        return build_rescan_response(running_job=running)

    jobs = enqueue_rescan_for_roots((str(root) for root in roots), max_attempts=RESCAN_MAX_ATTEMPTS)
    return build_rescan_response(queued_jobs=jobs)


@router.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = serialize_job(get_job(job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    return {"job": job}


@router.get("/api/jobs")
async def get_jobs(
    type: Optional[str] = Query(None, alias="type"),
    state: Optional[str] = Query(None),
    limit: int = Query(50),
):
    jobs = [serialize_job(item) for item in list_jobs(job_type=type, state=state, limit=limit)]
    return {"jobs": jobs}
