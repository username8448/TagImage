from pathlib import Path

from fastapi import HTTPException

from ..repo.content import load_session, save_session_fields
from ..repo.db import ensure_db_ready
from ..state import ROOT_FOLDER, set_root_folders, set_root_paths


def set_root(folder: str, *, persist: bool = True) -> Path:
    ensure_db_ready()
    root = set_root_paths(folder, append=True)
    if persist:
        roots = get_roots()
        save_session_fields(root_path=str(root), root_paths=[str(item) for item in roots], last_image_id=None)
    return root


def get_roots() -> list[Path]:
    from .. import state

    roots = list(state.ROOT_FOLDERS)
    if not roots and state.ROOT_FOLDER is not None:
        roots = [state.ROOT_FOLDER]
    return roots


def restore_root_from_session() -> None:
    from .. import state

    if state.ROOT_FOLDERS:
        return
    session = load_session()
    roots = [str(item) for item in session.get("root_paths") or [] if item]
    if not roots and session.get("root_path"):
        roots = [session["root_path"]]
    restored = set_root_folders(roots)
    if not restored:
        root = session.get("root_path")
        if root and Path(root).is_dir():
            set_root(root, persist=False)


def require_root() -> Path:
    from .. import state

    if state.ROOT_FOLDER is None and not state.ROOT_FOLDERS:
        restore_root_from_session()
    if state.ROOT_FOLDER is None:
        raise HTTPException(400, "No folder set")
    return state.ROOT_FOLDER


def require_roots() -> list[Path]:
    roots = get_roots()
    if not roots:
        restore_root_from_session()
        roots = get_roots()
    if not roots:
        raise HTTPException(400, "No folder set")
    return roots
