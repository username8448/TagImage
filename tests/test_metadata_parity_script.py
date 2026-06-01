from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "compare-metadata-parity.py"
    spec = spec_from_file_location("compare_metadata_parity_for_tests", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {script_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metadata_parity = _load_script_module()


def test_compare_metadata_records_match():
    reference = {
        "image_id": "img-1",
        "path": "a/b.jpg",
        "ext": "jpg",
        "source_bytes": 42,
        "mtime": 123,
        "width": 10,
        "height": 20,
    }

    assert metadata_parity.compare_metadata_records(reference, dict(reference)) == []


def test_compare_metadata_records_reports_field_mismatches():
    reference = {
        "image_id": "img-1",
        "path": "a/b.jpg",
        "ext": "jpg",
        "source_bytes": 42,
        "mtime": 123,
        "width": 10,
        "height": 20,
    }
    rust = dict(reference)
    rust["width"] = 11

    mismatches = metadata_parity.compare_metadata_records(reference, rust)

    assert mismatches == [{"field": "width", "python": 10, "rust": 11}]


def test_build_report_counts_missing_and_mismatch_examples():
    rows = [
        {"id": "img-1", "path": "ok.jpg"},
        {"id": "img-2", "path": "bad.jpg"},
        {"id": "img-3", "path": "missing.jpg"},
    ]
    references = {
        "img-1": {
            "image_id": "img-1",
            "path": "ok.jpg",
            "ext": "jpg",
            "source_bytes": 1,
            "mtime": 2,
            "width": 3,
            "height": 4,
        },
        "img-2": {
            "image_id": "img-2",
            "path": "bad.jpg",
            "ext": "jpg",
            "source_bytes": 1,
            "mtime": 2,
            "width": 3,
            "height": 4,
        },
    }
    jobs = {
        "img-1": {"id": "job-1"},
        "img-2": {"id": "job-2"},
        "img-3": {"id": "job-3"},
    }
    results = {
        "job-1": {"succeeded_event_data": {"metadata": dict(references["img-1"])}},
        "job-2": {
            "succeeded_event_data": {
                "metadata": {
                    **references["img-2"],
                    "height": 5,
                }
            }
        },
    }

    report = metadata_parity.build_report(rows, references, jobs, results)

    assert report["selected"] == 3
    assert report["compared"] == 2
    assert report["matched"] == 1
    assert report["mismatched"] == 1
    assert report["missing_results"] == 1
    assert len(report["examples"]) == 2
