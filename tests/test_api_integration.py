import uuid
import time
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

@pytest.fixture(scope="session")
def db_available():
    os.environ.setdefault("IMGVIEWER_INLINE_WORKER", "0")
    os.environ.setdefault("IMGVIEWER_THUMB_JOB_MODE", "queue")
    os.environ.setdefault("IMGVIEWER_THUMB_WAIT_MS", "50")
    os.environ.setdefault("IMGVIEWER_THUMB_POLL_MS", "20")
    os.environ.setdefault("IMGVIEWER_THUMB_SYNC_FALLBACK", "0")
    os.environ.setdefault("IMGVIEWER_THUMB_WORKER_EXPECTED", "1")
    from app.repo.db import ensure_db_ready

    try:
        ensure_db_ready()
    except Exception as exc:
        pytest.skip(f"PostgreSQL is required for integration tests: {exc}")
    return True


@pytest.fixture()
def client(monkeypatch, db_available):
    monkeypatch.setenv("IMGVIEWER_INLINE_WORKER", "0")
    monkeypatch.setenv("IMGVIEWER_THUMB_JOB_MODE", "queue")
    monkeypatch.setenv("IMGVIEWER_THUMB_WAIT_MS", "50")
    monkeypatch.setenv("IMGVIEWER_THUMB_POLL_MS", "20")
    monkeypatch.setenv("IMGVIEWER_THUMB_SYNC_FALLBACK", "0")
    monkeypatch.setenv("IMGVIEWER_THUMB_WORKER_EXPECTED", "1")
    from app.api.app import app

    with TestClient(app) as c:
        yield c


def _write_tiny_jpeg(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (16, 16), (120, 80, 20))
    im.save(path, "JPEG", quality=80)


def _cleanup_root_data(root_path: str) -> None:
    from app.repo.db import db_connect

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE payload->>'root_path' = %s", (root_path,))
            cur.execute("DELETE FROM images WHERE root_path = %s", (root_path,))


def test_smoke_rescan_job_and_pagination_filters(client, tmp_path):
    from app.services.worker import worker_loop
    from app.services.scanner import make_thumb_sync

    root = tmp_path / f"pics-{uuid.uuid4().hex[:6]}"
    _write_tiny_jpeg(root / "cats" / "sleep.jpg")
    _write_tiny_jpeg(root / "cats" / "play.jpg")
    _write_tiny_jpeg(root / "dogs" / "walk.jpg")

    response = client.post("/api/folder", json={"path": str(root)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    job_id = body["job_id"]

    worker_loop(once=True, worker_id="pytest-worker", poll_interval=0.01)

    job_body = None
    for _ in range(30):
        job = client.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200
        job_body = job.json()["job"]
        if job_body["state"] == "succeeded":
            break
        time.sleep(0.1)
    assert job_body is not None
    assert job_body["state"] == "succeeded"

    page1 = client.get("/api/images", params={"limit": 1, "include_total": 1})
    assert page1.status_code == 200
    p1 = page1.json()
    assert len(p1["items"]) == 1
    assert p1["page"]["total"] == 3
    assert p1["page"]["has_more"] is True
    assert p1["page"]["next_cursor"]

    page2 = client.get("/api/images", params={"limit": 2, "cursor": p1["page"]["next_cursor"], "include_total": 1})
    assert page2.status_code == 200
    p2 = page2.json()
    assert len(p2["items"]) == 2

    cats_all = client.get("/api/images", params={"include_tags": "cats", "match_mode": "all", "include_total": 1})
    assert cats_all.status_code == 200
    assert cats_all.json()["page"]["total"] == 2

    exclude_cats = client.get("/api/images", params={"exclude_tags": "cats", "include_total": 1})
    assert exclude_cats.status_code == 200
    assert exclude_cats.json()["page"]["total"] == 1

    thumb_enqueue = client.post("/api/thumbs/rebuild", json={"stale_only": False})
    assert thumb_enqueue.status_code == 200
    te = thumb_enqueue.json()
    assert te["ok"] is True
    assert te["enqueued"] >= 1
    assert "queued_existing" in te

    first = p1["items"][0]
    t1 = client.get(f"/thumb/{first['id']}")
    assert t1.status_code == 202
    body_202 = t1.json()
    assert body_202["pending"] is True
    assert body_202["job_id"]
    t2 = client.get(f"/thumb/{first['id']}")
    if t2.status_code == 202:
        assert t2.json()["job_id"] == body_202["job_id"]
    else:
        assert t2.status_code == 200

    # simulate worker completion and verify transition 202 -> 200
    src_path = root / first["path"]
    thumb_path = root / first["thumb"]
    assert make_thumb_sync(src_path, thumb_path) is True
    fast_thumb = client.get(f"/thumb-file/{first['id']}.jpg")
    assert fast_thumb.status_code == 200
    assert fast_thumb.headers["content-type"].startswith("image/jpeg")
    t3 = client.get(f"/thumb/{first['id']}")
    assert t3.status_code == 200
    assert t3.headers["content-type"].startswith("image/jpeg")

    status = client.get("/api/status")
    assert status.status_code == 200
    s = status.json()
    assert "workers" in s and "queues" in s
    assert "thumb_queue_depth" in s["queues"]
    assert "thumb_running" in s["queues"]

    _cleanup_root_data(str(root))


def test_tags_and_sessions(client):
    tag_name = f"pytest-{uuid.uuid4().hex[:8]}"
    created = client.post("/api/tags", json={"name": tag_name})
    assert created.status_code == 200
    assert any(t["name"] == tag_name for t in created.json()["tags"])

    renamed = f"{tag_name}-renamed"
    upd = client.patch(f"/api/tags/{tag_name}", json={"name": renamed, "color": "#AABBCC"})
    assert upd.status_code == 200
    assert upd.json()["tag"]["name"] == renamed
    assert upd.json()["tag"]["color"] == "#AABBCC"

    sess_patch = client.patch(
        "/api/session",
        json={
            "search_tags": [renamed],
            "search_mode": "all",
            "tabs": [{"id": "t1", "title": "T", "includeTags": [renamed], "excludeTags": []}],
            "active_tab_id": "t1",
        },
    )
    assert sess_patch.status_code == 200

    sess = client.get("/api/session")
    assert sess.status_code == 200
    s = sess.json()
    assert s["search_mode"] == "all"
    assert renamed.lower() in [x.lower() for x in s["search_tags"]]

    deleted = client.delete(f"/api/tags/{renamed}")
    assert deleted.status_code == 200
