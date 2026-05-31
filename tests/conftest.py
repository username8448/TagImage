import os
import json
from collections.abc import Generator

import pytest


def detect_test_database() -> str | None:
    raw = os.getenv("TEST_DATABASE_URL", "").strip()
    return raw or None


def db_is_test_safe(db_url: str | None) -> bool:
    if not db_url:
        return False
    lowered = db_url.lower()
    markers = ("test", "pytest", "tagimage_test")
    return any(marker in lowered for marker in markers)


def snapshot_app_session(cur):
    cur.execute(
        """
        SELECT root_path, root_paths, search_tags, search_mode, last_image_id, tabs, active_tab_id
        FROM app_session
        WHERE id = 1
        """
    )
    return cur.fetchone()


def restore_app_session(cur, snapshot) -> None:
    if snapshot is None:
        cur.execute(
            """
            INSERT INTO app_session (id)
            VALUES (1)
            ON CONFLICT (id) DO NOTHING
            """
        )
        cur.execute(
            """
            UPDATE app_session
            SET root_path = NULL,
                root_paths = '{}'::text[],
                search_tags = '{}'::text[],
                search_mode = 'any',
                last_image_id = NULL,
                tabs = '[]'::jsonb,
                active_tab_id = NULL,
                updated_at = now()
            WHERE id = 1
            """
        )
        return

    cur.execute(
        """
        INSERT INTO app_session (id)
        VALUES (1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    tabs_json = json.dumps(snapshot[5])
    cur.execute(
        """
        UPDATE app_session
        SET root_path = %s,
            root_paths = %s,
            search_tags = %s,
            search_mode = %s,
            last_image_id = %s,
            tabs = %s::jsonb,
            active_tab_id = %s,
            updated_at = now()
        WHERE id = 1
        """,
        (
            snapshot[0],
            snapshot[1],
            snapshot[2],
            snapshot[3],
            snapshot[4],
            tabs_json,
            snapshot[6],
        ),
    )


def truncate_test_tables(cur) -> None:
    cur.execute(
        """
        TRUNCATE TABLE
            job_events,
            job_attempts,
            jobs,
            image_tags,
            images,
            suppressed_auto_tags,
            tags
        RESTART IDENTITY CASCADE
        """
    )
    restore_app_session(cur, None)


def cleanup_test_rows(cur) -> None:
    pytest_roots_patterns = (
        "/tmp/pytest-%",
        "/private/tmp/pytest-%",
    )

    cur.execute(
        """
        DELETE FROM jobs
        WHERE payload->>'root_path' LIKE %s
           OR payload->>'root_path' LIKE %s
        """,
        pytest_roots_patterns,
    )
    cur.execute(
        """
        DELETE FROM images
        WHERE root_path LIKE %s
           OR root_path LIKE %s
        """,
        pytest_roots_patterns,
    )

    # Cleanup explicit test tags without touching user data broadly.
    cur.execute(
        """
        DELETE FROM suppressed_auto_tags
        WHERE normalized LIKE 'pytest-%'
        """
    )
    cur.execute(
        """
        DELETE FROM tags t
        WHERE t.normalized LIKE 'pytest-%'
          AND NOT EXISTS (
              SELECT 1
              FROM image_tags it
              WHERE it.tag_id = t.id
          )
        """
    )


def has_non_test_active_jobs(cur) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM jobs
            WHERE state IN ('queued', 'running')
              AND (
                  payload->>'root_path' IS NULL
                  OR (
                      payload->>'root_path' NOT LIKE '/tmp/pytest-%'
                      AND payload->>'root_path' NOT LIKE '/private/tmp/pytest-%'
                  )
              )
        )
        """
    )
    row = cur.fetchone()
    return bool(row and row[0])


def sanitize_app_session_paths(cur) -> None:
    cur.execute(
        """
        SELECT root_path, root_paths
        FROM app_session
        WHERE id = 1
        """
    )
    row = cur.fetchone()
    if row is None:
        return

    root_path, root_paths = row
    paths = list(root_paths or [])

    def is_pytest_path(value: str | None) -> bool:
        if not value:
            return False
        return value.startswith("/tmp/pytest-") or value.startswith("/private/tmp/pytest-")

    cleaned_paths = [value for value in paths if not is_pytest_path(value)]
    cleaned_root = root_path if not is_pytest_path(root_path) else None

    if cleaned_root is None and cleaned_paths:
        cleaned_root = cleaned_paths[0]

    if cleaned_root != root_path or cleaned_paths != paths:
        cur.execute(
            """
            UPDATE app_session
            SET root_path = %s,
                root_paths = %s,
                updated_at = now()
            WHERE id = 1
            """,
            (cleaned_root, cleaned_paths),
        )


@pytest.fixture(scope="session", autouse=True)
def configure_test_database() -> None:
    test_db = detect_test_database()
    if not test_db:
        os.environ.setdefault("TAGIMAGE_TEST_DB_ISOLATION", "fallback")
        return

    if not db_is_test_safe(test_db):
        pytest.exit(
            "TEST_DATABASE_URL is set but looks unsafe. "
            "Use a DB name/URL containing test/pytest/tagimage_test.",
            returncode=2,
        )

    os.environ["DATABASE_URL"] = test_db
    os.environ["TAGIMAGE_TEST_DB_ISOLATION"] = "test_db"


@pytest.fixture(autouse=True)
def isolate_integration_db_state(request) -> Generator[None, None, None]:
    module_name = getattr(request.module, "__name__", "")
    if not (module_name.endswith("test_api_integration") or module_name.endswith("test_job_recovery")):
        yield
        return

    isolation_mode = os.getenv("TAGIMAGE_TEST_DB_ISOLATION", "fallback")

    from app.repo.db import db_connect, ensure_db_ready

    ensure_db_ready()

    with db_connect() as conn:
        with conn.cursor() as cur:
            if isolation_mode != "test_db" and has_non_test_active_jobs(cur):
                pytest.skip(
                    "Integration DB tests require TEST_DATABASE_URL when non-test queued/running jobs exist "
                    "in the primary DATABASE_URL."
                )
            snapshot = snapshot_app_session(cur)
            if isolation_mode == "test_db":
                truncate_test_tables(cur)
            else:
                cleanup_test_rows(cur)
                # Run each integration test with an empty session state,
                # then restore the user session in teardown.
                restore_app_session(cur, None)

    try:
        yield
    finally:
        with db_connect() as conn:
            with conn.cursor() as cur:
                if isolation_mode == "test_db":
                    truncate_test_tables(cur)
                else:
                    cleanup_test_rows(cur)
                    restore_app_session(cur, snapshot)
                    sanitize_app_session_paths(cur)
