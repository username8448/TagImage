from pathlib import Path
from typing import Optional

from .config import INDEX_DIR_NAME, THUMBS_DIR_NAME

ROOT_FOLDER: Optional[Path] = None
INDEX_DIR: Optional[Path] = None
THUMBS_DIR: Optional[Path] = None


def ensure_dirs() -> None:
    if INDEX_DIR is None or THUMBS_DIR is None:
        return
    INDEX_DIR.mkdir(exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


def set_root_paths(folder: str) -> Path:
    global ROOT_FOLDER, INDEX_DIR, THUMBS_DIR
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    ROOT_FOLDER = root
    INDEX_DIR = root / INDEX_DIR_NAME
    THUMBS_DIR = INDEX_DIR / THUMBS_DIR_NAME
    ensure_dirs()
    return root
