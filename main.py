import threading

from app.api.app import app
from app.services.app_state import require_root, set_root
from app.services.scanner import run_rescan_job


def build_index_sync():
    root = require_root()
    return run_rescan_job(root=root, job_id=None)


if __name__ == "__main__":
    import sys

    import uvicorn

    folder = sys.argv[1] if len(sys.argv) > 1 else None
    if folder:
        try:
            set_root(folder)
            print(f"[tagimage] Root: {folder}")
            print("[tagimage] Starting background scan")
            threading.Thread(target=build_index_sync, daemon=True).start()
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        except Exception as exc:
            print(f"Database error: {exc}")
            sys.exit(1)

    print("[tagimage] Open http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
