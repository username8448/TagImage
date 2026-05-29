from pathlib import Path
from typing import Any, Mapping

from ..config import JOB_TYPE_THUMB, THUMB_MAX_ATTEMPTS, THUMB_MAX_SIZE
from ..contracts.thumbs import ThumbJobPayload, build_thumb_payload, thumb_dedupe_key
from ..repo.content import list_thumb_rebuild_rows
from ..repo.db import enqueue_job


def thumb_job_payload_for_image(
    *,
    image_id: str,
    root_path: str,
    path: str,
    thumb: str,
    mtime: int,
    max_size: tuple[int, int] | list[int] = THUMB_MAX_SIZE,
) -> ThumbJobPayload:
    return build_thumb_payload(
        image_id=image_id,
        root_path=root_path,
        path=path,
        thumb=thumb,
        mtime=mtime,
        max_size=max_size,
    )


def thumb_job_payload_for_row(
    row: Mapping[str, Any],
    *,
    max_size: tuple[int, int] | list[int] = THUMB_MAX_SIZE,
) -> ThumbJobPayload:
    return thumb_job_payload_for_image(
        image_id=row["id"],
        root_path=row["root_path"],
        path=row["path"],
        thumb=row["thumb"],
        mtime=int(row["mtime"] or 0),
        max_size=max_size,
    )


def enqueue_thumb_job(
    *,
    image_id: str,
    root_path: str,
    path: str,
    thumb: str,
    mtime: int,
    max_size: tuple[int, int] | list[int] = THUMB_MAX_SIZE,
    priority: int = 20,
    max_attempts: int = THUMB_MAX_ATTEMPTS,
) -> dict[str, Any]:
    payload = thumb_job_payload_for_image(
        image_id=image_id,
        root_path=root_path,
        path=path,
        thumb=thumb,
        mtime=mtime,
        max_size=max_size,
    )
    return enqueue_job(
        JOB_TYPE_THUMB,
        payload,
        priority=priority,
        max_attempts=max_attempts,
        dedupe_key=thumb_dedupe_key(image_id, mtime),
    )


def enqueue_thumb_rebuild_jobs(
    roots: list[Path],
    *,
    stale_only: bool,
    limit: int | None,
    max_size: tuple[int, int] | list[int] = THUMB_MAX_SIZE,
    priority: int = 20,
    max_attempts: int = THUMB_MAX_ATTEMPTS,
) -> dict[str, int]:
    root_map = {str(root): root for root in roots}
    rows = list_thumb_rebuild_rows(list(root_map), limit=limit)

    enqueued = 0
    queued_existing = 0
    skipped = 0
    for row in rows:
        root_str = row["root_path"]
        root = root_map.get(root_str)
        if root is None:
            skipped += 1
            continue

        src = root / row["path"]
        thumb = root / row["thumb"]
        if stale_only:
            if not src.exists():
                skipped += 1
                continue
            if thumb.exists() and int(thumb.stat().st_mtime) >= int(row["mtime"] or 0):
                skipped += 1
                continue

        payload = thumb_job_payload_for_row(row, max_size=max_size)
        job = enqueue_thumb_job(
            image_id=payload["image_id"],
            root_path=payload["root_path"],
            path=payload["path"],
            thumb=payload["thumb"],
            mtime=payload["mtime"],
            max_size=payload["max_size"],
            priority=priority,
            max_attempts=max_attempts,
        )
        if bool(job.get("__deduped")):
            queued_existing += 1
        enqueued += 1

    return {
        "enqueued": enqueued,
        "queued_existing": queued_existing,
        "skipped": skipped,
        "total": len(rows),
    }
