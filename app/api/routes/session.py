from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import state
from ...repo.content import load_session, save_session_fields
from ...repo.db import _model_dump
from ...services.app_state import get_roots, restore_root_from_session, set_root

router = APIRouter()


class SessionPatch(BaseModel):
    root_path: Optional[str] = None
    root_paths: Optional[list[str]] = None
    search_tags: Optional[list[str]] = None
    search_mode: Optional[str] = None
    last_image_id: Optional[str] = None
    tabs: Optional[list[dict[str, Any]]] = None
    active_tab_id: Optional[str] = None


@router.get("/api/session")
async def get_session():
    try:
        session = load_session()
        if state.ROOT_FOLDER is None and not get_roots():
            restore_root_from_session()
            session = load_session()
        return session
    except Exception as exc:
        raise HTTPException(500, f"Database error: {exc}")


@router.patch("/api/session")
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
