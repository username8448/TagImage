#!/usr/bin/env python3
"""
ImgViewer launcher.

Usage:
    python run.py                    # open browser, enter path manually
    python run.py /path/to/photos   # start with folder pre-loaded
"""
import sys
import threading
import webbrowser
from pathlib import Path

# ── Try to import app pieces ──────────────────────────────────────────────────
try:
    import main as app_module
except ImportError as e:
    print(f"Import error: {e}\nRun: pip install -r requirements.txt")
    sys.exit(1)

try:
    import uvicorn
except ImportError:
    print("uvicorn not found. Run: pip install -r requirements.txt")
    sys.exit(1)


def _launch_browser(url: str):
    import time
    time.sleep(1.2)
    webbrowser.open(url)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ImgViewer")
    parser.add_argument("folder", nargs="?", default=None, help="Path to image folder")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    folder = args.folder
    port   = args.port

    if folder:
        folder = str(Path(folder).resolve())
        try:
            app_module.set_root(folder)
            print(f"[imgviewer] Root folder : {folder}")
            threading.Thread(target=app_module.build_index_sync, daemon=True).start()
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Database error: {e}")
            print("Set DATABASE_URL or start the bundled PostgreSQL with: docker compose up -d postgres")
            sys.exit(1)

    url = f"http://localhost:{port}"
    print(f"[imgviewer] Starting server at {url}")
    threading.Thread(target=_launch_browser, args=(url,), daemon=True).start()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
