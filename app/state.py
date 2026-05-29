from pathlib import Path
from typing import Optional

from .config import INDEX_DIR_NAME, THUMBS_DIR_NAME

ROOT_FOLDER: Optional[Path] = None
ROOT_FOLDERS: list[Path] = []
INDEX_DIR: Optional[Path] = None
THUMBS_DIR: Optional[Path] = None


def ensure_dirs() -> None:
    if INDEX_DIR is None or THUMBS_DIR is None:
        return
    INDEX_DIR.mkdir(exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


def _activate_root(root: Path) -> Path:
    global ROOT_FOLDER, INDEX_DIR, THUMBS_DIR
    ROOT_FOLDER = root
    INDEX_DIR = root / INDEX_DIR_NAME
    THUMBS_DIR = INDEX_DIR / THUMBS_DIR_NAME
    ensure_dirs()
    return root


def set_root_paths(folder: str, *, append: bool = False) -> Path:
    global ROOT_FOLDERS
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    if append:
        if root not in ROOT_FOLDERS:
            ROOT_FOLDERS.append(root)
    else:
        ROOT_FOLDERS = [root]
    _activate_root(root)
    return root


def set_root_folders(folders: list[str]) -> list[Path]:
    global ROOT_FOLDERS
    roots: list[Path] = []
    for folder in folders:
        root = Path(folder).expanduser().resolve()
        if root.is_dir() and root not in roots:
            roots.append(root)
    ROOT_FOLDERS = roots
    if roots:
        _activate_root(roots[-1])
    return roots
