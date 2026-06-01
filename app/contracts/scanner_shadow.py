from typing import Any, TypedDict


class ScannerShadowJobPayload(TypedDict):
    root_path: str


def build_scanner_shadow_payload(root_path: str) -> ScannerShadowJobPayload:
    if not isinstance(root_path, str):
        raise ValueError("root_path must be a string")
    return {"root_path": root_path}


def parse_scanner_shadow_payload(payload: Any) -> ScannerShadowJobPayload:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    payload_keys = set(payload.keys())
    if payload_keys != {"root_path"}:
        raise ValueError("payload keys must match ScannerShadowJobPayload contract")
    root_path = payload.get("root_path")
    if not isinstance(root_path, str):
        raise ValueError("root_path must be a string")
    return {"root_path": root_path}
