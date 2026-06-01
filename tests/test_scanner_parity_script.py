from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "compare-scanner-parity.py"
    spec = spec_from_file_location("compare_scanner_parity_for_tests", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {script_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scanner_parity = _load_script_module()


def test_compare_image_records_match():
    reference = {
        "path": "a/b.jpg",
        "ext": "jpg",
        "source_bytes": 42,
        "mtime": 123,
        "width": 10,
        "height": 20,
    }

    assert scanner_parity.compare_image_records(reference, dict(reference)) == []


def test_compare_image_records_reports_field_mismatch():
    reference = {
        "path": "a/b.jpg",
        "ext": "jpg",
        "source_bytes": 42,
        "mtime": 123,
        "width": 10,
        "height": 20,
    }
    rust = dict(reference)
    rust["height"] = 21

    mismatches = scanner_parity.compare_image_records(reference, rust)

    assert mismatches == [{"field": "height", "python": 20, "rust": 21}]


def test_compare_scans_counts_global_and_image_mismatches():
    reference = {
        "root_path": "/photos",
        "total": 2,
        "paths": ["a.jpg", "b.jpg"],
        "missing_candidates": [],
        "hidden_candidates": [],
        "thumb_job_candidates": ["b.jpg"],
        "images": [
            {
                "path": "a.jpg",
                "ext": "jpg",
                "source_bytes": 1,
                "mtime": 2,
                "width": 3,
                "height": 4,
            },
            {
                "path": "b.jpg",
                "ext": "jpg",
                "source_bytes": 5,
                "mtime": 6,
                "width": 7,
                "height": 8,
            },
        ],
    }
    rust = {
        **reference,
        "thumb_job_candidates": [],
        "images": [
            dict(reference["images"][0]),
            {
                **reference["images"][1],
                "width": 9,
            },
        ],
    }

    report = scanner_parity.compare_scans(reference, rust)

    assert report["compared"] == 2
    assert report["matched"] == 1
    assert report["mismatched"] == 2
    assert report["missing_results"] == 0
    assert len(report["examples"]) == 2
