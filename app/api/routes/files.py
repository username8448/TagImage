import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...repo.content import get_image_record

router = APIRouter()


@router.get("/file/{img_id}")
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
