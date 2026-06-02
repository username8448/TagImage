from app.services.worker_capabilities import get_worker_capabilities


def test_capabilities_default_to_rust_runtime(monkeypatch):
    monkeypatch.delenv("IMGVIEWER_LEGACY_PYTHON", raising=False)
    monkeypatch.delenv("IMGVIEWER_RUST_SCANNER", raising=False)
    monkeypatch.delenv("IMGVIEWER_METADATA_WORKER", raising=False)
    monkeypatch.delenv("IMGVIEWER_METADATA_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("IMGVIEWER_THUMB_JOB_MODE", raising=False)
    monkeypatch.delenv("IMGVIEWER_INLINE_WORKER", raising=False)

    capabilities = get_worker_capabilities()

    assert capabilities["thumb"]["mode"] == "queue"
    assert capabilities["thumb"]["rust_supported"] is True
    assert capabilities["thumb"]["python_fallback"] is False
    assert capabilities["rescan"]["mode"] == "rust"
    assert capabilities["rescan"]["rust_supported"] is True
    assert capabilities["rescan"]["inline_worker"] is False
    assert capabilities["metadata"]["mode"] == "authoritative"
    assert capabilities["metadata"]["rust_supported"] is True
    assert capabilities["metadata"]["authoritative"] is True


def test_capabilities_legacy_profile_defaults_to_python(monkeypatch):
    monkeypatch.setenv("IMGVIEWER_LEGACY_PYTHON", "1")
    monkeypatch.delenv("IMGVIEWER_RUST_SCANNER", raising=False)
    monkeypatch.delenv("IMGVIEWER_METADATA_WORKER", raising=False)
    monkeypatch.delenv("IMGVIEWER_THUMB_JOB_MODE", raising=False)
    monkeypatch.delenv("IMGVIEWER_INLINE_WORKER", raising=False)

    capabilities = get_worker_capabilities()

    assert capabilities["thumb"]["mode"] == "sync"
    assert capabilities["rescan"]["mode"] == "python"
    assert capabilities["rescan"]["inline_worker"] is True
    assert capabilities["metadata"]["mode"] == "not_enabled"
    assert capabilities["metadata"]["authoritative"] is False


def test_explicit_flags_can_override_legacy_profile(monkeypatch):
    monkeypatch.setenv("IMGVIEWER_LEGACY_PYTHON", "1")
    monkeypatch.setenv("IMGVIEWER_RUST_SCANNER", "1")
    monkeypatch.setenv("IMGVIEWER_METADATA_WORKER", "1")
    monkeypatch.setenv("IMGVIEWER_METADATA_AUTHORITATIVE", "1")
    monkeypatch.setenv("IMGVIEWER_THUMB_JOB_MODE", "queue")
    monkeypatch.setenv("IMGVIEWER_INLINE_WORKER", "0")

    capabilities = get_worker_capabilities()

    assert capabilities["thumb"]["mode"] == "queue"
    assert capabilities["rescan"]["mode"] == "rust"
    assert capabilities["rescan"]["inline_worker"] is False
    assert capabilities["metadata"]["mode"] == "authoritative"
    assert capabilities["metadata"]["authoritative"] is True


def test_rescan_capabilities_can_report_rust_scanner(monkeypatch):
    monkeypatch.delenv("IMGVIEWER_LEGACY_PYTHON", raising=False)
    monkeypatch.setenv("IMGVIEWER_RUST_SCANNER", "1")
    monkeypatch.setenv("IMGVIEWER_INLINE_WORKER", "0")

    capabilities = get_worker_capabilities()

    assert capabilities["rescan"]["mode"] == "rust"
    assert capabilities["rescan"]["rust_supported"] is True
    assert capabilities["rescan"]["inline_worker"] is False


def test_metadata_capabilities_report_optional_shadow_worker(monkeypatch):
    monkeypatch.delenv("IMGVIEWER_LEGACY_PYTHON", raising=False)
    monkeypatch.setenv("IMGVIEWER_METADATA_WORKER", "1")
    monkeypatch.setenv("IMGVIEWER_METADATA_AUTHORITATIVE", "0")

    capabilities = get_worker_capabilities()

    assert capabilities["metadata"]["mode"] == "shadow"
    assert capabilities["metadata"]["rust_supported"] is True
    assert capabilities["metadata"]["authoritative"] is False
