import asyncio
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...config import JOB_TYPE_RESCAN
from ...repo.content import folder_tree_rows
from ...repo.db import enqueue_job
from ...services.app_state import get_roots, require_roots, set_root

router = APIRouter()


class FolderRequest(BaseModel):
    path: str


_picker_executor: Optional[ThreadPoolExecutor] = None
_pick_folder_callback: Optional[Callable[[], dict[str, Any]]] = None


def configure_runtime(*, executor: ThreadPoolExecutor, pick_folder_callback: Callable[[], dict[str, Any]]) -> None:
    global _picker_executor, _pick_folder_callback
    _picker_executor = executor
    _pick_folder_callback = pick_folder_callback


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


def pick_folder_native_default() -> dict[str, Any]:
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
        path, cancelled, _error = _pick_folder_with_command(command)
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


@router.post("/api/folder")
async def set_folder_endpoint(req: FolderRequest):
    try:
        root = set_root(req.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Database error: {exc}")

    job = enqueue_job(JOB_TYPE_RESCAN, {"root_path": str(root)})
    roots = [str(item) for item in get_roots()]
    return {"ok": True, "root": str(root), "root_paths": roots, "job_id": job["id"]}


@router.post("/api/folder/pick")
async def pick_folder(request: Request):
    if not _is_local_request(request):
        raise HTTPException(403, "Folder picker is available only from localhost")
    picker = _pick_folder_callback or pick_folder_native_default
    try:
        return await asyncio.get_running_loop().run_in_executor(_picker_executor, picker)
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))


@router.get("/api/folders")
async def list_folders():
    try:
        roots = [str(root) for root in require_roots()]
    except HTTPException:
        roots = []
    return {"roots": roots, "items": folder_tree_rows(roots)}
