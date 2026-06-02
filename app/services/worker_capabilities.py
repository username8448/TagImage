import os
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no"}


def _thumb_rust_supported() -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    cargo_toml = repo_root / "rust" / "thumb-worker" / "Cargo.toml"
    rust_binary = repo_root / "rust" / "thumb-worker" / "target" / "release" / "imgviewer-thumb-worker"
    return cargo_toml.exists() or rust_binary.exists()


def _scanner_rust_supported() -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    cargo_toml = repo_root / "rust" / "scanner-worker" / "Cargo.toml"
    rust_binary = repo_root / "rust" / "thumb-worker" / "target" / "release" / "imgviewer-scanner-worker"
    return cargo_toml.exists() or rust_binary.exists()


def _metadata_rust_supported() -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    cargo_toml = repo_root / "rust" / "metadata-worker" / "Cargo.toml"
    rust_binary = repo_root / "rust" / "thumb-worker" / "target" / "release" / "imgviewer-metadata-worker"
    return cargo_toml.exists() or rust_binary.exists()


def get_worker_capabilities() -> dict[str, Any]:
    legacy_python = _env_bool("IMGVIEWER_LEGACY_PYTHON", False)
    thumb_mode = (
        os.getenv("IMGVIEWER_THUMB_JOB_MODE", "sync" if legacy_python else "queue")
        .strip()
        .lower()
    )
    thumb_sync_fallback = _env_bool("IMGVIEWER_THUMB_SYNC_FALLBACK", False)
    inline_worker_enabled = _env_bool("IMGVIEWER_INLINE_WORKER", legacy_python)
    rust_scanner_enabled = _env_bool("IMGVIEWER_RUST_SCANNER", not legacy_python)
    metadata_worker_enabled = _env_bool("IMGVIEWER_METADATA_WORKER", not legacy_python)
    metadata_authoritative = metadata_worker_enabled and _env_bool(
        "IMGVIEWER_METADATA_AUTHORITATIVE",
        not legacy_python,
    )
    metadata_mode = "not_enabled"
    if metadata_worker_enabled:
        metadata_mode = "authoritative" if metadata_authoritative else "shadow"

    return {
        "thumb": {
            "mode": thumb_mode,
            "rust_supported": _thumb_rust_supported(),
            "python_fallback": thumb_sync_fallback,
        },
        "rescan": {
            "mode": "rust" if rust_scanner_enabled else "python",
            "rust_supported": _scanner_rust_supported(),
            "inline_worker": inline_worker_enabled,
        },
        "metadata": {
            "mode": metadata_mode,
            "rust_supported": _metadata_rust_supported(),
            "authoritative": metadata_authoritative,
        },
        "hash": {
            "mode": "not_enabled",
            "rust_supported": False,
        },
    }
