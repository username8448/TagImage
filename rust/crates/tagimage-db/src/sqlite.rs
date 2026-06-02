use crate::sqlite_schema::{INDEX_STATEMENTS, SQLITE_SCHEMA_VERSION, TABLE_STATEMENTS};
use rusqlite::Connection;
use std::path::Path;
use std::time::Duration;

pub fn init_sqlite_db(path: &Path) -> Result<Connection, String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("create sqlite parent dir {}: {e}", parent.display()))?;
        }
    }

    let mut conn =
        Connection::open(path).map_err(|e| format!("open sqlite db {}: {e}", path.display()))?;

    conn.busy_timeout(Duration::from_millis(5_000))
        .map_err(|e| format!("set sqlite busy_timeout: {e}"))?;
    conn.execute_batch(
        r#"
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;
        PRAGMA busy_timeout = 5000;
        "#,
    )
    .map_err(|e| format!("set sqlite pragmas: {e}"))?;

    let tx = conn
        .transaction()
        .map_err(|e| format!("begin sqlite schema tx: {e}"))?;
    for statement in TABLE_STATEMENTS {
        tx.execute_batch(statement)
            .map_err(|e| format!("create sqlite table: {e}"))?;
    }
    for statement in INDEX_STATEMENTS {
        tx.execute_batch(statement)
            .map_err(|e| format!("create sqlite index: {e}"))?;
    }
    tx.execute(
        r#"
        INSERT INTO tagimage_schema_version (id, version)
        VALUES (1, ?1)
        ON CONFLICT(id) DO NOTHING
        "#,
        [SQLITE_SCHEMA_VERSION],
    )
    .map_err(|e| format!("record sqlite schema version: {e}"))?;

    let version = tx
        .query_row(
            "SELECT version FROM tagimage_schema_version WHERE id = 1",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| format!("read sqlite schema version: {e}"))?;
    if version != SQLITE_SCHEMA_VERSION {
        return Err(format!(
            "unsupported sqlite schema version {version}; expected {SQLITE_SCHEMA_VERSION}"
        ));
    }

    tx.commit()
        .map_err(|e| format!("commit sqlite schema tx: {e}"))?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::init_sqlite_db;
    use crate::sqlite_schema::SQLITE_SCHEMA_VERSION;
    use rusqlite::Connection;
    use std::collections::HashSet;

    fn temp_db_path() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("tagimage.sqlite");
        (dir, db_path)
    }

    fn names(conn: &Connection, sql: &str) -> HashSet<String> {
        let mut stmt = conn.prepare(sql).expect("prepare names query");
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .expect("query names");
        rows.map(|row| row.expect("name row")).collect()
    }

    #[test]
    fn init_creates_schema_and_is_idempotent() {
        let (_dir, db_path) = temp_db_path();

        let conn = init_sqlite_db(&db_path).expect("first init");
        drop(conn);
        let conn = init_sqlite_db(&db_path).expect("second init");

        let tables = names(
            &conn,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        );
        for expected in [
            "tagimage_schema_version",
            "images",
            "tags",
            "suppressed_auto_tags",
            "image_tags",
            "app_session",
            "jobs",
            "job_attempts",
            "job_events",
        ] {
            assert!(tables.contains(expected), "missing table {expected}");
        }

        let version: i64 = conn
            .query_row(
                "SELECT version FROM tagimage_schema_version WHERE id = 1",
                [],
                |row| row.get(0),
            )
            .expect("schema version");
        assert_eq!(version, SQLITE_SCHEMA_VERSION);
    }

    #[test]
    fn init_creates_required_indexes() {
        let (_dir, db_path) = temp_db_path();
        let conn = init_sqlite_db(&db_path).expect("init");

        let indexes = names(
            &conn,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'",
        );
        for expected in [
            "images_root_path_idx",
            "images_root_hidden_idx",
            "images_root_hidden_sort_idx",
            "images_root_hidden_mtime_idx",
            "images_root_hidden_size_idx",
            "tags_name_idx",
            "tags_normalized_idx",
            "image_tags_image_idx",
            "image_tags_image_kind_idx",
            "image_tags_tag_idx",
            "image_tags_tag_kind_idx",
            "jobs_queue_lookup_idx",
            "jobs_type_state_idx",
            "jobs_type_state_scheduled_idx",
            "jobs_active_dedupe_idx",
            "job_attempts_job_id_idx",
            "job_events_job_id_idx",
        ] {
            assert!(indexes.contains(expected), "missing index {expected}");
        }
    }

    #[test]
    fn init_sets_required_pragmas() {
        let (_dir, db_path) = temp_db_path();
        let conn = init_sqlite_db(&db_path).expect("init");

        let enabled: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .expect("foreign_keys pragma");
        assert_eq!(enabled, 1);

        let timeout_ms: i64 = conn
            .query_row("PRAGMA busy_timeout", [], |row| row.get(0))
            .expect("busy_timeout pragma");
        assert_eq!(timeout_ms, 5_000);
    }

    #[test]
    fn basic_insert_select_works_for_index_tables() {
        let (_dir, db_path) = temp_db_path();
        let conn = init_sqlite_db(&db_path).expect("init");

        conn.execute(
            r#"
            INSERT INTO app_session (id, root_path, root_paths, search_tags, tabs)
            VALUES (1, ?1, ?2, ?3, ?4)
            "#,
            [
                "/photos",
                r#"["/photos"]"#,
                r#"["portrait"]"#,
                r#"[{"id":"tab-1"}]"#,
            ],
        )
        .expect("insert app_session");

        conn.execute(
            r#"
            INSERT INTO images (id, root_path, path, thumb, size, mtime, width, height, ext)
            VALUES ('img-1', '/photos', 'a/cat.jpg', '.imgindex/thumbs/img-1.jpg', 123, 456, 640, 480, 'jpg')
            "#,
            [],
        )
        .expect("insert image");

        conn.execute(
            "INSERT INTO tags (name, normalized, user_defined) VALUES ('Cat', 'cat', 1)",
            [],
        )
        .expect("insert tag");
        let tag_id = conn.last_insert_rowid();

        conn.execute(
            "INSERT INTO image_tags (image_id, tag_id, kind) VALUES ('img-1', ?1, 'user')",
            [tag_id],
        )
        .expect("insert image tag");

        conn.execute(
            r#"
            INSERT INTO jobs (id, job_type, payload, state, priority, dedupe_key)
            VALUES ('job-1', 'thumb', ?1, 'queued', 20, 'thumb:img-1:456')
            "#,
            [r#"{"image_id":"img-1"}"#],
        )
        .expect("insert job");

        conn.execute(
            "INSERT INTO job_attempts (job_id, attempt, worker_id, state) VALUES ('job-1', 1, 'worker-1', 'running')",
            [],
        )
        .expect("insert job attempt");
        conn.execute(
            "INSERT INTO job_events (job_id, event, data) VALUES ('job-1', 'enqueued', ?1)",
            [r#"{"job_type":"thumb"}"#],
        )
        .expect("insert job event");

        let image_count: i64 = conn
            .query_row(
                r#"
                SELECT COUNT(*)
                FROM images i
                JOIN image_tags it ON it.image_id = i.id
                JOIN tags t ON t.id = it.tag_id
                WHERE i.root_path = '/photos'
                  AND i.path = 'a/cat.jpg'
                  AND i.thumb = '.imgindex/thumbs/img-1.jpg'
                  AND i.ext = 'jpg'
                  AND t.normalized = 'cat'
                "#,
                [],
                |row| row.get(0),
            )
            .expect("select image/tag");
        assert_eq!(image_count, 1);

        let job_count: i64 = conn
            .query_row(
                r#"
                SELECT COUNT(*)
                FROM jobs j
                JOIN job_attempts ja ON ja.job_id = j.id
                JOIN job_events je ON je.job_id = j.id
                WHERE j.id = 'job-1'
                  AND j.job_type = 'thumb'
                  AND j.state = 'queued'
                  AND ja.attempt = 1
                  AND je.event = 'enqueued'
                "#,
                [],
                |row| row.get(0),
            )
            .expect("select job graph");
        assert_eq!(job_count, 1);

        let root_paths: String = conn
            .query_row(
                "SELECT root_paths FROM app_session WHERE id = 1",
                [],
                |row| row.get(0),
            )
            .expect("select root paths");
        assert_eq!(root_paths, r#"["/photos"]"#);
    }

    #[test]
    fn schema_has_no_media_blob_columns() {
        let (_dir, db_path) = temp_db_path();
        let conn = init_sqlite_db(&db_path).expect("init");

        let mut stmt = conn
            .prepare(
                r#"
                SELECT m.name, p.name, p.type
                FROM sqlite_master m
                JOIN pragma_table_info(m.name) p
                WHERE m.type = 'table'
                "#,
            )
            .expect("prepare schema query");
        let columns = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .expect("query columns");

        for column in columns {
            let (table, name, column_type) = column.expect("column row");
            assert!(
                !column_type.eq_ignore_ascii_case("BLOB"),
                "{table}.{name} must not be BLOB"
            );
            let lowered = name.to_ascii_lowercase();
            assert!(
                !matches!(
                    lowered.as_str(),
                    "original"
                        | "original_blob"
                        | "original_bytes"
                        | "image_blob"
                        | "image_bytes"
                        | "thumbnail_blob"
                        | "thumbnail_bytes"
                        | "thumb_blob"
                        | "thumb_bytes"
                        | "preview_blob"
                        | "preview_bytes"
                ),
                "{table}.{name} looks like media content storage"
            );
        }
    }
}
