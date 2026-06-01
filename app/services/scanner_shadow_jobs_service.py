from typing import Any, Optional

from ..config import JOB_TYPE_SCANNER_SHADOW
from ..contracts.scanner_shadow import build_scanner_shadow_payload
from ..repo.db import enqueue_job


def enqueue_scanner_shadow_job(
    root_path: str,
    *,
    priority: int = 0,
    max_attempts: Optional[int] = None,
) -> dict[str, Any]:
    payload = build_scanner_shadow_payload(root_path)
    attempts = 1 if max_attempts is None else max_attempts
    return enqueue_job(
        JOB_TYPE_SCANNER_SHADOW,
        payload,
        priority=priority,
        max_attempts=attempts,
    )
