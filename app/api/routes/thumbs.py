import asyncio
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ...config import (
    INDEX_DIR_NAME,
    THUMB_JOB_MODE,
    THUMB_MAX_ATTEMPTS,
    THUMB_MAX_SIZE,
    THUMB_POLL_MS,
    THUMB_SYNC_FALLBACK,
    THUMB_WAIT_MS,
    THUMBS_DIR_NAME,
)
from ...repo.content import get_image_record
from ...services.app_state import require_root, require_roots
from ...services.scanner import make_thumb_sync
from ...services.thumbnail_jobs_service import enqueue_thumb_job, enqueue_thumb_rebuild_jobs

router = APIRouter()


class ThumbRebuildRequest(BaseModel):
    stale_only: bool = True
    limit: Optional[int] = None


_thumb_executor: Optional[ThreadPoolExecutor] = None


def configure_runtime(*, executor: ThreadPoolExecutor) -> None:
    global _thumb_executor
    _thumb_executor = executor


@router.get("/thumb-file/{img_id}.jpg")
async def get_thumb_file(img_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", img_id):
        raise HTTPException(404, "Not found")
    root = require_root()
    thumb_path = root / INDEX_DIR_NAME / THUMBS_DIR_NAME / f"{img_id}.jpg"
    try:
        stat_result = os.stat(thumb_path)
    except FileNotFoundError:
        raise HTTPException(404, "Not found")
    return FileResponse(
        thumb_path,
        stat_result=stat_result,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/thumb/{img_id}")
async def get_thumb(img_id: str):
    started = time.perf_counter()
    image = get_image_record(img_id)
    db_elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if image is None:
        raise HTTPException(404, "Not found")
    root = Path(image["root_path"])
    thumb_path = root / image["thumb"]

    if thumb_path.exists():
        return FileResponse(
            thumb_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Elapsed-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
                "X-Db-Lookup-Ms": str(db_elapsed_ms),
            },
        )

    orig = root / image["path"]
    if not orig.exists():
        raise HTTPException(404, "Original not found")

    if THUMB_JOB_MODE == "queue":
        mtime = int(image["mtime"] or int(orig.stat().st_mtime))
        job = enqueue_thumb_job(
            image_id=img_id,
            root_path=str(root),
            path=image["path"],
            thumb=image["thumb"],
            mtime=mtime,
            max_size=THUMB_MAX_SIZE,
            priority=30,
            max_attempts=THUMB_MAX_ATTEMPTS,
        )

        if THUMB_WAIT_MS > 0:
            deadline = asyncio.get_event_loop().time() + (THUMB_WAIT_MS / 1000.0)
            while asyncio.get_event_loop().time() < deadline:
                if thumb_path.exists():
                    break
                await asyncio.sleep(THUMB_POLL_MS / 1000.0)

        if thumb_path.exists():
            return FileResponse(
                thumb_path,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Elapsed-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
                    "X-Db-Lookup-Ms": str(db_elapsed_ms),
                },
            )

        if THUMB_SYNC_FALLBACK:
            await asyncio.get_event_loop().run_in_executor(_thumb_executor, make_thumb_sync, orig, thumb_path)
            if thumb_path.exists():
                return FileResponse(
                    thumb_path,
                    media_type="image/jpeg",
                    headers={
                        "Cache-Control": "public, max-age=86400",
                        "X-Elapsed-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
                        "X-Db-Lookup-Ms": str(db_elapsed_ms),
                    },
                )

        return JSONResponse(
            {
                "ok": False,
                "pending": True,
                "job_id": job.get("id"),
                "retry_after_ms": THUMB_POLL_MS,
                "thumb_url": f"/thumb/{img_id}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "db_lookup_ms": db_elapsed_ms,
            },
            status_code=202,
            headers={
                "Server-Timing": f"thumb-db;dur={db_elapsed_ms}",
                "X-Db-Lookup-Ms": str(db_elapsed_ms),
            },
        )

    await asyncio.get_event_loop().run_in_executor(_thumb_executor, make_thumb_sync, orig, thumb_path)
    if not thumb_path.exists():
        raise HTTPException(500, "Could not generate thumbnail")
    return FileResponse(
        thumb_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Elapsed-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
            "X-Db-Lookup-Ms": str(db_elapsed_ms),
        },
    )


@router.post("/api/thumbs/rebuild")
async def enqueue_thumb_rebuild(req: ThumbRebuildRequest):
    roots = require_roots()
    result = enqueue_thumb_rebuild_jobs(
        roots,
        stale_only=req.stale_only,
        limit=req.limit,
        max_size=THUMB_MAX_SIZE,
        priority=20,
        max_attempts=THUMB_MAX_ATTEMPTS,
    )

    return {"ok": True, **result}
