import os
from pathlib import Path


DEFAULT_DATABASE_URL = "postgresql://imgviewer:imgviewer@127.0.0.1:5432/imgviewer"


def detect_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent

    for candidate in [current, *current.parents]:
        if (candidate / "app").is_dir() and (candidate / "scripts").is_dir():
            return candidate
    return Path(__file__).resolve().parent


def load_env_file(repo_root: Path | None = None, *, override: bool = False) -> bool:
    root = repo_root or detect_repo_root()
    env_path = root / ".env"
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if not override and key in os.environ:
            continue

        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value
    return True


def effective_database_url(*, fallback: str = DEFAULT_DATABASE_URL) -> str:
    load_env_file()
    value = os.getenv("DATABASE_URL", "").strip()
    return value or fallback


def ensure_database_url(*, fallback: str = DEFAULT_DATABASE_URL) -> str:
    database_url = effective_database_url(fallback=fallback)
    os.environ["DATABASE_URL"] = database_url
    return database_url
