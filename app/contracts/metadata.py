from typing import Any, Optional, TypedDict


class MetadataJobPayload(TypedDict):
    image_id: str
    root_path: str
    path: str


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int")
    return value


def build_metadata_payload(*, image_id: str, root_path: str, path: str) -> MetadataJobPayload:
    payload: MetadataJobPayload = {
        "image_id": _require_string(image_id, "image_id"),
        "root_path": _require_string(root_path, "root_path"),
        "path": _require_string(path, "path"),
    }
    return payload


def parse_metadata_payload(payload: Any) -> MetadataJobPayload:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    payload_keys = set(payload.keys())
    required_keys = {"image_id", "root_path", "path"}
    if payload_keys != required_keys:
        raise ValueError("payload keys must match MetadataJobPayload contract")
    parsed: MetadataJobPayload = {
        "image_id": _require_string(payload["image_id"], "image_id"),
        "root_path": _require_string(payload["root_path"], "root_path"),
        "path": _require_string(payload["path"], "path"),
    }
    return parsed


def metadata_dedupe_key(
    image_id: str,
    mtime: Optional[int] = None,
    path: Optional[str] = None,
) -> str:
    normalized_image_id = _require_string(image_id, "image_id")
    if mtime is not None:
        normalized_mtime = _require_int(mtime, "mtime")
        return f"metadata:{normalized_image_id}:{normalized_mtime}"
    if path is not None:
        _require_string(path, "path")
    return f"metadata:{normalized_image_id}"
