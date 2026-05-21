from pathlib import Path

from fastapi import HTTPException

from ..repo.content import load_session, save_session_fields
from ..repo.db import ensure_db_ready
from ..state import ROOT_FOLDER, set_root_paths


def set_root(folder: str, *, persist: bool = True) -> Path:
    ensure_db_ready()
    root = set_root_paths(folder)
    if persist:
        save_session_fields(root_path=str(root), last_image_id=None)
    return root


def restore_root_from_session() -> None:
    from .. import state

    if state.ROOT_FOLDER is not None:
        return
    session = load_session()
    root = session.get("root_path")
    if root and Path(root).is_dir():
        set_root(root, persist=False)


def require_root() -> Path:
    from .. import state

    if state.ROOT_FOLDER is None:
        restore_root_from_session()
    if state.ROOT_FOLDER is None:
        raise HTTPException(400, "No folder set")
    return state.ROOT_FOLDER
