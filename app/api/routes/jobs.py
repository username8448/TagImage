from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ...config import JOB_STATE_RUNNING, JOB_TYPE_RESCAN, RESCAN_MAX_ATTEMPTS
from ...repo.db import enqueue_job, get_job, list_jobs, serialize_job
from ...services.app_state import require_roots

router = APIRouter()


@router.post("/api/rescan")
async def rescan():
    roots = require_roots()
    running = list_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_RUNNING, limit=1)
    if running:
        active = serialize_job(running[0])
        return {"ok": False, "message": "Scan already running", "job_id": active["id"]}

    jobs = [
        enqueue_job(
            JOB_TYPE_RESCAN,
            {"root_path": str(root)},
            max_attempts=RESCAN_MAX_ATTEMPTS,
        )
        for root in roots
    ]
    return {"ok": True, "job_id": jobs[0]["id"] if jobs else None, "job_ids": [job["id"] for job in jobs]}


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
