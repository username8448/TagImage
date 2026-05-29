import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import state
from ..config import (
    INDEX_DIR_NAME,
    JOB_CLEANUP_INTERVAL_SEC,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_TYPE_RESCAN,
    JOB_TYPE_THUMB,
    RESCAN_MAX_ATTEMPTS,
    RESCAN_WORKER_EXPECTED,
    THUMB_JOB_MODE,
    THUMB_MAX_ATTEMPTS,
    THUMB_MAX_SIZE,
    THUMBS_DIR_NAME,
    THUMB_POLL_MS,
    THUMB_SYNC_FALLBACK,
    THUMB_WAIT_MS,
    THUMB_WORKER_EXPECTED,
)
from ..repo.content import (
    clean_tag_list,
    folder_tree_rows,
    get_image_record,
    load_session,
    normalize_color,
    normalize_tag,
    query_images_page,
    replace_image_tags,
    rows_to_images,
    save_session_fields,
    tag_summary_by_norm,
    tag_summary_rows,
)
from ..repo.db import (
    _model_dump,
    cleanup_old_jobs,
    close_db_pools,
    count_jobs,
    db_connect,
    db_health,
    enqueue_job,
    ensure_db_ready,
    get_job,
    list_jobs,
    serialize_job,
)
from ..services.app_state import get_roots, require_root, require_roots, restore_root_from_session, set_root
from ..services.inline_worker import start_inline_worker_if_enabled
from ..services.scanner import make_thumb_sync

try:
    from psycopg.rows import dict_row
except ImportError:
    dict_row = None


app = FastAPI(title="TagImage")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)
cleanup_task: Optional[asyncio.Task] = None


class TagRequest(BaseModel):
    tags: List[str]


class TagCreateRequest(BaseModel):
    name: str


class TagUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class FolderRequest(BaseModel):
    path: str


class SessionPatch(BaseModel):
    root_path: Optional[str] = None
    root_paths: Optional[List[str]] = None
    search_tags: Optional[List[str]] = None
    search_mode: Optional[str] = None
    last_image_id: Optional[str] = None
    tabs: Optional[list[dict[str, Any]]] = None
    active_tab_id: Optional[str] = None


class ThumbRebuildRequest(BaseModel):
    stale_only: bool = True
    limit: Optional[int] = None


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _pick_folder_with_command(command: list[str]) -> tuple[Optional[str], bool, Optional[str]]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return None, False, None
    except Exception as exc:
        return None, False, str(exc)
    path = result.stdout.strip()
    if result.returncode == 0 and path:
        return str(Path(path).expanduser().resolve()), False, None
    if result.returncode == 1 and not result.stderr.strip():
        return None, True, None
    if result.returncode == 1 and "cancel" in result.stderr.lower():
        return None, True, None
    return None, False, result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"


def _pick_folder_native() -> dict[str, Any]:
    initial = str(Path.home())
    for binary, command in (
        ("kdialog", ["kdialog", "--getexistingdirectory", initial]),
        ("zenity", ["zenity", "--file-selection", "--directory", "--title", "Выберите папку", "--filename", f"{initial}/"]),
        ("yad", ["yad", "--file-selection", "--directory", "--title", "Выберите папку", "--filename", f"{initial}/"]),
        ("qarma", ["qarma", "--file-selection", "--directory", "--title", "Выберите папку", "--filename", f"{initial}/"]),
        ("matedialog", ["matedialog", "--file-selection", "--directory", "--title", "Выберите папку", "--filename", f"{initial}/"]),
    ):
        if not shutil.which(binary):
            continue
        path, cancelled, error = _pick_folder_with_command(command)
        if path:
            return {"path": path}
        if cancelled:
            return {"path": None, "cancelled": True}

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=initial, title="Выберите папку")
        root.destroy()
        if selected:
            return {"path": str(Path(selected).expanduser().resolve())}
        return {"path": None, "cancelled": True}
    except Exception:
        pass

    detail = (
        "Нативный выбор папки недоступен. "
        "Установите kdialog, zenity, yad, qarma или matedialog, либо введите путь вручную."
    )
    raise RuntimeError(detail)


@app.on_event("startup")
async def startup_restore_session() -> None:
    global cleanup_task
    try:
        ensure_db_ready()
        restore_root_from_session()
        start_inline_worker_if_enabled()
        if cleanup_task is None or cleanup_task.done():
            cleanup_task = asyncio.create_task(_job_cleanup_loop())
    except Exception:
        pass


async def _job_cleanup_loop() -> None:
    while True:
        try:
            cleanup_old_jobs()
        except Exception:
            pass
        await asyncio.sleep(JOB_CLEANUP_INTERVAL_SEC)


@app.on_event("shutdown")
async def shutdown_tasks() -> None:
    global cleanup_task
    if cleanup_task is not None:
        cleanup_task.cancel()
        cleanup_task = None
    close_db_pools()


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


@app.post("/api/folder")
async def set_folder(req: FolderRequest):
    try:
        root = set_root(req.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Database error: {exc}")

    job = enqueue_job(JOB_TYPE_RESCAN, {"root_path": str(root)})
    roots = [str(item) for item in get_roots()]
    return {"ok": True, "root": str(root), "root_paths": roots, "job_id": job["id"]}


@app.post("/api/folder/pick")
async def pick_folder(request: Request):
    if not _is_local_request(request):
        raise HTTPException(403, "Folder picker is available only from localhost")
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, _pick_folder_native)
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))


@app.get("/api/status")
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


@app.get("/api/session")
async def get_session():
    try:
        session = load_session()
        if state.ROOT_FOLDER is None and not get_roots():
            restore_root_from_session()
            session = load_session()
        return session
    except Exception as exc:
        raise HTTPException(500, f"Database error: {exc}")


@app.patch("/api/session")
async def patch_session(req: SessionPatch):
    payload = _model_dump(req)
    if "root_path" in payload and payload["root_path"]:
        try:
            set_root(payload["root_path"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        payload["root_path"] = str(state.ROOT_FOLDER)
        payload["root_paths"] = [str(item) for item in get_roots()]
    try:
        return save_session_fields(**payload)
    except Exception as exc:
        raise HTTPException(500, f"Database error: {exc}")


@app.get("/api/images")
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
    payload = {
        "items": items,
        "page": page["page"],
        # legacy compatibility
        "images": items,
        "total": page["page"]["total"],
        "elapsed_ms": elapsed_ms,
    }
    return payload


@app.get("/api/folders")
async def list_folders():
    try:
        roots = [str(root) for root in require_roots()]
    except HTTPException:
        roots = []
    return {"roots": roots, "items": folder_tree_rows(roots)}


@app.get("/api/tags")
async def list_all_tags():
    return {"tags": tag_summary_rows()}


@app.post("/api/tags")
async def create_tag(req: TagCreateRequest):
    ensure_db_ready()
    cleaned = clean_tag_list([req.name])
    if not cleaned:
        raise HTTPException(400, "Tag name is empty")
    with db_connect() as conn:
        with conn.cursor() as cur:
            from ..repo.content import ensure_tag

            ensure_tag(cur, cleaned[0], user_defined=True)
    tag = tag_summary_by_norm(normalize_tag(cleaned[0]))
    return {
        "tag": tag
        or {
            "name": cleaned[0],
            "color": None,
            "image_count": 0,
            "auto_count": 0,
            "user_count": 0,
            "user_defined": True,
            "is_auto": False,
        },
        "tags": tag_summary_rows(),
    }


@app.patch("/api/tags/{tag}")
async def update_tag(tag: str, req: TagUpdateRequest):
    ensure_db_ready()
    payload = _model_dump(req)
    if "name" not in payload and "color" not in payload:
        summary = tag_summary_by_norm(normalize_tag(tag))
        if summary is None:
            raise HTTPException(404, "Tag not found")
        return {"tag": summary, "tags": tag_summary_rows()}

    new_name: Optional[str] = None
    if "name" in payload and payload["name"] is not None:
        cleaned = clean_tag_list([payload["name"]])
        if not cleaned:
            raise HTTPException(400, "Tag name is empty")
        new_name = cleaned[0]
    has_color = "color" in payload
    new_color = normalize_color(payload.get("color")) if has_color else None
    source_norm = normalize_tag(tag)
    final_norm = source_norm

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                WHERE t.normalized = %s
                GROUP BY t.id
                """,
                (source_norm,),
            )
            source = cur.fetchone()
            if source is None:
                raise HTTPException(404, "Tag not found")

            target_id = source["id"]
            if new_name is not None:
                new_norm = normalize_tag(new_name)
                cur.execute("DELETE FROM suppressed_auto_tags WHERE normalized = %s", (new_norm,))
                source_is_auto = int(source["auto_count"] or 0) > 0
                if source_is_auto and new_name != source["name"]:
                    raise HTTPException(400, "Folder tags cannot be renamed")

                if new_norm == source["normalized"]:
                    if new_name != source["name"]:
                        cur.execute(
                            "UPDATE tags SET name = %s, user_defined = true WHERE id = %s",
                            (new_name, source["id"]),
                        )
                    elif not source_is_auto:
                        cur.execute("UPDATE tags SET user_defined = true WHERE id = %s", (source["id"],))
                    final_norm = new_norm
                else:
                    if source_is_auto:
                        raise HTTPException(400, "Folder tags cannot be renamed")
                    cur.execute(
                        """
                        SELECT
                            t.id,
                            t.name,
                            t.normalized,
                            COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                        FROM tags t
                        LEFT JOIN image_tags it ON it.tag_id = t.id
                        WHERE t.normalized = %s
                        GROUP BY t.id
                        """,
                        (new_norm,),
                    )
                    target = cur.fetchone()
                    if target is not None:
                        if int(target["auto_count"] or 0) > 0:
                            raise HTTPException(400, "Cannot merge into a folder tag")
                        cur.execute(
                            """
                            INSERT INTO image_tags (image_id, tag_id, kind, created_at)
                            SELECT image_id, %s, kind, created_at
                            FROM image_tags
                            WHERE tag_id = %s
                            ON CONFLICT DO NOTHING
                            """,
                            (target["id"], source["id"]),
                        )
                        cur.execute("DELETE FROM tags WHERE id = %s", (source["id"],))
                        cur.execute("UPDATE tags SET user_defined = true WHERE id = %s", (target["id"],))
                        target_id = target["id"]
                        final_norm = target["normalized"]
                    else:
                        cur.execute(
                            "UPDATE tags SET name = %s, normalized = %s, user_defined = true WHERE id = %s",
                            (new_name, new_norm, source["id"]),
                        )
                        target_id = source["id"]
                        final_norm = new_norm

            if has_color:
                cur.execute("UPDATE tags SET color = %s WHERE id = %s", (new_color, target_id))

    summary = tag_summary_by_norm(final_norm)
    return {"tag": summary, "tags": tag_summary_rows()}


@app.delete("/api/tags/{tag}")
async def delete_tag(tag: str):
    ensure_db_ready()
    norm = normalize_tag(tag)
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                WHERE t.normalized = %s
                GROUP BY t.id
                """,
                (norm,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Tag not found")
            if int(row["auto_count"] or 0) > 0:
                cur.execute(
                    """
                    INSERT INTO suppressed_auto_tags (normalized, name)
                    VALUES (%s, %s)
                    ON CONFLICT (normalized) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (row["normalized"], row["name"]),
                )
            cur.execute("DELETE FROM tags WHERE id = %s", (row["id"],))
    return {"ok": True, "tags": tag_summary_rows()}


@app.post("/api/tag/{img_id}")
async def set_tags(img_id: str, req: TagRequest):
    ensure_db_ready()
    image = get_image_record(img_id)
    if image is None:
        raise HTTPException(404, "Image not found")

    user_tags = clean_tag_list(req.tags)
    with db_connect() as conn:
        with conn.cursor() as cur:
            replace_image_tags(cur, img_id, user_tags, "user")

    refreshed = rows_to_images([image])[0]
    return {
        "id": img_id,
        "tags": refreshed["tags"],
        "auto_tags": refreshed["auto_tags"],
        "folder_tags": refreshed["auto_tags"],
        "user_tags": refreshed["user_tags"],
    }


@app.get("/thumb-file/{img_id}.jpg")
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


@app.get("/thumb/{img_id}")
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
        dedupe_key = f"thumb:{img_id}:{mtime}"
        job = enqueue_job(
            JOB_TYPE_THUMB,
            {
                "image_id": img_id,
                "root_path": str(root),
                "path": image["path"],
                "thumb": image["thumb"],
                "mtime": mtime,
                "max_size": [int(THUMB_MAX_SIZE[0]), int(THUMB_MAX_SIZE[1])],
            },
            priority=30,
            max_attempts=THUMB_MAX_ATTEMPTS,
            dedupe_key=dedupe_key,
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
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, make_thumb_sync, orig, thumb_path)
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

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, make_thumb_sync, orig, thumb_path)
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


@app.get("/file/{img_id}")
async def get_file(img_id: str):
    image = get_image_record(img_id)
    if image is None:
        raise HTTPException(404, "Not found")
    file_path = Path(image["root_path"]) / image["path"]
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    mt, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        file_path,
        media_type=mt or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/api/rescan")
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


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = serialize_job(get_job(job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    return {"job": job}


@app.get("/api/jobs")
async def get_jobs(
    type: Optional[str] = Query(None, alias="type"),
    state: Optional[str] = Query(None),
    limit: int = Query(50),
):
    jobs = [serialize_job(item) for item in list_jobs(job_type=type, state=state, limit=limit)]
    return {"jobs": jobs}


@app.post("/api/thumbs/rebuild")
async def enqueue_thumb_rebuild(req: ThumbRebuildRequest):
    roots = require_roots()
    root_map = {str(root): root for root in roots}
    root_strings = list(root_map)
    max_rows = 0 if req.limit is None else max(0, min(int(req.limit), 200000))

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT id, root_path, path, thumb, mtime
                FROM images
                WHERE root_path = ANY(%s) AND hidden = false
                ORDER BY root_path, lower(path), path
            """
            params: list[Any] = [root_strings]
            if max_rows > 0:
                sql += " LIMIT %s"
                params.append(max_rows)
            cur.execute(sql, params)
            rows = cur.fetchall()

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
        if req.stale_only:
            if not src.exists():
                skipped += 1
                continue
            if thumb.exists() and int(thumb.stat().st_mtime) >= int(row["mtime"] or 0):
                skipped += 1
                continue
        job = enqueue_job(
            JOB_TYPE_THUMB,
            {
                "image_id": row["id"],
                "root_path": root_str,
                "path": row["path"],
                "thumb": row["thumb"],
                "mtime": int(row["mtime"] or 0),
                "max_size": [int(THUMB_MAX_SIZE[0]), int(THUMB_MAX_SIZE[1])],
            },
            priority=20,
            max_attempts=THUMB_MAX_ATTEMPTS,
            dedupe_key=f"thumb:{row['id']}:{int(row['mtime'] or 0)}",
        )
        if bool(job.get("__deduped")):
            queued_existing += 1
        enqueued += 1

    return {"ok": True, "enqueued": enqueued, "queued_existing": queued_existing, "skipped": skipped, "total": len(rows)}


STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


def _find_index_html() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    for path in [STATIC_DIR / "index.html", root / "index.html"]:
        if path.exists():
            return path
    return STATIC_DIR / "index.html"


@app.get("/")
async def serve_index():
    html = _find_index_html()
    if not html.exists():
        return JSONResponse(
            {"error": "index.html not found. Place it inside static/ next to main.py."},
            status_code=500,
        )
    return FileResponse(html)


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
