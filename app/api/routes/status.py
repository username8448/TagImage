from typing import Any

from fastapi import APIRouter

from ... import state
from ...config import (
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_TYPE_RESCAN,
    JOB_TYPE_THUMB,
    RESCAN_WORKER_EXPECTED,
    THUMB_JOB_MODE,
    THUMB_WORKER_EXPECTED,
)
from ...repo.db import count_jobs, db_health, list_jobs, serialize_job
from ...services.app_state import get_roots, restore_root_from_session
from ...services.worker_capabilities import get_worker_capabilities

router = APIRouter()


def _rescan_status() -> dict[str, Any]:
    running = list_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_RUNNING, limit=1)
    if running:
        current = serialize_job(running[0])
        progress = current["progress"]
        return {
            "running": True,
            "queued": False,
            "job_id": current["id"],
            "total": progress["total"],
            "done": progress["done"],
            "error": None,
        }

    queued = list_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_QUEUED, limit=1)
    if queued:
        current = serialize_job(queued[0])
        progress = current["progress"]
        return {
            "running": False,
            "queued": True,
            "job_id": current["id"],
            "total": progress["total"],
            "done": progress["done"],
            "error": None,
        }

    latest = list_jobs(job_type=JOB_TYPE_RESCAN, limit=1)
    if latest:
        current = serialize_job(latest[0])
        progress = current["progress"]
        return {
            "running": False,
            "queued": False,
            "job_id": current["id"],
            "total": progress["total"],
            "done": progress["done"],
            "error": current.get("error"),
        }

    return {"running": False, "queued": False, "job_id": None, "total": 0, "done": 0, "error": None}


def _status_workers_and_queues() -> dict[str, Any]:
    thumb_running = count_jobs(job_type=JOB_TYPE_THUMB, state=JOB_STATE_RUNNING)
    thumb_queued = count_jobs(job_type=JOB_TYPE_THUMB, state=JOB_STATE_QUEUED)
    rescan_running = count_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_RUNNING)
    rescan_queued = count_jobs(job_type=JOB_TYPE_RESCAN, state=JOB_STATE_QUEUED)
    return {
        "workers": {
            "rescan_worker_expected": RESCAN_WORKER_EXPECTED,
            "thumb_worker_expected": THUMB_WORKER_EXPECTED,
            "capabilities": get_worker_capabilities(),
        },
        "queues": {
            "rescan_queue_depth": rescan_queued,
            "rescan_running": rescan_running,
            "thumb_queue_depth": thumb_queued,
            "thumb_running": thumb_running,
            "thumb_mode": THUMB_JOB_MODE,
            "degraded": bool(THUMB_WORKER_EXPECTED and THUMB_JOB_MODE == "queue" and thumb_running == 0 and thumb_queued > 0),
        },
    }


@router.get("/api/status")
async def get_status():
    health = db_health()
    if health["db_ready"] and state.ROOT_FOLDER is None and not get_roots():
        try:
            restore_root_from_session()
        except Exception:
            pass
    roots = [str(item) for item in get_roots()]
    root = str(state.ROOT_FOLDER) if state.ROOT_FOLDER is not None else (roots[-1] if roots else None)
    scan = _rescan_status()
    extra = _status_workers_and_queues()
    return {
        "ready": bool(root) and not scan["running"] and not scan.get("queued", False),
        "root": root,
        "root_paths": roots,
        **scan,
        **health,
        **extra,
    }
