#!/usr/bin/env python3
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


IMAGE_COMPARE_FIELDS = ("path", "ext", "source_bytes", "mtime", "width", "height")
MAX_DIFF_ITEMS = 10


def detect_repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / "app").is_dir() and (candidate / "scripts").is_dir():
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
    parser = argparse.ArgumentParser(description="Compare Python scanner reference vs Rust scanner shadow result")
    parser.add_argument("--root", default=None, help="folder to scan; defaults to saved active session root")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for Rust scanner result")
    parser.add_argument("--poll-interval", type=float, default=0.25, help="seconds between DB polls")
    parser.add_argument(
        "--no-start-worker",
        action="store_true",
        help="do not start a local scanner-worker; wait for an already running worker",
    )
    parser.add_argument(
        "--skip-build-worker",
        action="store_true",
        help="do not run cargo build before starting scanner-worker",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    return parser.parse_args()


def resolve_root(explicit_root: str | None) -> str:
    if explicit_root:
        return str(Path(explicit_root).expanduser().resolve())

    from app.repo.content import load_session

    session = load_session()
    root = session.get("root_path")
    if not root:
        roots = [item for item in session.get("root_paths") or [] if item]
        root = roots[0] if roots else None
    if not root:
        raise SystemExit("No --root provided and no saved session root found")
    return str(Path(root).expanduser().resolve())


def sort_rel_paths(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda value: (value.lower(), value))


def load_existing_images(root_path: str) -> dict[str, dict[str, Any]]:
    from app.repo.db import db_connect, dict_row, ensure_db_ready

    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, path, hidden
                FROM images
                WHERE root_path = %s
                """,
                (root_path,),
            )
            return {row["path"]: dict(row) for row in cur.fetchall()}


def python_reference_scan(root_path: str) -> dict[str, Any]:
    from app.services.scanner import build_image_record_from_path, scan_image_paths, should_regenerate_thumb

    root = Path(root_path)
    existing_by_path = load_existing_images(root_path)
    image_paths = scan_image_paths(root)
    scanned_paths: set[str] = set()
    paths: list[str] = []
    images: list[dict[str, Any]] = []
    thumb_job_candidates: list[str] = []

    for image_path in image_paths:
        record = build_image_record_from_path(root, image_path)
        rel = record["rel"]
        existing = existing_by_path.get(rel)
        existing_id = existing["id"] if existing else None
        thumb_rel = f".imgindex/thumbs/{existing_id}.jpg" if existing_id else None
        thumb_job_candidate = True
        if thumb_rel:
            thumb_job_candidate = should_regenerate_thumb(record["stat"], root / thumb_rel)

        if thumb_job_candidate:
            thumb_job_candidates.append(rel)
        scanned_paths.add(rel)
        paths.append(rel)
        images.append(
            {
                "path": rel,
                "ext": image_path.suffix.lower().lstrip(".") or "unknown",
                "source_bytes": int(record["size"]),
                "mtime": int(record["mtime"]),
                "width": int(record["width"]),
                "height": int(record["height"]),
                "existing_id": existing_id,
                "is_new": existing is None,
                "thumb": thumb_rel,
                "thumb_job_candidate": thumb_job_candidate,
            }
        )

    missing_candidates = [
        row["path"]
        for row in existing_by_path.values()
        if row["path"] not in scanned_paths
    ]
    hidden_candidates = [
        row["path"]
        for row in existing_by_path.values()
        if row["path"] not in scanned_paths and not bool(row["hidden"])
    ]

    return {
        "root_path": root_path,
        "total": len(images),
        "paths": paths,
        "images": images,
        "missing_candidates": sort_rel_paths(missing_candidates),
        "hidden_candidates": sort_rel_paths(hidden_candidates),
        "thumb_job_candidates": sort_rel_paths(thumb_job_candidates),
        "shadow": True,
    }


def enqueue_scanner_shadow_job(root_path: str) -> dict[str, Any]:
    from app.services.scanner_shadow_jobs_service import enqueue_scanner_shadow_job

    return enqueue_scanner_shadow_job(root_path, priority=100, max_attempts=1)


def build_scanner_worker(repo_root: Path) -> None:
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            str(repo_root / "rust" / "scanner-worker" / "Cargo.toml"),
        ],
        cwd=repo_root,
        check=True,
    )


def scanner_worker_binary(repo_root: Path) -> Path:
    return repo_root / "rust" / "thumb-worker" / "target" / "release" / "imgviewer-scanner-worker"


def start_scanner_worker(repo_root: Path) -> tuple[subprocess.Popen[Any], Any]:
    log_dir = repo_root / ".logs"
    log_dir.mkdir(exist_ok=True)
    log_file = (log_dir / "scanner-parity-worker.log").open("w", encoding="utf-8")
    binary = scanner_worker_binary(repo_root)
    if binary.is_file() and os.access(binary, os.X_OK):
        cmd = [str(binary)]
    else:
        cmd = [
            "cargo",
            "run",
            "--release",
            "--manifest-path",
            str(repo_root / "rust" / "scanner-worker" / "Cargo.toml"),
        ]

    env = os.environ.copy()
    env.setdefault("IMGVIEWER_SCANNER_POLL_MS", "100")
    process = subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log_file


def stop_scanner_worker(process: subprocess.Popen[Any] | None, log_file: Any | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    if log_file is not None:
        log_file.close()


def fetch_scanner_result(job_id: str) -> dict[str, Any] | None:
    from app.repo.db import db_connect, dict_row

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    j.id,
                    j.state,
                    j.error,
                    e.data AS succeeded_event_data
                FROM jobs j
                LEFT JOIN LATERAL (
                    SELECT data
                    FROM job_events
                    WHERE job_id = j.id
                      AND event = 'succeeded'
                      AND data ? 'scanner_shadow'
                    ORDER BY id DESC
                    LIMIT 1
                ) e ON true
                WHERE j.id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def wait_for_scanner_result(job_id: str, *, timeout: float, poll_interval: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.1, timeout)
    interval = max(0.05, poll_interval)
    latest = None
    while time.monotonic() < deadline:
        latest = fetch_scanner_result(job_id)
        if latest and latest.get("succeeded_event_data"):
            return latest
        time.sleep(interval)
    return fetch_scanner_result(job_id) or latest


def extract_rust_scan(result_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result_row:
        return None
    data = result_row.get("succeeded_event_data")
    if not isinstance(data, dict):
        return None
    scan = data.get("scanner_shadow")
    return scan if isinstance(scan, dict) else None


def compare_image_records(reference: dict[str, Any], rust: dict[str, Any] | None) -> list[dict[str, Any]]:
    if rust is None:
        return [{"field": "image", "python": reference, "rust": None}]

    mismatches: list[dict[str, Any]] = []
    for field in IMAGE_COMPARE_FIELDS:
        expected = reference.get(field)
        actual = rust.get(field)
        if field in {"source_bytes", "mtime", "width", "height"} and actual is not None:
            try:
                actual = int(actual)
            except Exception:
                pass
        if actual != expected:
            mismatches.append({"field": field, "python": expected, "rust": actual})
    return mismatches


def summarize_global_mismatch(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    if isinstance(expected, list) and isinstance(actual, list):
        expected_items = [str(item) for item in expected]
        actual_items = [str(item) for item in actual]
        missing_in_rust = sort_rel_paths(list(set(expected_items) - set(actual_items)))
        extra_in_rust = sort_rel_paths(list(set(actual_items) - set(expected_items)))
        return {
            "field": field,
            "python_count": len(expected_items),
            "rust_count": len(actual_items),
            "missing_in_rust": missing_in_rust[:MAX_DIFF_ITEMS],
            "extra_in_rust": extra_in_rust[:MAX_DIFF_ITEMS],
        }

    return {"field": field, "python": expected, "rust": actual}


def compare_scans(reference: dict[str, Any], rust: dict[str, Any] | None, *, max_examples: int = 5) -> dict[str, Any]:
    if rust is None:
        return {
            "root_path": reference.get("root_path"),
            "compared": 0,
            "matched": 0,
            "mismatched": 0,
            "missing_results": 1,
            "examples": [{"reason": "missing_rust_scanner_shadow_result"}],
        }

    examples: list[dict[str, Any]] = []
    global_mismatches = 0
    for field in ("root_path", "total", "paths", "missing_candidates", "hidden_candidates", "thumb_job_candidates"):
        expected = reference.get(field)
        actual = rust.get(field)
        if field == "total" and actual is not None:
            try:
                actual = int(actual)
            except Exception:
                pass
        if actual != expected:
            global_mismatches += 1
            if len(examples) < max_examples:
                examples.append(summarize_global_mismatch(field, expected, actual))

    rust_by_path = {
        str(item.get("path")): item
        for item in rust.get("images") or []
        if isinstance(item, dict) and item.get("path") is not None
    }

    compared = 0
    matched = 0
    mismatched = global_mismatches
    missing_results = 0

    for image in reference.get("images") or []:
        path = image["path"]
        rust_image = rust_by_path.get(path)
        if rust_image is None:
            missing_results += 1
            if len(examples) < max_examples:
                examples.append({"path": path, "reason": "missing_rust_image_record"})
            continue
        compared += 1
        image_mismatches = compare_image_records(image, rust_image)
        if image_mismatches:
            mismatched += 1
            if len(examples) < max_examples:
                examples.append({"path": path, "mismatches": image_mismatches})
        else:
            matched += 1

    return {
        "root_path": reference.get("root_path"),
        "compared": compared,
        "matched": matched,
        "mismatched": mismatched,
        "missing_results": missing_results,
        "examples": examples,
    }


def print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    print(f"root_path={report['root_path']}")
    print(f"compared={report['compared']}")
    print(f"matched={report['matched']}")
    print(f"mismatched={report['mismatched']}")
    print(f"missing_results={report['missing_results']}")
    if report["examples"]:
        print("examples:")
        for example in report["examples"]:
            print(json.dumps(example, ensure_ascii=False, sort_keys=True))


def main() -> int:
    args = parse_args()
    repo_root = bootstrap_sys_path()
    maybe_reexec_venv(repo_root)

    from tagimage_env import ensure_database_url

    ensure_database_url()
    root_path = resolve_root(args.root)
    if not Path(root_path).is_dir():
        raise SystemExit(f"Root folder does not exist: {root_path}")

    reference = python_reference_scan(root_path)
    job = enqueue_scanner_shadow_job(root_path)
    job_id = job.get("id")
    if not job_id:
        raise SystemExit("Failed to enqueue scanner shadow job")

    worker: subprocess.Popen[Any] | None = None
    worker_log = None
    try:
        if not args.no_start_worker:
            if not args.skip_build_worker:
                build_scanner_worker(repo_root)
            worker, worker_log = start_scanner_worker(repo_root)
        result_row = wait_for_scanner_result(
            job_id,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
    finally:
        stop_scanner_worker(worker, worker_log)

    report = compare_scans(reference, extract_rust_scan(result_row))
    print_report(report, as_json=args.json)
    return 0 if report["mismatched"] == 0 and report["missing_results"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
