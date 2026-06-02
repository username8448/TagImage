use crate::{config::AppConfig, error::ApiError};
use base64::{engine::general_purpose::URL_SAFE, Engine as _};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};
use tokio_postgres::{types::ToSql, Client, NoTls, Row};
use uuid::Uuid;

const DEFAULT_PAGE_LIMIT: i64 = 120;
const MAX_PAGE_LIMIT: i64 = 500;
const VALID_JOB_STATES: &[&str] = &["queued", "running", "succeeded", "failed", "canceled"];
const VALID_MATCH_MODES: &[&str] = &["any", "all"];
const IMAGE_SORTS: &[&str] = &[
    "path_asc",
    "path_desc",
    "date_desc",
    "date_asc",
    "size_desc",
    "size_asc",
];
const INDEX_DIR_NAME: &str = ".imgindex";
const THUMBS_DIR_NAME: &str = "thumbs";

#[derive(Clone)]
pub struct AppState {
    pub config: AppConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub root_path: Option<String>,
    pub root_paths: Vec<String>,
    pub search_tags: Vec<String>,
    pub search_mode: String,
    pub last_image_id: Option<String>,
    pub tabs: Value,
    pub active_tab_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ImageRecord {
    pub id: String,
    pub root_path: String,
    pub path: String,
    pub thumb: String,
    pub size: i64,
    pub mtime: i64,
    pub width: i32,
    pub height: i32,
}

#[derive(Debug, Clone)]
pub struct ImageRow {
    pub id: String,
    pub path: String,
    pub thumb: String,
    pub size: i64,
    pub mtime: i64,
    pub width: i32,
    pub height: i32,
    pub lower_path: String,
}

#[derive(Debug, Clone, Default)]
pub struct ImagesQuery {
    pub tags: Option<String>,
    pub include_tags: Option<String>,
    pub exclude_tags: Option<String>,
    pub match_mode: Option<String>,
    pub mode: Option<String>,
    pub limit: Option<i64>,
    pub cursor: Option<String>,
    pub sort: Option<String>,
    pub include_total: bool,
}

#[derive(Debug, Clone)]
struct CursorPayload {
    sort: String,
    lower_path: String,
    path: String,
    id: String,
    mtime: i64,
    size: i64,
}

#[derive(Debug, Clone, Default)]
pub struct ThumbRebuildInput {
    pub stale_only: bool,
    pub limit: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct ThumbRebuildResult {
    pub enqueued: i64,
    pub queued_existing: i64,
    pub skipped: i64,
    pub total: i64,
}

impl AppState {
    pub fn new(config: AppConfig) -> Self {
        Self { config }
    }

    pub async fn connect(&self) -> Result<Client, ApiError> {
        let (client, connection) = tokio_postgres::connect(&self.config.database_url, NoTls)
            .await
            .map_err(|e| ApiError::internal(format!("Database error: {e}")))?;
        tokio::spawn(async move {
            if let Err(error) = connection.await {
                eprintln!("[rust-api] postgres connection error: {error}");
            }
        });
        Ok(client)
    }

    pub async fn db_health(&self) -> Value {
        match self.connect().await {
            Ok(client) => match client.query_one("SELECT 1", &[]).await {
                Ok(_) => json!({"db_ready": true, "db_error": null}),
                Err(error) => json!({"db_ready": false, "db_error": error.to_string()}),
            },
            Err(error) => json!({"db_ready": false, "db_error": error.detail}),
        }
    }

    pub fn repo_path(&self, rel: &str) -> PathBuf {
        self.config.repo_root.join(rel)
    }

    pub fn thumb_rust_supported(&self) -> bool {
        self.repo_path("rust/thumb-worker/Cargo.toml").exists()
            || self
                .repo_path("rust/thumb-worker/target/release/imgviewer-thumb-worker")
                .exists()
    }

    pub fn scanner_rust_supported(&self) -> bool {
        self.repo_path("rust/scanner-worker/Cargo.toml").exists()
            || self
                .repo_path("rust/thumb-worker/target/release/imgviewer-scanner-worker")
                .exists()
    }

    pub fn metadata_rust_supported(&self) -> bool {
        self.repo_path("rust/metadata-worker/Cargo.toml").exists()
            || self
                .repo_path("rust/thumb-worker/target/release/imgviewer-metadata-worker")
                .exists()
    }
}

pub fn normalize_tag(tag: &str) -> String {
    tag.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

pub fn clean_tag_list(tags: &[String]) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for tag in tags {
        let name = tag.split_whitespace().collect::<Vec<_>>().join(" ");
        let norm = normalize_tag(&name);
        if name.is_empty() || !seen.insert(norm) {
            continue;
        }
        out.push(name);
    }
    out
}

pub fn parse_csv_tags(raw: Option<&str>) -> Vec<String> {
    raw.unwrap_or("")
        .split(',')
        .filter_map(|item| {
            let norm = normalize_tag(item);
            if norm.is_empty() {
                None
            } else {
                Some(norm)
            }
        })
        .collect()
}

fn normalize_color(value: Option<&Value>) -> Result<Option<String>, ApiError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(raw) = value.as_str() else {
        return Err(ApiError::bad_request(
            "Color must be a hex value like #E5E5E5",
        ));
    };
    let color = raw.trim();
    if color.is_empty() {
        return Ok(None);
    }
    let bytes = color.as_bytes();
    let valid =
        bytes.len() == 7 && bytes[0] == b'#' && bytes[1..].iter().all(|b| b.is_ascii_hexdigit());
    if !valid {
        return Err(ApiError::bad_request(
            "Color must be a hex value like #E5E5E5",
        ));
    }
    Ok(Some(color.to_ascii_uppercase()))
}

pub async fn load_session(client: &Client) -> Result<Session, ApiError> {
    let row = client
        .query_opt(
            r#"
            SELECT root_path, root_paths, search_tags, search_mode, last_image_id, tabs, active_tab_id
            FROM app_session
            WHERE id = 1
            "#,
            &[],
        )
        .await?;

    Ok(match row {
        Some(row) => Session {
            root_path: row.try_get("root_path").unwrap_or(None),
            root_paths: row.try_get("root_paths").unwrap_or_else(|_| Vec::new()),
            search_tags: row.try_get("search_tags").unwrap_or_else(|_| Vec::new()),
            search_mode: row
                .try_get::<_, Option<String>>("search_mode")
                .ok()
                .flatten()
                .unwrap_or_else(|| "any".to_string()),
            last_image_id: row.try_get("last_image_id").unwrap_or(None),
            tabs: row
                .try_get::<_, Option<Value>>("tabs")
                .ok()
                .flatten()
                .unwrap_or_else(|| json!([])),
            active_tab_id: row.try_get("active_tab_id").unwrap_or(None),
        },
        None => Session {
            root_path: None,
            root_paths: Vec::new(),
            search_tags: Vec::new(),
            search_mode: "any".to_string(),
            last_image_id: None,
            tabs: json!([]),
            active_tab_id: None,
        },
    })
}

pub async fn save_session_value(client: &Client, fields: &Value) -> Result<Session, ApiError> {
    let mut session = load_session(client).await?;
    let Some(object) = fields.as_object() else {
        return Err(ApiError::bad_request("Request body must be an object"));
    };

    if let Some(value) = object.get("root_path") {
        session.root_path = value.as_str().map(|item| item.to_string());
    }
    if let Some(value) = object.get("root_paths") {
        session.root_paths = string_array(value).unwrap_or_default();
    }
    if let Some(value) = object.get("search_tags") {
        let tags = string_array(value).unwrap_or_default();
        session.search_tags = clean_tag_list(&tags)
            .into_iter()
            .map(|tag| normalize_tag(&tag))
            .collect();
    }
    if let Some(value) = object.get("search_mode") {
        let mode = value.as_str().unwrap_or("any");
        session.search_mode = if VALID_MATCH_MODES.contains(&mode) {
            mode.to_string()
        } else {
            "any".to_string()
        };
    }
    if object.contains_key("last_image_id") {
        session.last_image_id = object
            .get("last_image_id")
            .and_then(|value| value.as_str())
            .map(|item| item.to_string());
    }
    if object.contains_key("tabs") {
        session.tabs = object.get("tabs").cloned().unwrap_or_else(|| json!([]));
        if session.tabs.is_null() {
            session.tabs = json!([]);
        }
    }
    if object.contains_key("active_tab_id") {
        session.active_tab_id = object
            .get("active_tab_id")
            .and_then(|value| value.as_str())
            .map(|item| item.to_string());
    }

    client
        .execute(
            r#"
            INSERT INTO app_session (id)
            VALUES (1)
            ON CONFLICT (id) DO NOTHING
            "#,
            &[],
        )
        .await?;
    client
        .execute(
            r#"
            UPDATE app_session
            SET root_path = $1,
                root_paths = $2,
                search_tags = $3,
                search_mode = $4,
                last_image_id = $5,
                tabs = $6,
                active_tab_id = $7,
                updated_at = now()
            WHERE id = 1
            "#,
            &[
                &session.root_path,
                &session.root_paths,
                &session.search_tags,
                &session.search_mode,
                &session.last_image_id,
                &session.tabs,
                &session.active_tab_id,
            ],
        )
        .await?;
    load_session(client).await
}

pub async fn set_root(client: &Client, path: &str, append: bool) -> Result<Session, ApiError> {
    let root = std::fs::canonicalize(Path::new(path))
        .map_err(|_| ApiError::bad_request(format!("Not a directory: {path}")))?;
    if !root.is_dir() {
        return Err(ApiError::bad_request(format!(
            "Not a directory: {}",
            root.display()
        )));
    }
    let root_str = root.to_string_lossy().to_string();
    let mut session = load_session(client).await?;
    if append {
        if !session.root_paths.iter().any(|item| item == &root_str) {
            session.root_paths.push(root_str.clone());
        }
    } else {
        session.root_paths = vec![root_str.clone()];
    }
    session.root_path = Some(root_str);
    save_session_value(
        client,
        &json!({
            "root_path": session.root_path,
            "root_paths": session.root_paths,
            "last_image_id": null
        }),
    )
    .await
}

pub fn roots_from_session(session: &Session) -> Vec<String> {
    let mut roots = Vec::new();
    for root in &session.root_paths {
        if !root.is_empty() && !roots.iter().any(|item| item == root) {
            roots.push(root.clone());
        }
    }
    if roots.is_empty() {
        if let Some(root) = &session.root_path {
            if !root.is_empty() {
                roots.push(root.clone());
            }
        }
    }
    roots
}

pub fn require_roots(session: &Session) -> Result<Vec<String>, ApiError> {
    let roots = roots_from_session(session);
    if roots.is_empty() {
        Err(ApiError::bad_request("No folder set"))
    } else {
        Ok(roots)
    }
}

pub fn active_root(session: &Session) -> Option<String> {
    roots_from_session(session)
        .last()
        .cloned()
        .or_else(|| session.root_path.clone())
}

pub async fn tag_summary_rows(client: &Client) -> Result<Vec<Value>, ApiError> {
    let rows = client
        .query(
            r#"
            SELECT
                t.id,
                t.name,
                t.normalized,
                t.color,
                t.user_defined,
                COUNT(DISTINCT CASE WHEN i.id IS NOT NULL THEN it.image_id END) AS image_count,
                COUNT(DISTINCT CASE WHEN i.id IS NOT NULL AND it.kind = 'auto' THEN it.image_id END) AS auto_count,
                COUNT(DISTINCT CASE WHEN i.id IS NOT NULL AND it.kind = 'user' THEN it.image_id END) AS user_count
            FROM tags t
            LEFT JOIN image_tags it ON it.tag_id = t.id
            LEFT JOIN images i ON i.id = it.image_id AND i.hidden = false
            GROUP BY t.id
            HAVING t.user_defined OR COUNT(DISTINCT i.id) > 0
            ORDER BY lower(t.name), t.name
            "#,
            &[],
        )
        .await?;
    Ok(rows.iter().map(tag_summary_value).collect())
}

fn tag_summary_value(row: &Row) -> Value {
    let auto_count = row.get::<_, i64>("auto_count");
    json!({
        "name": row.get::<_, String>("name"),
        "normalized": row.get::<_, String>("normalized"),
        "color": row.get::<_, Option<String>>("color"),
        "image_count": row.get::<_, i64>("image_count"),
        "auto_count": auto_count,
        "user_count": row.get::<_, i64>("user_count"),
        "user_defined": row.get::<_, bool>("user_defined"),
        "is_auto": auto_count > 0,
    })
}

pub async fn tag_summary_by_norm(client: &Client, norm: &str) -> Result<Option<Value>, ApiError> {
    Ok(tag_summary_rows(client)
        .await?
        .into_iter()
        .find(|row| row.get("normalized").and_then(Value::as_str) == Some(norm)))
}

async fn ensure_tag(
    client: &Client,
    name: &str,
    user_defined: bool,
) -> Result<Option<i64>, ApiError> {
    let cleaned = name.split_whitespace().collect::<Vec<_>>().join(" ");
    let norm = normalize_tag(&cleaned);
    if cleaned.is_empty() || norm.is_empty() {
        return Ok(None);
    }
    if user_defined {
        client
            .execute(
                "DELETE FROM suppressed_auto_tags WHERE normalized = $1",
                &[&norm],
            )
            .await?;
    }
    let row = client
        .query_opt(
            r#"
            INSERT INTO tags (name, normalized, user_defined)
            VALUES ($1, $2, $3)
            ON CONFLICT (normalized) DO UPDATE SET
                name = CASE WHEN EXCLUDED.user_defined THEN EXCLUDED.name ELSE tags.name END,
                user_defined = tags.user_defined OR EXCLUDED.user_defined
            RETURNING id
            "#,
            &[&cleaned, &norm, &user_defined],
        )
        .await?;
    Ok(row.map(|row| row.get::<_, i64>("id")))
}

pub async fn create_user_tag_entry(client: &Client, name: &str) -> Result<Value, ApiError> {
    let cleaned = clean_tag_list(&[name.to_string()]);
    let Some(name) = cleaned.first() else {
        return Err(ApiError::bad_request("Tag name is empty"));
    };
    ensure_tag(client, name, true).await?;
    Ok(tag_summary_by_norm(client, &normalize_tag(name))
        .await?
        .unwrap_or_else(|| {
            json!({
                "name": name,
                "color": null,
                "image_count": 0,
                "auto_count": 0,
                "user_count": 0,
                "user_defined": true,
                "is_auto": false,
            })
        }))
}

pub async fn update_tag_definition(
    client: &Client,
    tag: &str,
    payload: &Value,
) -> Result<Value, ApiError> {
    let Some(object) = payload.as_object() else {
        return Err(ApiError::bad_request("Request body must be an object"));
    };
    let has_name = object.contains_key("name");
    let has_color = object.contains_key("color");
    if !has_name && !has_color {
        let norm = normalize_tag(tag);
        return tag_summary_by_norm(client, &norm)
            .await?
            .ok_or_else(|| ApiError::not_found("Tag not found"));
    }

    let new_name = if has_name {
        let raw = object.get("name").and_then(Value::as_str).unwrap_or("");
        let cleaned = clean_tag_list(&[raw.to_string()]);
        if cleaned.is_empty() {
            return Err(ApiError::bad_request("Tag name is empty"));
        }
        Some(cleaned[0].clone())
    } else {
        None
    };
    let new_color = normalize_color(object.get("color"))?;
    let source_norm = normalize_tag(tag);
    let mut final_norm = source_norm.clone();

    let source = client
        .query_opt(
            r#"
            SELECT
                t.id,
                t.name,
                t.normalized,
                COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
            FROM tags t
            LEFT JOIN image_tags it ON it.tag_id = t.id
            WHERE t.normalized = $1
            GROUP BY t.id
            "#,
            &[&source_norm],
        )
        .await?;
    let Some(source) = source else {
        return Err(ApiError::not_found("Tag not found"));
    };
    let source_id = source.get::<_, i64>("id");
    let source_name = source.get::<_, String>("name");
    let source_normalized = source.get::<_, String>("normalized");
    let source_is_auto = source.get::<_, i64>("auto_count") > 0;
    let mut target_id = source_id;

    if let Some(new_name) = &new_name {
        let new_norm = normalize_tag(new_name);
        client
            .execute(
                "DELETE FROM suppressed_auto_tags WHERE normalized = $1",
                &[&new_norm],
            )
            .await?;
        if source_is_auto && new_name != &source_name {
            return Err(ApiError::bad_request("Folder tags cannot be renamed"));
        }
        if new_norm == source_normalized {
            if new_name != &source_name {
                client
                    .execute(
                        "UPDATE tags SET name = $1, user_defined = true WHERE id = $2",
                        &[new_name, &source_id],
                    )
                    .await?;
            } else if !source_is_auto {
                client
                    .execute(
                        "UPDATE tags SET user_defined = true WHERE id = $1",
                        &[&source_id],
                    )
                    .await?;
            }
            final_norm = new_norm;
        } else {
            if source_is_auto {
                return Err(ApiError::bad_request("Folder tags cannot be renamed"));
            }
            let target = client
                .query_opt(
                    r#"
                    SELECT
                        t.id,
                        t.name,
                        t.normalized,
                        COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                    FROM tags t
                    LEFT JOIN image_tags it ON it.tag_id = t.id
                    WHERE t.normalized = $1
                    GROUP BY t.id
                    "#,
                    &[&new_norm],
                )
                .await?;
            if let Some(target) = target {
                if target.get::<_, i64>("auto_count") > 0 {
                    return Err(ApiError::bad_request("Cannot merge into a folder tag"));
                }
                let existing_id = target.get::<_, i64>("id");
                client
                    .execute(
                        r#"
                    INSERT INTO image_tags (image_id, tag_id, kind, created_at)
                    SELECT image_id, $1, kind, created_at
                    FROM image_tags
                    WHERE tag_id = $2
                    ON CONFLICT DO NOTHING
                    "#,
                        &[&existing_id, &source_id],
                    )
                    .await?;
                client
                    .execute("DELETE FROM tags WHERE id = $1", &[&source_id])
                    .await?;
                client
                    .execute(
                        "UPDATE tags SET user_defined = true WHERE id = $1",
                        &[&existing_id],
                    )
                    .await?;
                target_id = existing_id;
                final_norm = target.get::<_, String>("normalized");
            } else {
                client
                    .execute(
                    "UPDATE tags SET name = $1, normalized = $2, user_defined = true WHERE id = $3",
                    &[new_name, &new_norm, &source_id],
                )
                .await?;
                target_id = source_id;
                final_norm = new_norm;
            }
        }
    }

    if has_color {
        client
            .execute(
                "UPDATE tags SET color = $1 WHERE id = $2",
                &[&new_color, &target_id],
            )
            .await?;
    }

    tag_summary_by_norm(client, &final_norm)
        .await?
        .ok_or_else(|| ApiError::not_found("Tag not found"))
}

pub async fn delete_tag_definition(client: &Client, tag: &str) -> Result<(), ApiError> {
    let norm = normalize_tag(tag);
    let row = client
        .query_opt(
            r#"
            SELECT
                t.id,
                t.name,
                t.normalized,
                COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
            FROM tags t
            LEFT JOIN image_tags it ON it.tag_id = t.id
            WHERE t.normalized = $1
            GROUP BY t.id
            "#,
            &[&norm],
        )
        .await?;
    let Some(row) = row else {
        return Err(ApiError::not_found("Tag not found"));
    };
    if row.get::<_, i64>("auto_count") > 0 {
        let normalized = row.get::<_, String>("normalized");
        let name = row.get::<_, String>("name");
        client
            .execute(
                r#"
            INSERT INTO suppressed_auto_tags (normalized, name)
            VALUES ($1, $2)
            ON CONFLICT (normalized) DO UPDATE SET name = EXCLUDED.name
            "#,
                &[&normalized, &name],
            )
            .await?;
    }
    let id = row.get::<_, i64>("id");
    client
        .execute("DELETE FROM tags WHERE id = $1", &[&id])
        .await?;
    Ok(())
}

pub async fn get_image_record(
    client: &Client,
    image_id: &str,
) -> Result<Option<ImageRecord>, ApiError> {
    let row = client
        .query_opt(
            r#"
            SELECT id, root_path, path, thumb, size, mtime, width, height, hidden
            FROM images
            WHERE id = $1 AND hidden = false
            "#,
            &[&image_id],
        )
        .await?;
    Ok(row.map(|row| ImageRecord {
        id: row.get("id"),
        root_path: row.get("root_path"),
        path: row.get("path"),
        thumb: row.get("thumb"),
        size: row.get("size"),
        mtime: row.get("mtime"),
        width: row.get("width"),
        height: row.get("height"),
    }))
}

pub async fn replace_image_user_tags(
    client: &Client,
    image_id: &str,
    tags: &[String],
) -> Result<(), ApiError> {
    let cleaned = clean_tag_list(tags);
    client
        .execute(
            "DELETE FROM image_tags WHERE image_id = $1 AND kind = 'user'",
            &[&image_id],
        )
        .await?;
    for tag in cleaned {
        let name = tag.split_whitespace().collect::<Vec<_>>().join(" ");
        let norm = normalize_tag(&name);
        if name.is_empty() || norm.is_empty() {
            continue;
        }
        client
            .execute(
                "DELETE FROM suppressed_auto_tags WHERE normalized = $1",
                &[&norm],
            )
            .await?;
        let row = client
            .query_opt(
                r#"
                INSERT INTO tags (name, normalized, user_defined)
                VALUES ($1, $2, true)
                ON CONFLICT (normalized) DO UPDATE SET
                    name = EXCLUDED.name,
                    user_defined = true
                RETURNING id
                "#,
                &[&name, &norm],
            )
            .await?;
        let Some(row) = row else {
            continue;
        };
        let tag_id = row.get::<_, i64>("id");
        client
            .execute(
                r#"
            INSERT INTO image_tags (image_id, tag_id, kind)
            VALUES ($1, $2, 'user')
            ON CONFLICT DO NOTHING
            "#,
                &[&image_id, &tag_id],
            )
            .await?;
    }
    Ok(())
}

pub async fn fetch_tags_for_image_ids(
    client: &Client,
    ids: &[String],
) -> Result<HashMap<String, (Vec<String>, Vec<String>)>, ApiError> {
    let mut map: HashMap<String, (Vec<String>, Vec<String>)> = ids
        .iter()
        .map(|id| (id.clone(), (Vec::new(), Vec::new())))
        .collect();
    if ids.is_empty() {
        return Ok(map);
    }
    let rows = client
        .query(
            r#"
            SELECT it.image_id, it.kind, t.name
            FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id = ANY($1)
            ORDER BY lower(t.name), t.name
            "#,
            &[&ids.to_vec()],
        )
        .await?;
    for row in rows {
        let image_id = row.get::<_, String>("image_id");
        let kind = row.get::<_, String>("kind");
        let name = row.get::<_, String>("name");
        let entry = map.entry(image_id).or_default();
        if kind == "auto" {
            entry.0.push(name);
        } else {
            entry.1.push(name);
        }
    }
    Ok(map)
}

pub async fn rows_to_images(client: &Client, rows: Vec<ImageRow>) -> Result<Vec<Value>, ApiError> {
    let ids = rows.iter().map(|row| row.id.clone()).collect::<Vec<_>>();
    let tags_by_image = fetch_tags_for_image_ids(client, &ids).await?;
    Ok(rows
        .into_iter()
        .map(|row| {
            let (auto_tags, user_tags) = tags_by_image.get(&row.id).cloned().unwrap_or_default();
            let mut combined = auto_tags.clone();
            combined.extend(user_tags.clone());
            let all_tags = clean_tag_list(&combined);
            let height = row.height.max(0);
            let width = row.width.max(0);
            let aspect_ratio = if height > 0 {
                width as f64 / height as f64
            } else {
                1.0
            };
            json!({
                "id": row.id,
                "path": row.path,
                "thumb": row.thumb,
                "thumb_url": format!("/thumb-file/{}.jpg", row.id),
                "size": row.size,
                "mtime": row.mtime,
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
                "tags": all_tags,
                "auto_tags": auto_tags,
                "folder_tags": auto_tags,
                "user_tags": user_tags,
            })
        })
        .collect())
}

pub async fn query_images_page(
    client: &Client,
    roots: &[String],
    query: ImagesQuery,
) -> Result<Value, ApiError> {
    if roots.is_empty() {
        return Err(ApiError::bad_request("No folder set"));
    }
    let limit = query
        .limit
        .unwrap_or(DEFAULT_PAGE_LIMIT)
        .clamp(1, MAX_PAGE_LIMIT);
    let sort_mode = query.sort.unwrap_or_else(|| "date_desc".to_string());
    if !IMAGE_SORTS.contains(&sort_mode.as_str()) {
        return Err(ApiError::bad_request(format!(
            "Unsupported sort. Allowed: {}",
            IMAGE_SORTS.join(", ")
        )));
    }

    let include_source = query.include_tags.as_deref().or(query.tags.as_deref());
    let mut include_list = parse_csv_tags(include_source);
    let mut exclude_list = parse_csv_tags(query.exclude_tags.as_deref());
    include_list.dedup();
    exclude_list.dedup();
    let resolved_mode = if query.include_tags.is_some() {
        query.match_mode.as_deref().unwrap_or("any")
    } else {
        query.mode.as_deref().unwrap_or("any")
    };
    let resolved_mode = if VALID_MATCH_MODES.contains(&resolved_mode) {
        resolved_mode
    } else {
        "any"
    };
    let cursor_payload = decode_cursor(query.cursor.as_deref());

    let mut params: Vec<Box<dyn ToSql + Sync + Send>> = Vec::new();
    let roots_param = push_param(&mut params, roots.to_vec());
    let mut filters = vec![
        format!("i.root_path = ANY({roots_param})"),
        "i.hidden = false".to_string(),
    ];
    if let Some((sql, mut values)) = cursor_filter(&sort_mode, cursor_payload) {
        let mut rendered = sql;
        for value in values.drain(..) {
            let placeholder = push_param(&mut params, value);
            rendered = rendered.replacen('?', &placeholder, 1);
        }
        filters.push(rendered);
    }

    let mut join_sql = String::new();
    let mut having_clauses = Vec::new();
    let mut having_values: Vec<ParamValue> = Vec::new();
    if !include_list.is_empty() || !exclude_list.is_empty() {
        join_sql =
            "LEFT JOIN image_tags it ON it.image_id = i.id LEFT JOIN tags t ON t.id = it.tag_id"
                .to_string();
    }
    if !include_list.is_empty() {
        if resolved_mode == "all" {
            having_clauses.push(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) = ?"
                    .to_string(),
            );
            having_values.push(ParamValue::StringVec(include_list.clone()));
            having_values.push(ParamValue::I64(include_list.len() as i64));
        } else {
            having_clauses.push(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) > 0"
                    .to_string(),
            );
            having_values.push(ParamValue::StringVec(include_list.clone()));
        }
    }
    if !exclude_list.is_empty() {
        having_clauses.push(
            "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) = 0".to_string(),
        );
        having_values.push(ParamValue::StringVec(exclude_list.clone()));
    }
    let mut rendered_having = Vec::new();
    for clause in having_clauses {
        let mut rendered = clause;
        while rendered.contains('?') {
            let value = having_values.remove(0);
            let placeholder = push_param(&mut params, value);
            rendered = rendered.replacen('?', &placeholder, 1);
        }
        rendered_having.push(rendered);
    }
    let having_sql = if rendered_having.is_empty() {
        String::new()
    } else {
        format!("HAVING {}", rendered_having.join(" AND "))
    };

    let where_sql = filters.join(" AND ");
    let order_sql = sort_order_sql(&sort_mode);
    let limit_param = push_param(&mut params, limit + 1);
    let sql = format!(
        r#"
        SELECT
            i.id,
            i.path,
            i.thumb,
            i.size,
            i.mtime,
            i.width,
            i.height,
            lower(i.path) AS lower_path
        FROM images i {join_sql}
        WHERE {where_sql}
        GROUP BY i.id
        {having_sql}
        ORDER BY {order_sql}
        LIMIT {limit_param}
        "#
    );
    let page_param_refs = param_refs(&params);
    let raw_rows = client.query(&sql, &page_param_refs).await?;

    let total = if query.include_total {
        let mut total_params: Vec<Box<dyn ToSql + Sync + Send>> = Vec::new();
        let total_roots = push_param(&mut total_params, roots.to_vec());
        let mut total_having_values = Vec::new();
        let mut total_having_clauses = Vec::new();
        if !include_list.is_empty() || !exclude_list.is_empty() {
            // same join as page query
        }
        if !include_list.is_empty() {
            if resolved_mode == "all" {
                total_having_clauses.push(
                    "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) = ?"
                        .to_string(),
                );
                total_having_values.push(ParamValue::StringVec(include_list.clone()));
                total_having_values.push(ParamValue::I64(include_list.len() as i64));
            } else {
                total_having_clauses.push(
                    "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) > 0"
                        .to_string(),
                );
                total_having_values.push(ParamValue::StringVec(include_list.clone()));
            }
        }
        if !exclude_list.is_empty() {
            total_having_clauses.push(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(?) THEN t.normalized END) = 0"
                    .to_string(),
            );
            total_having_values.push(ParamValue::StringVec(exclude_list.clone()));
        }
        let mut total_rendered_having = Vec::new();
        for clause in total_having_clauses {
            let mut rendered = clause;
            while rendered.contains('?') {
                let value = total_having_values.remove(0);
                let placeholder = push_param(&mut total_params, value);
                rendered = rendered.replacen('?', &placeholder, 1);
            }
            total_rendered_having.push(rendered);
        }
        let total_having_sql = if total_rendered_having.is_empty() {
            String::new()
        } else {
            format!("HAVING {}", total_rendered_having.join(" AND "))
        };
        let total_join = if !include_list.is_empty() || !exclude_list.is_empty() {
            "LEFT JOIN image_tags it ON it.image_id = i.id LEFT JOIN tags t ON t.id = it.tag_id"
        } else {
            ""
        };
        let total_sql = format!(
            r#"
            WITH filtered AS (
                SELECT i.id
                FROM images i {total_join}
                WHERE {base_filter_total}
                GROUP BY i.id
                {total_having_sql}
            )
            SELECT COUNT(*)::bigint AS total FROM filtered
            "#,
            base_filter_total = format!("i.root_path = ANY({total_roots}) AND i.hidden = false")
        );
        let refs = param_refs(&total_params);
        let row = client.query_one(&total_sql, &refs).await?;
        Some(row.get::<_, i64>("total"))
    } else {
        None
    };

    let has_more = raw_rows.len() as i64 > limit;
    let page_rows = raw_rows
        .into_iter()
        .take(limit as usize)
        .map(|row| ImageRow {
            id: row.get("id"),
            path: row.get("path"),
            thumb: row.get("thumb"),
            size: row.get("size"),
            mtime: row.get("mtime"),
            width: row.get("width"),
            height: row.get("height"),
            lower_path: row.get("lower_path"),
        })
        .collect::<Vec<_>>();
    let next_cursor = if has_more {
        page_rows.last().map(|row| encode_cursor(&sort_mode, row))
    } else {
        None
    };
    let rows_value = page_rows
        .iter()
        .map(|row| {
            json!({
                "id": row.id,
                "path": row.path,
                "thumb": row.thumb,
                "size": row.size,
                "mtime": row.mtime,
                "width": row.width,
                "height": row.height,
            })
        })
        .collect::<Vec<_>>();
    let items = rows_to_images(client, page_rows).await?;

    Ok(json!({
        "rows": rows_value,
        "items": items,
        "page": {
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
            "returned": items.len(),
            "total": total,
            "include_total": query.include_total,
            "sort": sort_mode,
        }
    }))
}

#[derive(Debug, Clone)]
enum ParamValue {
    String(String),
    StringVec(Vec<String>),
    I64(i64),
}

fn push_param<T>(params: &mut Vec<Box<dyn ToSql + Sync + Send>>, value: T) -> String
where
    T: ToSql + Sync + Send + 'static,
{
    params.push(Box::new(value));
    format!("${}", params.len())
}

fn param_refs(params: &[Box<dyn ToSql + Sync + Send>]) -> Vec<&(dyn ToSql + Sync)> {
    params
        .iter()
        .map(|value| value.as_ref() as &(dyn ToSql + Sync))
        .collect()
}

impl ToSql for ParamValue {
    fn to_sql(
        &self,
        ty: &tokio_postgres::types::Type,
        out: &mut bytes::BytesMut,
    ) -> Result<tokio_postgres::types::IsNull, Box<dyn std::error::Error + Sync + Send>>
    where
        Self: Sized,
    {
        match self {
            ParamValue::String(value) => value.to_sql(ty, out),
            ParamValue::StringVec(value) => value.to_sql(ty, out),
            ParamValue::I64(value) => value.to_sql(ty, out),
        }
    }

    fn accepts(_ty: &tokio_postgres::types::Type) -> bool
    where
        Self: Sized,
    {
        true
    }

    tokio_postgres::types::to_sql_checked!();
}

fn decode_cursor(raw: Option<&str>) -> Option<CursorPayload> {
    let raw = raw?;
    let bytes = URL_SAFE.decode(raw.as_bytes()).ok()?;
    let payload: Value = serde_json::from_slice(&bytes).ok()?;
    let id = payload.get("id")?.as_str()?.to_string();
    if id.is_empty() {
        return None;
    }
    Some(CursorPayload {
        sort: payload
            .get("sort")
            .and_then(Value::as_str)
            .unwrap_or("path_asc")
            .to_string(),
        lower_path: payload
            .get("lower_path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        path: payload
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        id,
        mtime: payload.get("mtime").and_then(Value::as_i64).unwrap_or(0),
        size: payload.get("size").and_then(Value::as_i64).unwrap_or(0),
    })
}

fn encode_cursor(sort_mode: &str, row: &ImageRow) -> String {
    let mut payload = json!({
        "sort": sort_mode,
        "lower_path": row.lower_path,
        "path": row.path,
        "id": row.id,
    });
    if sort_mode.starts_with("date_") {
        payload["mtime"] = json!(row.mtime);
    }
    if sort_mode.starts_with("size_") {
        payload["size"] = json!(row.size);
    }
    let raw = serde_json::to_string(&payload).unwrap_or_else(|_| "{}".to_string());
    URL_SAFE.encode(raw.as_bytes())
}

fn sort_order_sql(sort_mode: &str) -> &'static str {
    match sort_mode {
        "path_desc" => "lower_path DESC, path DESC, id DESC",
        "date_desc" => "mtime DESC, lower_path, path, id",
        "date_asc" => "mtime ASC, lower_path, path, id",
        "size_desc" => "size DESC, lower_path, path, id",
        "size_asc" => "size ASC, lower_path, path, id",
        _ => "lower_path, path, id",
    }
}

fn cursor_filter(
    sort_mode: &str,
    cursor: Option<CursorPayload>,
) -> Option<(String, Vec<ParamValue>)> {
    let cursor = cursor?;
    if cursor.sort != sort_mode || cursor.id.is_empty() {
        return None;
    }
    let lower_path = ParamValue::String(cursor.lower_path);
    let path = ParamValue::String(cursor.path);
    let id = ParamValue::String(cursor.id);
    match sort_mode {
        "path_desc" => Some((
            "(lower(i.path), i.path, i.id) < (?, ?, ?)".to_string(),
            vec![lower_path, path, id],
        )),
        "date_desc" => Some((
            "(i.mtime < ? OR (i.mtime = ? AND (lower(i.path), i.path, i.id) > (?, ?, ?)))"
                .to_string(),
            vec![
                ParamValue::I64(cursor.mtime),
                ParamValue::I64(cursor.mtime),
                lower_path,
                path,
                id,
            ],
        )),
        "date_asc" => Some((
            "(i.mtime > ? OR (i.mtime = ? AND (lower(i.path), i.path, i.id) > (?, ?, ?)))"
                .to_string(),
            vec![
                ParamValue::I64(cursor.mtime),
                ParamValue::I64(cursor.mtime),
                lower_path,
                path,
                id,
            ],
        )),
        "size_desc" => Some((
            "(i.size < ? OR (i.size = ? AND (lower(i.path), i.path, i.id) > (?, ?, ?)))"
                .to_string(),
            vec![
                ParamValue::I64(cursor.size),
                ParamValue::I64(cursor.size),
                lower_path,
                path,
                id,
            ],
        )),
        "size_asc" => Some((
            "(i.size > ? OR (i.size = ? AND (lower(i.path), i.path, i.id) > (?, ?, ?)))"
                .to_string(),
            vec![
                ParamValue::I64(cursor.size),
                ParamValue::I64(cursor.size),
                lower_path,
                path,
                id,
            ],
        )),
        _ => Some((
            "(lower(i.path), i.path, i.id) > (?, ?, ?)".to_string(),
            vec![lower_path, path, id],
        )),
    }
}

fn string_array(value: &Value) -> Option<Vec<String>> {
    value.as_array().map(|items| {
        items
            .iter()
            .filter_map(|item| item.as_str().map(|raw| raw.to_string()))
            .collect()
    })
}

pub async fn folder_tree_rows(client: &Client, roots: &[String]) -> Result<Vec<Value>, ApiError> {
    if roots.is_empty() {
        return Ok(Vec::new());
    }
    let rows = client
        .query(
            r#"
            SELECT root_path, path
            FROM images
            WHERE root_path = ANY($1)
              AND hidden = false
            ORDER BY root_path, lower(path), path
            "#,
            &[&roots.to_vec()],
        )
        .await?;
    Ok(rows
        .iter()
        .map(|row| {
            json!({
                "root_path": row.get::<_, String>("root_path"),
                "path": row.get::<_, String>("path"),
            })
        })
        .collect())
}

pub async fn enqueue_job(
    client: &Client,
    job_type: &str,
    payload: Value,
    priority: i32,
    max_attempts: i32,
    dedupe_key: Option<String>,
) -> Result<(Value, bool), ApiError> {
    if let Some(dedupe_key) = &dedupe_key {
        if let Some(existing) = client
            .query_opt(
                r#"
                SELECT *
                FROM jobs
                WHERE job_type = $1
                  AND dedupe_key = $2
                  AND state IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                "#,
                &[&job_type, dedupe_key],
            )
            .await?
        {
            return Ok((serialize_job_raw(&existing), true));
        }
    }

    let job_id = Uuid::new_v4().simple().to_string();
    let row = client
        .query_one(
            r#"
            INSERT INTO jobs (id, job_type, payload, state, priority, max_attempts, scheduled_at, dedupe_key)
            VALUES ($1, $2, $3, 'queued', $4, $5, now(), $6)
            RETURNING *
            "#,
            &[
                &job_id,
                &job_type,
                &payload,
                &priority,
                &max_attempts.max(1),
                &dedupe_key,
            ],
        )
        .await?;
    client
        .execute(
            r#"
            INSERT INTO job_events (job_id, event, data)
            VALUES ($1, 'enqueued', $2)
            "#,
            &[&job_id, &json!({"job_type": job_type})],
        )
        .await?;
    Ok((serialize_job_raw(&row), false))
}

pub async fn enqueue_rescan_job(
    client: &Client,
    root_path: &str,
    max_attempts: i32,
) -> Result<Value, ApiError> {
    let (job, _) = enqueue_job(
        client,
        "rescan",
        json!({ "root_path": root_path }),
        0,
        max_attempts,
        None,
    )
    .await?;
    Ok(job)
}

pub async fn running_rescan_job(client: &Client) -> Result<Option<Value>, ApiError> {
    let jobs = list_jobs(client, Some("rescan"), Some("running"), 1).await?;
    Ok(jobs.into_iter().next())
}

pub fn build_rescan_response(running_job: Option<Value>, queued_jobs: Vec<Value>) -> Value {
    if let Some(job) = running_job {
        return json!({
            "ok": false,
            "message": "Scan already running",
            "job_id": job.get("id").cloned().unwrap_or(Value::Null),
        });
    }
    let ids = queued_jobs
        .iter()
        .filter_map(|job| job.get("id").cloned())
        .collect::<Vec<_>>();
    json!({
        "ok": true,
        "job_id": ids.first().cloned().unwrap_or(Value::Null),
        "job_ids": ids,
    })
}

pub async fn get_job(client: &Client, job_id: &str) -> Result<Option<Value>, ApiError> {
    let row = client
        .query_opt("SELECT * FROM jobs WHERE id = $1", &[&job_id])
        .await?;
    Ok(row.map(|row| serialize_job_raw(&row)))
}

pub async fn list_jobs(
    client: &Client,
    job_type: Option<&str>,
    state: Option<&str>,
    limit: i64,
) -> Result<Vec<Value>, ApiError> {
    if let Some(state) = state {
        if !VALID_JOB_STATES.contains(&state) {
            return Ok(Vec::new());
        }
    }
    let capped_limit = limit.clamp(1, 200);
    let rows = match (job_type, state) {
        (Some(job_type), Some(state)) => {
            client
                .query(
                    "SELECT * FROM jobs WHERE job_type = $1 AND state = $2 ORDER BY created_at DESC LIMIT $3",
                    &[&job_type, &state, &capped_limit],
                )
                .await?
        }
        (Some(job_type), None) => {
            client
                .query(
                    "SELECT * FROM jobs WHERE job_type = $1 ORDER BY created_at DESC LIMIT $2",
                    &[&job_type, &capped_limit],
                )
                .await?
        }
        (None, Some(state)) => {
            client
                .query(
                    "SELECT * FROM jobs WHERE state = $1 ORDER BY created_at DESC LIMIT $2",
                    &[&state, &capped_limit],
                )
                .await?
        }
        (None, None) => {
            client
                .query(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT $1",
                    &[&capped_limit],
                )
                .await?
        }
    };
    Ok(rows.iter().map(serialize_job_raw).collect())
}

pub async fn count_jobs(
    client: &Client,
    job_type: Option<&str>,
    state: Option<&str>,
) -> Result<i64, ApiError> {
    if let Some(state) = state {
        if !VALID_JOB_STATES.contains(&state) {
            return Ok(0);
        }
    }
    let row =
        match (job_type, state) {
            (Some(job_type), Some(state)) => client
                .query_one(
                    "SELECT COUNT(*)::bigint AS total FROM jobs WHERE job_type = $1 AND state = $2",
                    &[&job_type, &state],
                )
                .await?,
            (Some(job_type), None) => {
                client
                    .query_one(
                        "SELECT COUNT(*)::bigint AS total FROM jobs WHERE job_type = $1",
                        &[&job_type],
                    )
                    .await?
            }
            (None, Some(state)) => {
                client
                    .query_one(
                        "SELECT COUNT(*)::bigint AS total FROM jobs WHERE state = $1",
                        &[&state],
                    )
                    .await?
            }
            (None, None) => {
                client
                    .query_one("SELECT COUNT(*)::bigint AS total FROM jobs", &[])
                    .await?
            }
        };
    Ok(row.get("total"))
}

pub async fn count_stale_running_jobs(
    client: &Client,
    job_type: Option<&str>,
    stale_after_sec: i64,
) -> Result<i64, ApiError> {
    if stale_after_sec <= 0 {
        return Ok(0);
    }
    let row = if let Some(job_type) = job_type {
        client
            .query_one(
                r#"
                SELECT COUNT(*)::bigint AS total
                FROM jobs
                WHERE state = 'running'
                  AND GREATEST(
                      COALESCE(started_at, created_at, '-infinity'::timestamptz),
                      COALESCE(updated_at, started_at, created_at, '-infinity'::timestamptz)
                  ) <= now() - ($1::bigint * INTERVAL '1 second')
                  AND job_type = $2
                "#,
                &[&stale_after_sec, &job_type],
            )
            .await?
    } else {
        client
            .query_one(
                r#"
                SELECT COUNT(*)::bigint AS total
                FROM jobs
                WHERE state = 'running'
                  AND GREATEST(
                      COALESCE(started_at, created_at, '-infinity'::timestamptz),
                      COALESCE(updated_at, started_at, created_at, '-infinity'::timestamptz)
                  ) <= now() - ($1::bigint * INTERVAL '1 second')
                "#,
                &[&stale_after_sec],
            )
            .await?
    };
    Ok(row.get("total"))
}

pub fn serialize_job_raw(row: &Row) -> Value {
    let payload = row
        .try_get::<_, Value>("payload")
        .unwrap_or_else(|_| json!({}));
    json!({
        "id": row.get::<_, String>("id"),
        "type": row.get::<_, String>("job_type"),
        "state": row.get::<_, String>("state"),
        "priority": row.get::<_, i32>("priority"),
        "attempt": row.get::<_, i32>("attempt"),
        "max_attempts": row.get::<_, i32>("max_attempts"),
        "progress": {
            "done": row.get::<_, i32>("progress_done"),
            "total": row.get::<_, i32>("progress_total"),
        },
        "error": row.get::<_, Option<String>>("error"),
        "worker_id": row.get::<_, Option<String>>("worker_id"),
        "payload": payload,
        "scheduled_at": datetime_value(row, "scheduled_at"),
        "started_at": datetime_value(row, "started_at"),
        "finished_at": datetime_value(row, "finished_at"),
        "created_at": datetime_value(row, "created_at"),
        "updated_at": datetime_value(row, "updated_at"),
    })
}

fn datetime_value(row: &Row, name: &str) -> Value {
    row.try_get::<_, Option<DateTime<Utc>>>(name)
        .ok()
        .flatten()
        .map(|value| json!(value))
        .unwrap_or(Value::Null)
}

pub async fn enqueue_thumb_job(
    client: &Client,
    image_id: &str,
    root_path: &str,
    path: &str,
    thumb: &str,
    mtime: i64,
    priority: i32,
    max_attempts: i32,
) -> Result<(Value, bool), ApiError> {
    let payload = json!({
        "image_id": image_id,
        "root_path": root_path,
        "path": path,
        "thumb": thumb,
        "mtime": mtime,
        "max_size": [640, 640],
    });
    enqueue_job(
        client,
        "thumb",
        payload,
        priority,
        max_attempts,
        Some(format!("thumb:{image_id}:{mtime}")),
    )
    .await
}

pub async fn enqueue_thumb_rebuild_jobs(
    client: &Client,
    roots: &[String],
    input: ThumbRebuildInput,
    max_attempts: i32,
) -> Result<ThumbRebuildResult, ApiError> {
    if roots.is_empty() {
        return Ok(ThumbRebuildResult {
            enqueued: 0,
            queued_existing: 0,
            skipped: 0,
            total: 0,
        });
    }
    let max_rows = input.limit.unwrap_or(0).clamp(0, 200000);
    let sql = if max_rows > 0 {
        r#"
        SELECT id, root_path, path, thumb, mtime
        FROM images
        WHERE root_path = ANY($1) AND hidden = false
        ORDER BY root_path, lower(path), path
        LIMIT $2
        "#
    } else {
        r#"
        SELECT id, root_path, path, thumb, mtime
        FROM images
        WHERE root_path = ANY($1) AND hidden = false
        ORDER BY root_path, lower(path), path
        "#
    };
    let rows = if max_rows > 0 {
        client.query(sql, &[&roots.to_vec(), &max_rows]).await?
    } else {
        client.query(sql, &[&roots.to_vec()]).await?
    };
    let mut enqueued = 0;
    let mut queued_existing = 0;
    let mut skipped = 0;
    for row in &rows {
        let image_id = row.get::<_, String>("id");
        let root_path = row.get::<_, String>("root_path");
        let path = row.get::<_, String>("path");
        let thumb = row.get::<_, String>("thumb");
        let mtime = row.get::<_, i64>("mtime");
        let src = Path::new(&root_path).join(&path);
        let thumb_path = Path::new(&root_path).join(&thumb);
        if input.stale_only {
            if !src.exists() {
                skipped += 1;
                continue;
            }
            if let Ok(metadata) = fs::metadata(&thumb_path) {
                if let Ok(modified) = metadata.modified() {
                    if let Ok(duration) = modified.duration_since(std::time::UNIX_EPOCH) {
                        if duration.as_secs() as i64 >= mtime {
                            skipped += 1;
                            continue;
                        }
                    }
                }
            }
        }
        let (_, deduped) = enqueue_thumb_job(
            client,
            &image_id,
            &root_path,
            &path,
            &thumb,
            mtime,
            20,
            max_attempts,
        )
        .await?;
        if deduped {
            queued_existing += 1;
        }
        enqueued += 1;
    }
    Ok(ThumbRebuildResult {
        enqueued,
        queued_existing,
        skipped,
        total: rows.len() as i64,
    })
}

pub async fn status_payload(state: &AppState, client: &Client) -> Result<Value, ApiError> {
    let health = state.db_health().await;
    let session = load_session(client).await?;
    let roots = roots_from_session(&session);
    let root = active_root(&session);
    let scan = rescan_status(client).await?;

    let thumb_running = count_jobs(client, Some("thumb"), Some("running")).await?;
    let thumb_queued = count_jobs(client, Some("thumb"), Some("queued")).await?;
    let thumb_stale_running =
        count_stale_running_jobs(client, Some("thumb"), state.config.job_stale_running_sec).await?;
    let rescan_running = count_jobs(client, Some("rescan"), Some("running")).await?;
    let rescan_queued = count_jobs(client, Some("rescan"), Some("queued")).await?;
    let rescan_stale_running =
        count_stale_running_jobs(client, Some("rescan"), state.config.job_stale_running_sec)
            .await?;
    let stale_running =
        count_stale_running_jobs(client, None, state.config.job_stale_running_sec).await?;
    let degraded = state.config.thumb_worker_expected
        && state.config.thumb_job_mode == "queue"
        && thumb_running == 0
        && thumb_queued > 0;

    Ok(json!({
        "ready": root.is_some() && !scan.get("running").and_then(Value::as_bool).unwrap_or(false) && !scan.get("queued").and_then(Value::as_bool).unwrap_or(false),
        "root": root,
        "root_paths": roots,
        "running": scan["running"],
        "queued": scan["queued"],
        "job_id": scan["job_id"],
        "total": scan["total"],
        "done": scan["done"],
        "error": scan["error"],
        "db_ready": health["db_ready"],
        "db_error": health["db_error"],
        "workers": {
            "rescan_worker_expected": state.config.rescan_worker_expected,
            "thumb_worker_expected": state.config.thumb_worker_expected,
            "capabilities": {
                "thumb": {
                    "mode": state.config.thumb_job_mode,
                    "rust_supported": state.thumb_rust_supported(),
                    "python_fallback": state.config.thumb_sync_fallback,
                },
                "rescan": {
                    "mode": if state.config.rust_scanner { "rust" } else { "python" },
                    "rust_supported": state.scanner_rust_supported(),
                    "inline_worker": state.config.inline_worker,
                },
                "metadata": {
                    "mode": if !state.config.metadata_worker {
                        "not_enabled"
                    } else if state.config.metadata_authoritative {
                        "authoritative"
                    } else {
                        "shadow"
                    },
                    "rust_supported": state.metadata_rust_supported(),
                    "authoritative": state.config.metadata_worker && state.config.metadata_authoritative,
                },
                "hash": {
                    "mode": "not_enabled",
                    "rust_supported": false,
                }
            }
        },
        "queues": {
            "rescan_queue_depth": rescan_queued,
            "rescan_running": rescan_running,
            "thumb_queue_depth": thumb_queued,
            "thumb_running": thumb_running,
            "thumb_stale_running": thumb_stale_running,
            "thumb_mode": state.config.thumb_job_mode,
            "rescan_stale_running": rescan_stale_running,
            "stale_running": stale_running,
            "degraded": degraded,
        }
    }))
}

async fn rescan_status(client: &Client) -> Result<Value, ApiError> {
    if let Some(current) = list_jobs(client, Some("rescan"), Some("running"), 1)
        .await?
        .into_iter()
        .next()
    {
        return Ok(json!({
            "running": true,
            "queued": false,
            "job_id": current["id"],
            "total": current["progress"]["total"],
            "done": current["progress"]["done"],
            "error": null,
        }));
    }
    if let Some(current) = list_jobs(client, Some("rescan"), Some("queued"), 1)
        .await?
        .into_iter()
        .next()
    {
        return Ok(json!({
            "running": false,
            "queued": true,
            "job_id": current["id"],
            "total": current["progress"]["total"],
            "done": current["progress"]["done"],
            "error": null,
        }));
    }
    if let Some(current) = list_jobs(client, Some("rescan"), None, 1)
        .await?
        .into_iter()
        .next()
    {
        return Ok(json!({
            "running": false,
            "queued": false,
            "job_id": current["id"],
            "total": current["progress"]["total"],
            "done": current["progress"]["done"],
            "error": current["error"],
        }));
    }
    Ok(json!({
        "running": false,
        "queued": false,
        "job_id": null,
        "total": 0,
        "done": 0,
        "error": null,
    }))
}

pub fn thumb_path_for_id(root: &str, image_id: &str) -> PathBuf {
    Path::new(root)
        .join(INDEX_DIR_NAME)
        .join(THUMBS_DIR_NAME)
        .join(format!("{image_id}.jpg"))
}

pub fn image_thumb_path(image: &ImageRecord) -> PathBuf {
    Path::new(&image.root_path).join(&image.thumb)
}

pub fn image_file_path(image: &ImageRecord) -> PathBuf {
    Path::new(&image.root_path).join(&image.path)
}

pub fn validate_image_id(value: &str) -> bool {
    let len = value.len();
    (6..=64).contains(&len)
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
