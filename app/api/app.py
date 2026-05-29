import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import JOB_CLEANUP_INTERVAL_SEC
from ..repo.db import cleanup_old_jobs, close_db_pools, ensure_db_ready
from ..services.app_state import restore_root_from_session
from ..services.inline_worker import start_inline_worker_if_enabled
from .routes import files, folders, images, jobs, session, static, status, tags, thumbs

executor = ThreadPoolExecutor(max_workers=4)
cleanup_task: Optional[asyncio.Task] = None


def _pick_folder_native() -> dict[str, Any]:
    return folders.pick_folder_native_default()


async def _job_cleanup_loop() -> None:
    while True:
        try:
            cleanup_old_jobs()
        except Exception:
            pass
        await asyncio.sleep(JOB_CLEANUP_INTERVAL_SEC)


def create_app() -> FastAPI:
    app = FastAPI(title="TagImage")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    folders.configure_runtime(executor=executor, pick_folder_callback=lambda: _pick_folder_native())
    thumbs.configure_runtime(executor=executor)

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

    @app.on_event("shutdown")
    async def shutdown_tasks() -> None:
        global cleanup_task
        if cleanup_task is not None:
            cleanup_task.cancel()
            cleanup_task = None
        close_db_pools()

    app.include_router(folders.router)
    app.include_router(status.router)
    app.include_router(session.router)
    app.include_router(images.router)
    app.include_router(tags.router)
    app.include_router(thumbs.router)
    app.include_router(files.router)
    app.include_router(jobs.router)
    app.include_router(static.router)
    static.mount_static(app)
    return app


app = create_app()
