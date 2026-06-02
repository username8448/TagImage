from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "compare-api-parity.py"
    spec = spec_from_file_location("compare_api_parity_for_tests", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {script_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api_parity = _load_script_module()


def test_normalize_images_response_replaces_cursor_and_elapsed():
    normalized = api_parity.normalize_json(
        "/api/images?limit=1",
        {
            "elapsed_ms": 12.34,
            "items": [],
            "page": {"next_cursor": "abc", "total": 0},
        },
    )

    assert "elapsed_ms" not in normalized
    assert normalized["page"]["next_cursor"] == "<cursor>"


def test_normalize_jobs_response_replaces_datetime_fields():
    normalized = api_parity.normalize_json(
        "/api/jobs/job-1",
        {
            "job": {
                "id": "job-1",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": None,
            }
        },
    )

    assert normalized["job"]["created_at"] == "<datetime>"
    assert normalized["job"]["updated_at"] is None


def test_normalize_enqueue_mismatch_ignores_job_ids():
    mismatch = {
        "endpoint": "/api/rescan",
        "python": {"status": 200, "body": {"ok": True, "job_id": "py", "job_ids": ["py"]}},
        "rust": {"status": 200, "body": {"ok": True, "job_id": "rs", "job_ids": ["rs"]}},
    }

    assert api_parity.normalize_enqueue_mismatch(mismatch) is None
