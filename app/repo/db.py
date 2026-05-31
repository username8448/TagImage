import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

from ..config import (
    DATABASE_URL,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_TYPE_RESCAN,
    JOB_TYPE_THUMB,
    JOB_TTL_HOURS,
    RESCAN_MAX_BACKOFF_SEC,
    THUMB_MAX_BACKOFF_SEC,
    VALID_JOB_STATES,
    job_stale_running_sec,
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    PSYCOPG_AVAILABLE = True
except ImportError:
    psycopg = None
    dict_row = None
    Jsonb = None
    PSYCOPG_AVAILABLE = False

try:
    from psycopg_pool import ConnectionPool

    PSYCOPG_POOL_AVAILABLE = True
except ImportError:
    ConnectionPool = None
    PSYCOPG_POOL_AVAILABLE = False


db_lock = threading.Lock()
db_initialized = False
db_last_error: Optional[str] = None
pool_lock = threading.Lock()
db_pools: dict[tuple[str, bool], Any] = {}


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _pool_key(row_factory=None, autocommit: bool = True) -> tuple[str, bool]:
    row_name = "dict" if row_factory is dict_row else "tuple"
    return (row_name, autocommit)


def _get_pool(*, row_factory=None, autocommit: bool = True):
    if not PSYCOPG_POOL_AVAILABLE:
        return None
    key = _pool_key(row_factory=row_factory, autocommit=autocommit)
    with pool_lock:
        pool = db_pools.get(key)
        if pool is not None:
            return pool
        kwargs: dict[str, Any] = {"autocommit": autocommit}
        if row_factory is not None:
            kwargs["row_factory"] = row_factory
        pool = ConnectionPool(
            conninfo=DATABASE_URL,
            kwargs=kwargs,
            min_size=1,
            max_size=12,
            open=True,
        )
        db_pools[key] = pool
        return pool


@contextmanager
def db_connect(*, row_factory=None, autocommit: bool = True):
    if not PSYCOPG_AVAILABLE:
        raise RuntimeError("psycopg is not installed. Run: pip install -r requirements.txt")
    pool = _get_pool(row_factory=row_factory, autocommit=autocommit)
    if pool is not None:
        with pool.connection() as conn:
            yield conn
        return
    kwargs = {}
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    conn = psycopg.connect(DATABASE_URL, connect_timeout=5, **kwargs)
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        conn.close()


def close_db_pools() -> None:
    with pool_lock:
        pools = list(db_pools.values())
        db_pools.clear()
    for pool in pools:
        pool.close()


def ensure_db_ready() -> None:
    global db_initialized, db_last_error
    if db_initialized:
        return

    with db_lock:
        if db_initialized:
            return
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS images (
                            id text PRIMARY KEY,
                            root_path text NOT NULL,
                            path text NOT NULL,
                            thumb text NOT NULL,
                            size bigint NOT NULL DEFAULT 0,
                            mtime bigint NOT NULL DEFAULT 0,
                            width integer NOT NULL DEFAULT 0,
                            height integer NOT NULL DEFAULT 0,
                            hidden boolean NOT NULL DEFAULT false,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            UNIQUE (root_path, path)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS tags (
                            id bigserial PRIMARY KEY,
                            name text NOT NULL,
                            normalized text NOT NULL UNIQUE,
                            color text,
                            user_defined boolean NOT NULL DEFAULT false,
                            created_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("ALTER TABLE tags ADD COLUMN IF NOT EXISTS color text")
                    cur.execute(
                        """
                        ALTER TABLE tags
                        ADD COLUMN IF NOT EXISTS user_defined boolean NOT NULL DEFAULT false
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS suppressed_auto_tags (
                            normalized text PRIMARY KEY,
                            name text NOT NULL,
                            created_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS image_tags (
                            image_id text NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                            tag_id bigint NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                            kind text NOT NULL CHECK (kind IN ('auto', 'user')),
                            created_at timestamptz NOT NULL DEFAULT now(),
                            PRIMARY KEY (image_id, tag_id, kind)
                        )
                        """
                    )
                    cur.execute(
                        """
                        UPDATE tags
                        SET user_defined = true
                        WHERE user_defined = false
                          AND EXISTS (
                              SELECT 1
                              FROM image_tags it
                              WHERE it.tag_id = tags.id
                                AND it.kind = 'user'
                          )
                        """
                    )
                    cur.execute("DELETE FROM image_tags WHERE kind = 'auto'")
                    cur.execute(
                        """
                        DELETE FROM tags t
                        WHERE t.user_defined = false
                          AND NOT EXISTS (
                              SELECT 1
                              FROM image_tags it
                              WHERE it.tag_id = t.id
                          )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS app_session (
                            id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                            root_path text,
                            root_paths text[] NOT NULL DEFAULT '{}',
                            search_tags text[] NOT NULL DEFAULT '{}',
                            search_mode text NOT NULL DEFAULT 'any',
                            last_image_id text,
                            tabs jsonb NOT NULL DEFAULT '[]'::jsonb,
                            active_tab_id text,
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute(
                        """
                        ALTER TABLE app_session
                        ADD COLUMN IF NOT EXISTS root_paths text[] NOT NULL DEFAULT '{}'
                        """
                    )
                    cur.execute(
                        """
                        ALTER TABLE app_session
                        ADD COLUMN IF NOT EXISTS tabs jsonb NOT NULL DEFAULT '[]'::jsonb
                        """
                    )
                    cur.execute(
                        """
                        ALTER TABLE app_session
                        ADD COLUMN IF NOT EXISTS active_tab_id text
                        """
                    )
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
                        SET root_paths = ARRAY[root_path]
                        WHERE root_path IS NOT NULL
                          AND cardinality(root_paths) = 0
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS jobs (
                            id text PRIMARY KEY,
                            job_type text NOT NULL,
                            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                            dedupe_key text,
                            state text NOT NULL CHECK (state IN ('queued','running','succeeded','failed','canceled')),
                            priority integer NOT NULL DEFAULT 0,
                            attempt integer NOT NULL DEFAULT 0,
                            max_attempts integer NOT NULL DEFAULT 3,
                            progress_done integer NOT NULL DEFAULT 0,
                            progress_total integer NOT NULL DEFAULT 0,
                            error text,
                            worker_id text,
                            scheduled_at timestamptz NOT NULL DEFAULT now(),
                            started_at timestamptz,
                            finished_at timestamptz,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_key text")
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS job_attempts (
                            id bigserial PRIMARY KEY,
                            job_id text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            attempt integer NOT NULL,
                            worker_id text,
                            started_at timestamptz NOT NULL DEFAULT now(),
                            finished_at timestamptz,
                            state text,
                            error text
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS job_events (
                            id bigserial PRIMARY KEY,
                            job_id text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            event text NOT NULL,
                            data jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )

                    # query indexes
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS images_root_hidden_idx ON images(root_path, hidden)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS images_root_hidden_sort_idx ON images(root_path, hidden, lower(path), path, id)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS images_root_hidden_mtime_idx ON images(root_path, hidden, mtime, lower(path), path, id)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS images_root_hidden_size_idx ON images(root_path, hidden, size, lower(path), path, id)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS tags_normalized_idx ON tags(normalized)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS image_tags_image_idx ON image_tags(image_id)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS image_tags_image_kind_idx ON image_tags(image_id, kind)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS image_tags_tag_idx ON image_tags(tag_id)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS image_tags_tag_kind_idx ON image_tags(tag_id, kind)"
                    )

                    # queue indexes
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS jobs_queue_lookup_idx ON jobs(state, scheduled_at, priority DESC, created_at)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS jobs_type_state_idx ON jobs(job_type, state, created_at DESC)"
                    )
                    cur.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe_idx
                        ON jobs(job_type, dedupe_key)
                        WHERE dedupe_key IS NOT NULL AND state IN ('queued', 'running')
                        """
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS job_attempts_job_id_idx ON job_attempts(job_id, started_at DESC)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS job_events_job_id_idx ON job_events(job_id, created_at DESC)"
                    )
            db_initialized = True
            db_last_error = None
        except Exception as exc:
            db_last_error = str(exc)
            raise


def db_health() -> dict[str, Any]:
    try:
        ensure_db_ready()
        return {"db_ready": True, "db_error": None}
    except Exception as exc:
        return {"db_ready": False, "db_error": str(exc)}


def normalize_tag(tag: str) -> str:
    return " ".join(tag.strip().split()).lower()


def clean_tag_list(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        name = " ".join(str(tag).strip().split())
        norm = normalize_tag(name)
        if not name or norm in seen:
            continue
        cleaned.append(name)
        seen.add(norm)
    return cleaned


def parse_csv_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [normalize_tag(t) for t in raw.split(",") if t.strip()]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_job(
    job_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    priority: int = 0,
    max_attempts: int = 3,
    dedupe_key: Optional[str] = None,
) -> dict[str, Any]:
    ensure_db_ready()
    job_id = uuid.uuid4().hex
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if dedupe_key:
                cur.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE job_type = %s
                      AND dedupe_key = %s
                      AND state IN ('queued', 'running')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (job_type, dedupe_key),
                )
                existing = cur.fetchone()
                if existing is not None:
                    out = dict(existing)
                    out["__deduped"] = True
                    return out

            try:
                cur.execute(
                    """
                    INSERT INTO jobs (id, job_type, payload, state, priority, max_attempts, scheduled_at, dedupe_key)
                    VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
                    RETURNING *
                    """,
                    (
                        job_id,
                        job_type,
                        Jsonb(payload or {}) if Jsonb is not None else (payload or {}),
                        JOB_STATE_QUEUED,
                        int(priority),
                        int(max(1, max_attempts)),
                        dedupe_key,
                    ),
                )
                job = cur.fetchone()
            except Exception:
                if dedupe_key:
                    cur.execute(
                        """
                        SELECT *
                        FROM jobs
                        WHERE job_type = %s
                          AND dedupe_key = %s
                          AND state IN ('queued', 'running')
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (job_type, dedupe_key),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        out = dict(existing)
                        out["__deduped"] = True
                        return out
                raise
            out = dict(job) if job is not None else {}
            out["__deduped"] = False
            cur.execute(
                """
                INSERT INTO job_events (job_id, event, data)
                VALUES (%s, 'enqueued', %s)
                """,
                (job_id, Jsonb({"job_type": job_type}) if Jsonb is not None else {"job_type": job_type}),
            )
            return out


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            return cur.fetchone()


def list_jobs(*, job_type: Optional[str] = None, state: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    ensure_db_ready()
    where = []
    params: list[Any] = []
    if job_type:
        where.append("job_type = %s")
        params.append(job_type)
    if state:
        if state not in VALID_JOB_STATES:
            return []
        where.append("state = %s")
        params.append(state)

    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(max(1, min(int(limit), 200)))

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def count_jobs(*, job_type: Optional[str] = None, state: Optional[str] = None) -> int:
    ensure_db_ready()
    where = []
    params: list[Any] = []
    if job_type:
        where.append("job_type = %s")
        params.append(job_type)
    if state:
        if state not in VALID_JOB_STATES:
            return 0
        where.append("state = %s")
        params.append(state)
    sql = "SELECT COUNT(*) FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0] if row else 0)


def count_stale_running_jobs(
    *, job_type: Optional[str] = None, stale_after_sec: Optional[int] = None
) -> int:
    stale_sec = job_stale_running_sec() if stale_after_sec is None else int(stale_after_sec)
    if stale_sec <= 0:
        return 0

    ensure_db_ready()
    where = [
        "state = %s",
        """
        GREATEST(
            COALESCE(started_at, created_at, '-infinity'::timestamptz),
            COALESCE(updated_at, started_at, created_at, '-infinity'::timestamptz)
        ) <= now() - (%s * INTERVAL '1 second')
        """,
    ]
    params: list[Any] = [JOB_STATE_RUNNING, stale_sec]
    if job_type:
        where.append("job_type = %s")
        params.append(job_type)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE " + " AND ".join(where), params)
            row = cur.fetchone()
            return int(row[0] if row else 0)


def find_active_job_by_dedupe(job_type: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM jobs
                WHERE job_type = %s
                  AND dedupe_key = %s
                  AND state IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (job_type, dedupe_key),
            )
            return cur.fetchone()


def claim_next_job(worker_id: str, *, job_type: Optional[str] = None) -> Optional[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                where = ["j.state = 'queued'", "j.scheduled_at <= now()"]
                params: list[Any] = []
                if job_type:
                    where.append("j.job_type = %s")
                    params.append(job_type)

                cur.execute(
                    f"""
                    WITH picked AS (
                        SELECT j.id
                        FROM jobs j
                        WHERE {' AND '.join(where)}
                        ORDER BY j.priority DESC, j.scheduled_at, j.created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs j
                    SET state = %s,
                        worker_id = %s,
                        started_at = COALESCE(j.started_at, now()),
                        attempt = j.attempt + 1,
                        updated_at = now(),
                        error = NULL
                    FROM picked
                    WHERE j.id = picked.id
                    RETURNING j.*
                    """,
                    [*params, JOB_STATE_RUNNING, worker_id],
                )
                job = cur.fetchone()
                if not job:
                    conn.rollback()
                    return None

                cur.execute(
                    """
                    INSERT INTO job_attempts (job_id, attempt, worker_id, state)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (job["id"], job["attempt"], worker_id, JOB_STATE_RUNNING),
                )
                cur.execute(
                    """
                    INSERT INTO job_events (job_id, event, data)
                    VALUES (%s, 'started', %s)
                    """,
                    (
                        job["id"],
                        Jsonb({"attempt": int(job["attempt"]), "worker_id": worker_id}) if Jsonb is not None else {"attempt": int(job["attempt"]), "worker_id": worker_id},
                    ),
                )
            conn.commit()
            return job
        except Exception:
            conn.rollback()
            raise


def touch_job_progress(job_id: str, *, done: int, total: Optional[int] = None) -> None:
    ensure_db_ready()
    if total is None:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET progress_done = GREATEST(0, %s), updated_at = now()
                    WHERE id = %s
                    """,
                    (int(done), job_id),
                )
    else:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET progress_done = GREATEST(0, %s),
                        progress_total = GREATEST(0, %s),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (int(done), int(total), job_id),
                )


def _set_last_attempt_state(cur, job_id: str, state: str, error: Optional[str] = None) -> None:
    cur.execute(
        """
        UPDATE job_attempts
        SET finished_at = now(), state = %s, error = %s
        WHERE id = (
            SELECT id
            FROM job_attempts
            WHERE job_id = %s
            ORDER BY started_at DESC
            LIMIT 1
        )
        """,
        (state, error, job_id),
    )


def mark_job_succeeded(job_id: str, *, total: Optional[int] = None) -> None:
    ensure_db_ready()
    with db_connect(row_factory=dict_row, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                if total is None:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET state = %s,
                            finished_at = now(),
                            error = NULL,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING progress_done, progress_total
                        """,
                        (JOB_STATE_SUCCEEDED, job_id),
                    )
                    row = cur.fetchone()
                    done = int(row["progress_done"] or 0) if row else 0
                    resolved_total = int(row["progress_total"] or done) if row else done
                else:
                    resolved_total = int(total)
                    cur.execute(
                        """
                        UPDATE jobs
                        SET state = %s,
                            progress_done = GREATEST(0, %s),
                            progress_total = GREATEST(0, %s),
                            finished_at = now(),
                            error = NULL,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (JOB_STATE_SUCCEEDED, resolved_total, resolved_total, job_id),
                    )
                _set_last_attempt_state(cur, job_id, JOB_STATE_SUCCEEDED, None)
                cur.execute(
                    """
                    INSERT INTO job_events (job_id, event, data)
                    VALUES (%s, 'succeeded', %s)
                    """,
                    (
                        job_id,
                        Jsonb({"total": resolved_total}) if Jsonb is not None else {"total": resolved_total},
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def mark_job_failed(job_id: str, error: str) -> None:
    ensure_db_ready()
    with db_connect(row_factory=dict_row, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, job_type, attempt, max_attempts FROM jobs WHERE id = %s FOR UPDATE",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return

                attempt = int(row["attempt"] or 0)
                max_attempts = int(row["max_attempts"] or 1)
                job_type = row["job_type"] or ""
                retries_left = max(0, max_attempts - attempt)
                if retries_left > 0:
                    max_backoff = THUMB_MAX_BACKOFF_SEC if job_type == JOB_TYPE_THUMB else RESCAN_MAX_BACKOFF_SEC if job_type == JOB_TYPE_RESCAN else 120
                    backoff = min(int(max_backoff), 2 ** max(1, attempt))
                    cur.execute(
                        """
                        UPDATE jobs
                        SET state = %s,
                            error = %s,
                            scheduled_at = now() + (%s * INTERVAL '1 second'),
                            worker_id = NULL,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (JOB_STATE_QUEUED, error, backoff, job_id),
                    )
                    attempt_state = JOB_STATE_QUEUED
                    event = "retry_scheduled"
                else:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET state = %s,
                            error = %s,
                            finished_at = now(),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (JOB_STATE_FAILED, error, job_id),
                    )
                    attempt_state = JOB_STATE_FAILED
                    event = "failed"

                _set_last_attempt_state(cur, job_id, attempt_state, error)
                cur.execute(
                    """
                    INSERT INTO job_events (job_id, event, data)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        job_id,
                        event,
                        Jsonb({"error": error, "attempt": attempt, "max_attempts": max_attempts}) if Jsonb is not None else {"error": error, "attempt": attempt, "max_attempts": max_attempts},
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def recover_stale_running_jobs(
    *, stale_after_sec: Optional[int] = None, limit: int = 100
) -> dict[str, Any]:
    stale_sec = job_stale_running_sec() if stale_after_sec is None else int(stale_after_sec)
    if stale_sec <= 0:
        return {"disabled": True, "checked": 0, "recovered": 0, "requeued": 0, "failed": 0}

    ensure_db_ready()
    checked = 0
    requeued = 0
    failed = 0
    capped_limit = max(1, min(int(limit), 500))

    with db_connect(row_factory=dict_row, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH stale AS (
                        SELECT *,
                               GREATEST(
                                   COALESCE(started_at, created_at, '-infinity'::timestamptz),
                                   COALESCE(updated_at, started_at, created_at, '-infinity'::timestamptz)
                               ) AS last_activity_at
                        FROM jobs
                        WHERE state = %s
                          AND GREATEST(
                              COALESCE(started_at, created_at, '-infinity'::timestamptz),
                              COALESCE(updated_at, started_at, created_at, '-infinity'::timestamptz)
                          ) <= now() - (%s * INTERVAL '1 second')
                        ORDER BY updated_at NULLS FIRST, started_at NULLS FIRST, created_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    SELECT *,
                           EXTRACT(EPOCH FROM (now() - last_activity_at))::bigint AS age_sec
                    FROM stale
                    """,
                    (JOB_STATE_RUNNING, stale_sec, capped_limit),
                )
                stale_jobs = list(cur.fetchall())
                checked = len(stale_jobs)

                for job in stale_jobs:
                    job_id = job["id"]
                    job_type = job.get("job_type") or "unknown"
                    previous_worker = job.get("worker_id")
                    attempt = int(job.get("attempt") or 0)
                    max_attempts = int(job.get("max_attempts") or 1)
                    age_sec = int(job.get("age_sec") or 0)

                    if attempt < max_attempts:
                        next_state = JOB_STATE_QUEUED
                        error = "stale running job recovered"
                        event_name = "recovered"
                        cur.execute(
                            """
                            UPDATE jobs
                            SET state = %s,
                                scheduled_at = now(),
                                worker_id = NULL,
                                error = %s,
                                updated_at = now()
                            WHERE id = %s
                              AND state = %s
                            """,
                            (next_state, error, job_id, JOB_STATE_RUNNING),
                        )
                        requeued += cur.rowcount
                    else:
                        next_state = JOB_STATE_FAILED
                        error = "stale running job exceeded max attempts"
                        event_name = "recovered_failed"
                        cur.execute(
                            """
                            UPDATE jobs
                            SET state = %s,
                                error = %s,
                                finished_at = now(),
                                updated_at = now()
                            WHERE id = %s
                              AND state = %s
                            """,
                            (next_state, error, job_id, JOB_STATE_RUNNING),
                        )
                        failed += cur.rowcount

                    if cur.rowcount <= 0:
                        continue

                    _set_last_attempt_state(cur, job_id, next_state, error)
                    event_data = {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "previous_worker": previous_worker,
                        "age_sec": age_sec,
                        "stale_after_sec": stale_sec,
                        "next_state": next_state,
                    }
                    cur.execute(
                        """
                        INSERT INTO job_events (job_id, event, data)
                        VALUES (%s, %s, %s)
                        """,
                        (job_id, event_name, Jsonb(event_data) if Jsonb is not None else event_data),
                    )
                    print(
                        "[jobs] recovered stale job "
                        f"id={job_id} type={job_type} previous_worker={previous_worker} "
                        f"age_sec={age_sec} next_state={next_state}",
                        flush=True,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    recovered = requeued + failed
    return {
        "disabled": False,
        "checked": checked,
        "recovered": recovered,
        "requeued": requeued,
        "failed": failed,
    }


def cleanup_old_jobs(*, ttl_hours: Optional[int] = None) -> dict[str, int]:
    ensure_db_ready()
    keep_hours = int(ttl_hours or JOB_TTL_HOURS)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM jobs
                WHERE state IN ('succeeded', 'failed', 'canceled')
                  AND finished_at IS NOT NULL
                  AND finished_at < now() - (%s * INTERVAL '1 hour')
                """,
                (keep_hours,),
            )
            removed_jobs = int(cur.rowcount or 0)
    return {"removed_jobs": removed_jobs}


def cancel_job(job_id: str) -> bool:
    ensure_db_ready()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET state = 'canceled', finished_at = now(), updated_at = now()
                WHERE id = %s AND state IN ('queued','running')
                """,
                (job_id,),
            )
            return cur.rowcount > 0


def serialize_job(job: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if job is None:
        return None
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = {"raw": payload}
    return {
        "id": job.get("id"),
        "type": job.get("job_type"),
        "state": job.get("state"),
        "priority": int(job.get("priority") or 0),
        "attempt": int(job.get("attempt") or 0),
        "max_attempts": int(job.get("max_attempts") or 0),
        "progress": {
            "done": int(job.get("progress_done") or 0),
            "total": int(job.get("progress_total") or 0),
        },
        "error": job.get("error"),
        "worker_id": job.get("worker_id"),
        "payload": payload,
        "scheduled_at": job.get("scheduled_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }
