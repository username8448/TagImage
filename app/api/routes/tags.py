from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...repo.content import (
    clean_tag_list,
    create_user_tag_entry,
    delete_tag_definition,
    get_image_record,
    normalize_tag,
    replace_image_user_tags,
    rows_to_images,
    tag_summary_by_norm,
    tag_summary_rows,
    update_tag_definition,
)
from ...repo.db import _model_dump

router = APIRouter()


class TagRequest(BaseModel):
    tags: list[str]


class TagCreateRequest(BaseModel):
    name: str


class TagUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


@router.get("/api/tags")
async def list_all_tags():
    return {"tags": tag_summary_rows()}


@router.post("/api/tags")
async def create_tag(req: TagCreateRequest):
    return {"tag": create_user_tag_entry(req.name), "tags": tag_summary_rows()}


@router.patch("/api/tags/{tag}")
async def update_tag(tag: str, req: TagUpdateRequest):
    payload = _model_dump(req)
    if "name" not in payload and "color" not in payload:
        summary = tag_summary_by_norm(normalize_tag(tag))
        if summary is None:
            raise HTTPException(404, "Tag not found")
        return {"tag": summary, "tags": tag_summary_rows()}
    summary = update_tag_definition(
        tag,
        name=payload.get("name"),
        color=payload.get("color"),
        has_name="name" in payload,
        has_color="color" in payload,
    )
    return {"tag": summary, "tags": tag_summary_rows()}


@router.delete("/api/tags/{tag}")
async def delete_tag(tag: str):
    delete_tag_definition(tag)
    return {"ok": True, "tags": tag_summary_rows()}


@router.post("/api/tag/{img_id}")
async def set_tags(img_id: str, req: TagRequest):
    image = get_image_record(img_id)
    if image is None:
        raise HTTPException(404, "Image not found")

    user_tags = clean_tag_list(req.tags)
    replace_image_user_tags(img_id, user_tags)

    refreshed = rows_to_images([image])[0]
    return {
        "id": img_id,
        "tags": refreshed["tags"],
        "auto_tags": refreshed["auto_tags"],
        "folder_tags": refreshed["auto_tags"],
        "user_tags": refreshed["user_tags"],
    }
