from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_module(module_name: str, relative_path: str):
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / relative_path
    spec = spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {module_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


thumbs_module = _load_module("contracts_thumbs_for_tests", "app/contracts/thumbs.py")
rescan_module = _load_module("contracts_rescan_for_tests", "app/contracts/rescan.py")
scanner_shadow_module = _load_module("contracts_scanner_shadow_for_tests", "app/contracts/scanner_shadow.py")

build_thumb_payload = thumbs_module.build_thumb_payload
parse_thumb_payload = thumbs_module.parse_thumb_payload
thumb_dedupe_key = thumbs_module.thumb_dedupe_key

build_rescan_payload = rescan_module.build_rescan_payload
parse_rescan_payload = rescan_module.parse_rescan_payload
build_scanner_shadow_payload = scanner_shadow_module.build_scanner_shadow_payload
parse_scanner_shadow_payload = scanner_shadow_module.parse_scanner_shadow_payload


def test_build_thumb_payload_has_required_keys():
    payload = build_thumb_payload(
        image_id="img-1",
        root_path="/data/root",
        path="folder/file.jpg",
        thumb=".imgindex/thumbs/img-1.jpg",
        mtime=123,
        max_size=[640, 640],
    )
    assert set(payload.keys()) == {"image_id", "root_path", "path", "thumb", "mtime", "max_size"}


def test_build_thumb_payload_keeps_max_size_as_list_of_two_numbers():
    payload = build_thumb_payload(
        image_id="img-2",
        root_path="/data/root",
        path="folder/file-2.jpg",
        thumb=".imgindex/thumbs/img-2.jpg",
        mtime=456,
        max_size=(640, 640),
    )
    assert isinstance(payload["max_size"], list)
    assert len(payload["max_size"]) == 2
    assert all(isinstance(value, int) for value in payload["max_size"])


def test_thumb_dedupe_key_format():
    assert thumb_dedupe_key("abc", 123) == "thumb:abc:123"


def test_parse_thumb_payload_round_trip():
    original = build_thumb_payload(
        image_id="img-3",
        root_path="/mnt/photos",
        path="cats/sleep.jpg",
        thumb=".imgindex/thumbs/img-3.jpg",
        mtime=789,
        max_size=[640, 640],
    )
    parsed = parse_thumb_payload(original)
    assert parsed == original


def test_build_rescan_payload_has_root_path():
    payload = build_rescan_payload("/photos/root")
    assert payload == {"root_path": "/photos/root"}


def test_parse_rescan_payload_keeps_root_path():
    original = build_rescan_payload("/photos/root-2")
    parsed = parse_rescan_payload(original)
    assert parsed == original


def test_build_scanner_shadow_payload_has_root_path():
    payload = build_scanner_shadow_payload("/photos/root")
    assert payload == {"root_path": "/photos/root"}


def test_parse_scanner_shadow_payload_keeps_root_path():
    original = build_scanner_shadow_payload("/photos/root-2")
    parsed = parse_scanner_shadow_payload(original)
    assert parsed == original
