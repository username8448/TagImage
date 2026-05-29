from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


def _find_index_html() -> Path:
    root = Path(__file__).resolve().parent.parent.parent.parent
    for path in [STATIC_DIR / "index.html", root / "index.html"]:
        if path.exists():
            return path
    return STATIC_DIR / "index.html"


def mount_static(app: FastAPI) -> None:
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@router.get("/")
async def serve_index():
    html = _find_index_html()
    if not html.exists():
        return JSONResponse(
            {"error": "index.html not found. Place it inside static/ next to main.py."},
            status_code=500,
        )
    return FileResponse(html)
