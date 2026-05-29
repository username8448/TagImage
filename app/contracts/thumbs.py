from typing import Any, TypedDict


class ThumbJobPayload(TypedDict):
    image_id: str
    root_path: str
    path: str
    thumb: str
    mtime: int
    max_size: list[int]


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int")
    return value


def _require_max_size(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("max_size must be a list of two ints")
    if len(value) != 2:
        raise ValueError("max_size must contain exactly two ints")
    width = _require_int(value[0], "max_size[0]")
    height = _require_int(value[1], "max_size[1]")
    return [width, height]


def build_thumb_payload(
    *,
    image_id: str,
    root_path: str,
    path: str,
    thumb: str,
    mtime: int,
    max_size: tuple[int, int] | list[int],
) -> ThumbJobPayload:
    payload: ThumbJobPayload = {
        "image_id": _require_string(image_id, "image_id"),
        "root_path": _require_string(root_path, "root_path"),
        "path": _require_string(path, "path"),
        "thumb": _require_string(thumb, "thumb"),
        "mtime": _require_int(mtime, "mtime"),
        "max_size": _require_max_size(list(max_size)),
    }
    return payload


def parse_thumb_payload(payload: Any) -> ThumbJobPayload:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    required_keys = {"image_id", "root_path", "path", "thumb", "mtime", "max_size"}
    payload_keys = set(payload.keys())
    if payload_keys != required_keys:
        raise ValueError("payload keys must match ThumbJobPayload contract")
    parsed: ThumbJobPayload = {
        "image_id": _require_string(payload["image_id"], "image_id"),
        "root_path": _require_string(payload["root_path"], "root_path"),
        "path": _require_string(payload["path"], "path"),
        "thumb": _require_string(payload["thumb"], "thumb"),
        "mtime": _require_int(payload["mtime"], "mtime"),
        "max_size": _require_max_size(payload["max_size"]),
    }
    return parsed


def thumb_dedupe_key(image_id: str, mtime: int) -> str:
    normalized_image_id = _require_string(image_id, "image_id")
    normalized_mtime = _require_int(mtime, "mtime")
    return f"thumb:{normalized_image_id}:{normalized_mtime}"
