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


COMPARE_FIELDS = ("image_id", "path", "ext", "source_bytes", "mtime", "width", "height")


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
    try:
        current = Path(sys.executable).absolute()
        target = venv_python.absolute()
    except Exception:
        return
    if current == target:
        return
    os.execv(str(target), [str(target), *sys.argv])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Python vs Rust metadata shadow results")
    parser.add_argument("--limit", type=int, default=20, help="number of images to compare")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for Rust metadata results")
    parser.add_argument("--poll-interval", type=float, default=0.25, help="seconds between DB polls")
    parser.add_argument(
        "--no-start-worker",
        action="store_true",
        help="do not start a local metadata-worker; wait for an already running worker",
    )
    parser.add_argument(
        "--skip-build-worker",
        action="store_true",
        help="do not run cargo build before starting metadata-worker",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    return parser.parse_args()


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 1000))


def select_images(limit: int) -> list[dict[str, Any]]:
    from app.repo.db import db_connect, dict_row, ensure_db_ready

    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, root_path, path, mtime
                FROM images
                WHERE hidden = false
                ORDER BY root_path, lower(path), path, id
                LIMIT %s
                """,
                (clamp_limit(limit),),
            )
            return list(cur.fetchall())


def python_reference_metadata(row: dict[str, Any]) -> dict[str, Any]:
    from app.services.scanner import build_image_record_from_path

    root = Path(row["root_path"])
    source = root / row["path"]
    record = build_image_record_from_path(root, source)
    ext = source.suffix.lower().lstrip(".") or "unknown"
    return {
        "image_id": row["id"],
        "root_path": row["root_path"],
        "path": row["path"],
        "ext": ext,
        "source_bytes": int(record["size"]),
        "mtime": int(record["mtime"]),
        "width": int(record["width"]),
        "height": int(record["height"]),
    }


def enqueue_metadata_jobs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from app.services.metadata_jobs_service import enqueue_metadata_job_for_image

    jobs_by_image_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        mtime_value = row.get("mtime")
        mtime = int(mtime_value) if mtime_value is not None else None
        job = enqueue_metadata_job_for_image(
            image_id=row["id"],
            root_path=row["root_path"],
            path=row["path"],
            mtime=mtime,
            priority=100,
            max_attempts=1,
        )
        jobs_by_image_id[row["id"]] = dict(job)
    return jobs_by_image_id


def build_metadata_worker(repo_root: Path) -> None:
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            str(repo_root / "rust" / "metadata-worker" / "Cargo.toml"),
        ],
        cwd=repo_root,
        check=True,
    )


def metadata_worker_binary(repo_root: Path) -> Path:
    return repo_root / "rust" / "thumb-worker" / "target" / "release" / "imgviewer-metadata-worker"


def start_metadata_worker(repo_root: Path) -> tuple[subprocess.Popen[Any], Any]:
    log_dir = repo_root / ".logs"
    log_dir.mkdir(exist_ok=True)
    log_file = (log_dir / "metadata-parity-worker.log").open("w", encoding="utf-8")
    binary = metadata_worker_binary(repo_root)
    if binary.is_file() and os.access(binary, os.X_OK):
        cmd = [str(binary)]
    else:
        cmd = [
            "cargo",
            "run",
            "--release",
            "--manifest-path",
            str(repo_root / "rust" / "metadata-worker" / "Cargo.toml"),
        ]

    env = os.environ.copy()
    env.setdefault("IMGVIEWER_METADATA_POLL_MS", "100")
    env.setdefault("IMGVIEWER_METADATA_METRICS_INTERVAL_SEC", "0")
    process = subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log_file


def stop_metadata_worker(process: subprocess.Popen[Any] | None, log_file: Any | None) -> None:
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


def fetch_metadata_results(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not job_ids:
        return {}

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
                      AND data ? 'metadata'
                    ORDER BY id DESC
                    LIMIT 1
                ) e ON true
                WHERE j.id = ANY(%s)
                """,
                (job_ids,),
            )
            return {row["id"]: dict(row) for row in cur.fetchall()}


def wait_for_metadata_results(job_ids: list[str], *, timeout: float, poll_interval: float) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.1, timeout)
    interval = max(0.05, poll_interval)
    latest: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        latest = fetch_metadata_results(job_ids)
        if all(latest.get(job_id, {}).get("succeeded_event_data") for job_id in job_ids):
            return latest
        time.sleep(interval)
    return fetch_metadata_results(job_ids)


def extract_rust_metadata(result_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result_row:
        return None
    data = result_row.get("succeeded_event_data")
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata


def compare_metadata_records(reference: dict[str, Any], rust: dict[str, Any] | None) -> list[dict[str, Any]]:
    if rust is None:
        return [{"field": "metadata", "python": reference, "rust": None}]

    mismatches: list[dict[str, Any]] = []
    for field in COMPARE_FIELDS:
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


def build_report(
    rows: list[dict[str, Any]],
    references: dict[str, dict[str, Any]],
    jobs_by_image_id: dict[str, dict[str, Any]],
    results_by_job_id: dict[str, dict[str, Any]],
    *,
    reference_errors: dict[str, str] | None = None,
    max_examples: int = 5,
) -> dict[str, Any]:
    compared = 0
    matched = 0
    mismatched = 0
    missing_results = 0
    examples: list[dict[str, Any]] = []
    reference_errors = reference_errors or {}

    for row in rows:
        image_id = row["id"]
        reference = references.get(image_id)
        job = jobs_by_image_id.get(image_id)
        job_id = job.get("id") if job else None
        result_row = results_by_job_id.get(job_id) if job_id else None
        rust = extract_rust_metadata(result_row)

        if reference is None or rust is None:
            missing_results += 1
            if len(examples) < max_examples:
                examples.append(
                    {
                        "image_id": image_id,
                        "path": row["path"],
                        "job_id": job_id,
                        "state": result_row.get("state") if result_row else None,
                        "error": result_row.get("error") if result_row else None,
                        "reason": "missing_python_reference" if reference is None else "missing_rust_metadata_event",
                        "python_error": reference_errors.get(image_id),
                    }
                )
            continue

        compared += 1
        mismatches = compare_metadata_records(reference, rust)
        if mismatches:
            mismatched += 1
            if len(examples) < max_examples:
                examples.append(
                    {
                        "image_id": image_id,
                        "path": row["path"],
                        "job_id": job_id,
                        "mismatches": mismatches,
                    }
                )
        else:
            matched += 1

    return {
        "selected": len(rows),
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

    print(f"selected={report['selected']}")
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

    rows = select_images(args.limit)
    if not rows:
        print("No non-hidden images found in images table.", file=sys.stderr)
        return 2

    references: dict[str, dict[str, Any]] = {}
    reference_errors: dict[str, str] = {}
    for row in rows:
        try:
            references[row["id"]] = python_reference_metadata(row)
        except Exception as exc:
            reference_errors[row["id"]] = str(exc)

    jobs_by_image_id = enqueue_metadata_jobs(rows)
    job_ids = [job["id"] for job in jobs_by_image_id.values() if job.get("id")]

    worker: subprocess.Popen[Any] | None = None
    worker_log = None
    try:
        if not args.no_start_worker:
            if not args.skip_build_worker:
                build_metadata_worker(repo_root)
            worker, worker_log = start_metadata_worker(repo_root)
        results_by_job_id = wait_for_metadata_results(
            job_ids,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
    finally:
        stop_metadata_worker(worker, worker_log)

    report = build_report(
        rows,
        references,
        jobs_by_image_id,
        results_by_job_id,
        reference_errors=reference_errors,
    )
    print_report(report, as_json=args.json)
    return 0 if report["mismatched"] == 0 and report["missing_results"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
