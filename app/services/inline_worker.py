import os
import socket
import threading

from .worker import worker_loop

_inline_worker_thread = None


def start_inline_worker_if_enabled() -> None:
    global _inline_worker_thread
    enabled = os.getenv("IMGVIEWER_INLINE_WORKER", "1").strip().lower() not in {"0", "false", "no"}
    if not enabled:
        return
    if _inline_worker_thread is not None and _inline_worker_thread.is_alive():
        return
    worker_id = f"inline-{socket.gethostname()}-{threading.get_ident()}"
    _inline_worker_thread = threading.Thread(
        target=worker_loop,
        kwargs={"poll_interval": 1.0, "once": False, "worker_id": worker_id},
        daemon=True,
        name="imgviewer-inline-worker",
    )
    _inline_worker_thread.start()
