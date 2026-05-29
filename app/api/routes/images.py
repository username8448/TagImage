import time
from typing import Optional

from fastapi import APIRouter, Query, Response

from ...repo.content import query_images_page, rows_to_images
from ...services.app_state import require_roots

router = APIRouter()


@router.get("/api/images")
async def list_images(
    response: Response,
    tags: Optional[str] = Query(None, description="comma-separated tags to filter"),
    include_tags: Optional[str] = Query(None, description="comma-separated tags that all must match"),
    exclude_tags: Optional[str] = Query(None, description="comma-separated tags that must not match"),
    match_mode: Optional[str] = Query(None, description="'any' or 'all' include-tag matching"),
    mode: Optional[str] = Query("any", description="'any' or 'all'"),
    limit: Optional[int] = Query(None, description="page size"),
    cursor: Optional[str] = Query(None, description="cursor from previous page"),
    sort: Optional[str] = Query("date_desc", description="path/date/size sort mode"),
    include_total: bool = Query(False, description="include full filtered total count"),
):
    roots = require_roots()
    started = time.perf_counter()
    page = query_images_page(
        root_paths=[str(root) for root in roots],
        tags=tags,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        match_mode=match_mode,
        mode=mode,
        limit=limit,
        cursor=cursor,
        sort=sort,
        include_total=include_total,
    )
    items = rows_to_images(page["rows"])
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["Server-Timing"] = f"api-images;dur={elapsed_ms}"
    return {
        "items": items,
        "page": page["page"],
        "images": items,
        "total": page["page"]["total"],
        "elapsed_ms": elapsed_ms,
    }
