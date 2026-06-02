#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


VOLATILE_JOB_FIELDS = {
    "scheduled_at",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
}


def detect_repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / "app").is_dir() and (candidate / "rust").is_dir() and (candidate / "scripts").is_dir():
            return candidate
    raise RuntimeError("Could not detect repo root from script location")


def bootstrap_sys_path() -> Path:
    repo_root = detect_repo_root()
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return repo_root


def maybe_reexec_venv(repo_root: Path) -> None:
    venv_python = repo_root / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return
    current = Path(sys.executable).absolute()
    target = venv_python.absolute()
    if current == target:
        return
    os.execv(str(target), [str(target), *sys.argv])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Python FastAPI responses with Rust API shadow server")
    parser.add_argument("--rust-port", type=int, default=8010)
    parser.add_argument("--rust-host", default="127.0.0.1")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--no-start-rust", action="store_true", help="use an already running Rust API server")
    parser.add_argument("--skip-build-rust", action="store_true", help="do not build rust/api-server before starting it")
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    return parser.parse_args()


class EndpointResult:
    def __init__(
        self,
        *,
        status: int,
        body: Any,
        content_type: str | None = None,
        body_len: int | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.body_len = body_len


def request_rust(base_url: str, method: str, path: str, payload: Any | None = None) -> EndpointResult:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            content_type = response.headers.get("content-type")
            return EndpointResult(
                status=response.status,
                body=decode_body(raw, content_type),
                content_type=content_type,
                body_len=len(raw),
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        content_type = exc.headers.get("content-type")
        return EndpointResult(
            status=exc.code,
            body=decode_body(raw, content_type),
            content_type=content_type,
            body_len=len(raw),
        )


def request_python(client, method: str, path: str, payload: Any | None = None) -> EndpointResult:
    response = client.request(method, path, json=payload)
    content_type = response.headers.get("content-type")
    return EndpointResult(
        status=response.status_code,
        body=decode_body(response.content, content_type),
        content_type=content_type,
        body_len=len(response.content),
    )


def decode_body(raw: bytes, content_type: str | None) -> Any:
    if content_type and "application/json" in content_type:
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    return raw


def normalize_response(path: str, result: EndpointResult) -> dict[str, Any]:
    body = normalize_json(path, result.body)
    content_type = result.content_type.split(";")[0] if result.content_type else None
    normalized = {"status": result.status, "body": body}
    if isinstance(result.body, bytes):
        normalized["content_type"] = content_type
        normalized["body_len"] = result.body_len
    return normalized


def normalize_json(path: str, value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_json(path, item) for item in value]
    if not isinstance(value, dict):
        return value

    out = {key: normalize_json(path, item) for key, item in value.items()}
    out.pop("elapsed_ms", None)
    out.pop("db_lookup_ms", None)
    if path.startswith("/api/images"):
        page = out.get("page")
        if isinstance(page, dict):
            if page.get("next_cursor"):
                page["next_cursor"] = "<cursor>"
    if path.startswith("/api/jobs"):
        out = normalize_job(out)
    if path == "/api/status":
        # Status can legitimately point at the latest global rescan job from previous runtime checks.
        # Keep queue/progress fields, but normalize timestamp-bearing job payloads recursively.
        out = normalize_job(out)
    return out


def normalize_job(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_job(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: normalize_job(item) for key, item in value.items()}
    for field in VOLATILE_JOB_FIELDS:
        if field in out and out[field] is not None:
            out[field] = "<datetime>"
    return out


def compare_result(name: str, python: EndpointResult, rust: EndpointResult) -> dict[str, Any] | None:
    normalized_python = normalize_response(name, python)
    normalized_rust = normalize_response(name, rust)
    if normalized_python == normalized_rust:
        return None
    return {
        "endpoint": name,
        "python": normalized_python,
        "rust": normalized_rust,
    }


def wait_for_rust(base_url: str, timeout: float, poll_interval: float) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = request_rust(base_url, "GET", "/api/session")
            if result.status < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(poll_interval)
    raise RuntimeError(f"Rust API did not become ready: {last_error}")


def start_rust_api(repo_root: Path, args: argparse.Namespace) -> subprocess.Popen | None:
    if args.no_start_rust:
        return None
    if not args.skip_build_rust:
        subprocess.run(
            ["cargo", "build", "--manifest-path", str(repo_root / "rust" / "api-server" / "Cargo.toml")],
            cwd=repo_root,
            check=True,
        )
    binary = repo_root / "rust" / "thumb-worker" / "target" / "debug" / "imgviewer-api-server"
    command = [str(binary) if binary.exists() else "cargo"]
    if not binary.exists():
        command = [
            "cargo",
            "run",
            "--manifest-path",
            str(repo_root / "rust" / "api-server" / "Cargo.toml"),
            "--",
        ]
    command.extend(["--host", args.rust_host, "--port", str(args.rust_port)])
    env = os.environ.copy()
    env.setdefault("IMGVIEWER_INLINE_WORKER", "0")
    env.setdefault("IMGVIEWER_THUMB_JOB_MODE", "queue")
    env.setdefault("IMGVIEWER_THUMB_WAIT_MS", "50")
    env.setdefault("IMGVIEWER_THUMB_POLL_MS", "20")
    env.setdefault("IMGVIEWER_THUMB_SYNC_FALLBACK", "0")
    env.setdefault("IMGVIEWER_THUMB_WORKER_EXPECTED", "1")
    return subprocess.Popen(command, cwd=repo_root, env=env)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def write_tiny_jpeg(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), (80, 120, 180)).save(path, "JPEG", quality=85)


def snapshot_session() -> dict[str, Any]:
    from app.repo.content import load_session

    return load_session()


def restore_session(snapshot: dict[str, Any]) -> None:
    from app.repo.content import save_session_fields

    save_session_fields(
        root_path=snapshot.get("root_path"),
        root_paths=snapshot.get("root_paths") or [],
        search_tags=snapshot.get("search_tags") or [],
        search_mode=snapshot.get("search_mode") or "any",
        last_image_id=snapshot.get("last_image_id"),
        tabs=snapshot.get("tabs") or [],
        active_tab_id=snapshot.get("active_tab_id"),
    )


def prepare_fixture(root: Path, image_id: str) -> None:
    from app.repo.content import save_session_fields
    from app.repo.db import db_connect, ensure_db_ready

    ensure_db_ready()
    image_path = root / "sample.jpg"
    thumb_path = root / ".imgindex" / "thumbs" / f"{image_id}.jpg"
    write_tiny_jpeg(image_path)
    write_tiny_jpeg(thumb_path)
    stat = image_path.stat()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cleanup_fixture_rows(cur, str(root), [])
            cur.execute(
                """
                INSERT INTO images (id, root_path, path, thumb, size, mtime, width, height, hidden)
                VALUES (%s, %s, 'sample.jpg', %s, %s, %s, 16, 12, false)
                ON CONFLICT (root_path, path) DO UPDATE SET
                    id = EXCLUDED.id,
                    thumb = EXCLUDED.thumb,
                    size = EXCLUDED.size,
                    mtime = EXCLUDED.mtime,
                    width = EXCLUDED.width,
                    height = EXCLUDED.height,
                    hidden = false,
                    updated_at = now()
                """,
                (
                    image_id,
                    str(root),
                    f".imgindex/thumbs/{image_id}.jpg",
                    stat.st_size,
                    int(stat.st_mtime),
                ),
            )
    save_session_fields(root_path=str(root), root_paths=[str(root)], last_image_id=None)


def cleanup_fixture_rows(cur, root: str, tag_names: list[str]) -> None:
    cur.execute("DELETE FROM jobs WHERE payload->>'root_path' = %s", (root,))
    cur.execute("DELETE FROM image_tags WHERE image_id IN (SELECT id FROM images WHERE root_path = %s)", (root,))
    cur.execute("DELETE FROM images WHERE root_path = %s", (root,))
    for tag_name in tag_names:
        norm = " ".join(tag_name.strip().split()).lower()
        cur.execute("DELETE FROM suppressed_auto_tags WHERE normalized = %s", (norm,))
        cur.execute(
            """
            DELETE FROM tags t
            WHERE t.normalized = %s
              AND NOT EXISTS (
                  SELECT 1 FROM image_tags it WHERE it.tag_id = t.id
              )
            """,
            (norm,),
        )


def cleanup_fixture(root: Path, session: dict[str, Any], tag_names: list[str]) -> None:
    from app.repo.db import db_connect

    with db_connect() as conn:
        with conn.cursor() as cur:
            cleanup_fixture_rows(cur, str(root), tag_names)
    restore_session(session)
    shutil.rmtree(root, ignore_errors=True)


def run_comparison(repo_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("IMGVIEWER_INLINE_WORKER", "0")
    os.environ.setdefault("IMGVIEWER_THUMB_JOB_MODE", "queue")
    os.environ.setdefault("IMGVIEWER_THUMB_WAIT_MS", "50")
    os.environ.setdefault("IMGVIEWER_THUMB_POLL_MS", "20")
    os.environ.setdefault("IMGVIEWER_THUMB_SYNC_FALLBACK", "0")
    os.environ.setdefault("IMGVIEWER_THUMB_WORKER_EXPECTED", "1")

    from fastapi.testclient import TestClient
    from app.api.app import app

    base_url = f"http://{args.rust_host}:{args.rust_port}"
    process = start_rust_api(repo_root, args)
    session = snapshot_session()
    root = Path(tempfile.mkdtemp(prefix="tagimage-api-parity-")).resolve()
    image_id = f"api{int(time.time() * 1000):x}"[-12:]
    tag_name = f"api-parity-{image_id}"
    tag_names = [tag_name]

    compared = 0
    mismatches: list[dict[str, Any]] = []
    try:
        prepare_fixture(root, image_id)
        wait_for_rust(base_url, args.timeout, args.poll_interval)
        with TestClient(app) as python_client:
            read_endpoints = [
                ("GET", "/api/session", None),
                ("GET", "/api/status", None),
                ("GET", "/api/images?limit=5&sort=path_asc&include_total=1", None),
                ("GET", "/api/tags", None),
                ("GET", "/api/jobs?limit=5", None),
                ("GET", f"/file/{image_id}", None),
                ("GET", f"/thumb-file/{image_id}.jpg", None),
                ("GET", f"/thumb/{image_id}", None),
            ]
            for method, path, payload in read_endpoints:
                compared += 1
                mismatch = compare_result(
                    path,
                    request_python(python_client, method, path, payload),
                    request_rust(base_url, method, path, payload),
                )
                if mismatch:
                    mismatches.append(mismatch)

            write_endpoints = [
                ("POST", "/api/tags", {"name": tag_name}),
                ("PATCH", f"/api/tags/{tag_name}", {"color": "#AABBCC"}),
                ("POST", f"/api/tag/{image_id}", {"tags": [tag_name]}),
                ("PATCH", "/api/session", {"search_tags": [tag_name], "search_mode": "all"}),
                ("POST", "/api/thumbs/rebuild", {"stale_only": True, "limit": 1}),
                ("POST", "/api/rescan", None),
                ("POST", "/api/folder", {"path": str(root)}),
            ]
            for method, path, payload in write_endpoints:
                compared += 1
                python_result = request_python(python_client, method, path, payload)
                rust_result = request_rust(base_url, method, path, payload)
                mismatch = compare_result(path, python_result, rust_result)
                if mismatch:
                    if path in {"/api/rescan", "/api/folder"}:
                        mismatch = normalize_enqueue_mismatch(mismatch)
                    if mismatch:
                        mismatches.append(mismatch)
    finally:
        cleanup_fixture(root, session, tag_names)
        stop_process(process)

    return {
        "compared": compared,
        "matched": compared - len(mismatches),
        "mismatched": len(mismatches),
        "examples": mismatches[:10],
    }


def normalize_enqueue_mismatch(mismatch: dict[str, Any]) -> dict[str, Any] | None:
    python = normalize_enqueue_body(mismatch["python"])
    rust = normalize_enqueue_body(mismatch["rust"])
    if python == rust:
        return None
    return {**mismatch, "python": python, "rust": rust}


def normalize_enqueue_body(value: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(value))
    body = out.get("body")
    if isinstance(body, dict):
        if body.get("job_id"):
            body["job_id"] = "<job_id>"
        if isinstance(body.get("job_ids"), list):
            body["job_ids"] = ["<job_id>" for _ in body["job_ids"]]
    return out


def print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return
    print("API parity report")
    print(f"  compared:   {report['compared']}")
    print(f"  matched:    {report['matched']}")
    print(f"  mismatched: {report['mismatched']}")
    if report["examples"]:
        print("  examples:")
        for item in report["examples"]:
            print(json.dumps(item, indent=2, sort_keys=True, ensure_ascii=False))


def main() -> int:
    repo_root = bootstrap_sys_path()
    maybe_reexec_venv(repo_root)
    args = parse_args()
    report = run_comparison(repo_root, args)
    print_report(report, args.json)
    return 0 if report["mismatched"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
