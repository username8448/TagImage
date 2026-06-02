from app.services.worker_capabilities import get_worker_capabilities


def test_rescan_capabilities_default_to_python(monkeypatch):
    monkeypatch.delenv("IMGVIEWER_RUST_SCANNER", raising=False)

    capabilities = get_worker_capabilities()

    assert capabilities["rescan"]["mode"] == "python"
    assert capabilities["rescan"]["rust_supported"] is True


def test_rescan_capabilities_can_report_rust_scanner(monkeypatch):
    monkeypatch.setenv("IMGVIEWER_RUST_SCANNER", "1")
    monkeypatch.setenv("IMGVIEWER_INLINE_WORKER", "0")

    capabilities = get_worker_capabilities()

    assert capabilities["rescan"]["mode"] == "rust"
    assert capabilities["rescan"]["rust_supported"] is True
    assert capabilities["rescan"]["inline_worker"] is False


def test_metadata_capabilities_report_optional_shadow_worker(monkeypatch):
    monkeypatch.setenv("IMGVIEWER_METADATA_WORKER", "1")

    capabilities = get_worker_capabilities()

    assert capabilities["metadata"]["mode"] == "shadow"
    assert capabilities["metadata"]["rust_supported"] is True
    assert capabilities["metadata"]["authoritative"] is False
