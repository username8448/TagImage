use image::image_dimensions;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tagimage_core::{parse_u64_env, RescanJobPayload, ScannerShadowJobPayload};
use tagimage_db::{
    claim_next_rescan_job, claim_next_scanner_shadow_job, mark_rescan_failed,
    mark_rescan_succeeded, mark_scanner_shadow_failed, mark_scanner_shadow_succeeded,
    touch_job_progress,
};
use tokio::time::sleep;
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

const INDEX_DIR_NAME: &str = ".imgindex";
const THUMBS_DIR_NAME: &str = "thumbs";
const SCANNER_POLL_MS_DEFAULT: u64 = 750;
const SCANNER_MAX_BACKOFF_SEC: i64 = 120;
const THUMB_PRIORITY: i32 = 20;
const THUMB_MAX_SIZE: [u32; 2] = [640, 640];

#[derive(Debug, Clone)]
struct ExistingImage {
    id: String,
    path: String,
    hidden: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WorkerMode {
    Shadow,
    Authoritative,
}

#[derive(Debug, Clone)]
struct ScannedImage {
    rel: String,
    ext: String,
    source_bytes: i64,
    mtime: i64,
    width: i32,
    height: i32,
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => {
            let normalized = value.trim().to_ascii_lowercase();
            matches!(normalized.as_str(), "1" | "true" | "yes" | "y" | "on")
        }
        Err(_) => default,
    }
}

fn parse_poll_ms() -> u64 {
    let parsed = parse_u64_env("IMGVIEWER_SCANNER_POLL_MS", SCANNER_POLL_MS_DEFAULT);
    if parsed == 0 {
        SCANNER_POLL_MS_DEFAULT
    } else {
        parsed
    }
}

fn parse_i32_env(name: &str, default: i32, min_value: i32) -> i32 {
    let parsed = std::env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<i32>().ok())
        .unwrap_or(default);
    parsed.max(min_value)
}

fn worker_mode() -> WorkerMode {
    if env_bool("IMGVIEWER_RUST_SCANNER", false) {
        WorkerMode::Authoritative
    } else {
        WorkerMode::Shadow
    }
}

fn thumb_queue_enabled() -> bool {
    std::env::var("IMGVIEWER_THUMB_JOB_MODE")
        .unwrap_or_else(|_| "sync".to_string())
        .trim()
        .eq_ignore_ascii_case("queue")
}

fn uuid_hex() -> String {
    Uuid::new_v4().simple().to_string()
}

fn new_image_id() -> String {
    uuid_hex().chars().take(12).collect()
}

fn supported_ext(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(|value| value.to_str())
            .map(|value| value.to_ascii_lowercase())
            .as_deref(),
        Some("jpg" | "jpeg" | "png" | "webp")
    )
}

fn ext_lower(path: &Path) -> String {
    path.extension()
        .and_then(|value| value.to_str())
        .map(|value| value.to_ascii_lowercase())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}

fn path_to_rel(root: &Path, path: &Path) -> Result<String, String> {
    path.strip_prefix(root)
        .map_err(|e| format!("strip root {} from {}: {e}", root.display(), path.display()))
        .map(|value| value.to_string_lossy().to_string())
}

fn sort_like_python_scan(root: &Path, paths: &mut [PathBuf]) {
    paths.sort_by(|left, right| {
        let left_key = path_to_rel(root, left).unwrap_or_default().to_lowercase();
        let right_key = path_to_rel(root, right).unwrap_or_default().to_lowercase();
        left_key.cmp(&right_key)
    });
}

fn sort_rel_paths(paths: &mut [String]) {
    paths.sort_by(|left, right| {
        left.to_lowercase()
            .cmp(&right.to_lowercase())
            .then_with(|| left.cmp(right))
    });
}

fn scan_dir(current: &Path, out: &mut Vec<PathBuf>) -> Result<(), String> {
    let entries =
        fs::read_dir(current).map_err(|e| format!("read dir {}: {e}", current.display()))?;

    for entry_result in entries {
        let entry =
            entry_result.map_err(|e| format!("read dir entry {}: {e}", current.display()))?;
        let path = entry.path();
        let file_type = entry
            .file_type()
            .map_err(|e| format!("read file type {}: {e}", path.display()))?;

        if file_type.is_dir() {
            if entry.file_name().to_string_lossy() == INDEX_DIR_NAME {
                continue;
            }
            scan_dir(&path, out)?;
        } else if file_type.is_file() && supported_ext(&path) {
            out.push(path);
        }
    }

    Ok(())
}

fn scan_image_paths(root: &Path) -> Result<Vec<PathBuf>, String> {
    if !root.is_dir() {
        return Err(format!("root is not a directory: {}", root.display()));
    }
    let mut images = Vec::new();
    scan_dir(root, &mut images)?;
    sort_like_python_scan(root, &mut images);
    Ok(images)
}

fn file_mtime(meta: &fs::Metadata, path: &Path) -> Result<i64, String> {
    let modified = meta
        .modified()
        .map_err(|e| format!("read mtime {}: {e}", path.display()))?;
    modified
        .duration_since(UNIX_EPOCH)
        .map_err(|e| format!("mtime before unix epoch {}: {e}", path.display()))
        .map(|duration| duration.as_secs() as i64)
}

fn thumb_rel_for(existing_id: &str) -> String {
    format!("{INDEX_DIR_NAME}/{THUMBS_DIR_NAME}/{existing_id}.jpg")
}

fn should_regenerate_thumb(src_mtime: i64, thumb_path: &Path) -> bool {
    let Ok(meta) = fs::metadata(thumb_path) else {
        return true;
    };
    let Ok(thumb_mtime) = file_mtime(&meta, thumb_path) else {
        return true;
    };
    thumb_mtime < src_mtime
}

fn le_u16(bytes: &[u8]) -> Option<u16> {
    if bytes.len() < 2 {
        return None;
    }
    Some(u16::from_le_bytes([bytes[0], bytes[1]]))
}

fn le_u24(bytes: &[u8]) -> Option<u32> {
    if bytes.len() < 3 {
        return None;
    }
    Some(u32::from(bytes[0]) | (u32::from(bytes[1]) << 8) | (u32::from(bytes[2]) << 16))
}

fn le_u32(bytes: &[u8]) -> Option<u32> {
    if bytes.len() < 4 {
        return None;
    }
    Some(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
}

fn webp_dimensions(bytes: &[u8]) -> Option<(u32, u32)> {
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WEBP" {
        return None;
    }

    let mut offset = 12usize;
    while offset + 8 <= bytes.len() {
        let fourcc = &bytes[offset..offset + 4];
        let chunk_size = le_u32(&bytes[offset + 4..offset + 8])? as usize;
        let data_start = offset + 8;
        let data_end = data_start.checked_add(chunk_size)?;
        if data_end > bytes.len() {
            return None;
        }
        let data = &bytes[data_start..data_end];

        match fourcc {
            b"VP8X" if data.len() >= 10 => {
                let width = le_u24(&data[4..7])? + 1;
                let height = le_u24(&data[7..10])? + 1;
                return Some((width, height));
            }
            b"VP8L" if data.len() >= 5 && data[0] == 0x2f => {
                let packed = le_u32(&data[1..5])?;
                let width = (packed & 0x3fff) + 1;
                let height = ((packed >> 14) & 0x3fff) + 1;
                return Some((width, height));
            }
            b"VP8 " if data.len() >= 10 && data[3..6] == [0x9d, 0x01, 0x2a] => {
                let width = u32::from(le_u16(&data[6..8])? & 0x3fff);
                let height = u32::from(le_u16(&data[8..10])? & 0x3fff);
                return Some((width, height));
            }
            _ => {}
        }

        offset = data_end + (chunk_size % 2);
    }

    None
}

fn image_dimensions_like_python(path: &Path) -> (u32, u32) {
    if let Ok(dimensions) = image_dimensions(path) {
        return dimensions;
    }

    fs::read(path)
        .ok()
        .and_then(|bytes| webp_dimensions(&bytes))
        .unwrap_or((0, 0))
}

fn build_scanned_image(root: &Path, image_path: &Path) -> Result<ScannedImage, String> {
    let rel = path_to_rel(root, image_path)?;
    let meta = fs::metadata(image_path)
        .map_err(|e| format!("read metadata {}: {e}", image_path.display()))?;
    let (width, height) = image_dimensions_like_python(image_path);
    Ok(ScannedImage {
        rel,
        ext: ext_lower(image_path),
        source_bytes: meta.len().min(i64::MAX as u64) as i64,
        mtime: file_mtime(&meta, image_path)?,
        width: width.min(i32::MAX as u32) as i32,
        height: height.min(i32::MAX as u32) as i32,
    })
}

async fn load_existing_images(
    client: &Client,
    root_path: &str,
) -> Result<HashMap<String, ExistingImage>, String> {
    let rows = client
        .query(
            "SELECT id, path, hidden FROM images WHERE root_path = $1",
            &[&root_path],
        )
        .await
        .map_err(|e| format!("query existing images: {e}"))?;

    let mut out = HashMap::new();
    for row in rows {
        let path: String = row.get("path");
        out.insert(
            path.clone(),
            ExistingImage {
                id: row.get("id"),
                path,
                hidden: row.get("hidden"),
            },
        );
    }
    Ok(out)
}

async fn load_existing_image_ids(
    client: &Client,
    root_path: &str,
) -> Result<HashMap<String, String>, String> {
    let rows = client
        .query(
            "SELECT path, id FROM images WHERE root_path = $1",
            &[&root_path],
        )
        .await
        .map_err(|e| format!("query existing image ids: {e}"))?;

    let mut out = HashMap::new();
    for row in rows {
        let path: String = row.get("path");
        let id: String = row.get("id");
        out.insert(path, id);
    }
    Ok(out)
}

async fn mark_images_hidden_for_root(client: &Client, root_path: &str) -> Result<(), String> {
    client
        .execute(
            "UPDATE images SET hidden = true, updated_at = now() WHERE root_path = $1",
            &[&root_path],
        )
        .await
        .map_err(|e| format!("mark images hidden: {e}"))?;
    Ok(())
}

async fn upsert_image_row(
    client: &Client,
    root_path: &str,
    image: &ScannedImage,
    image_id: &str,
    thumb_rel: &str,
) -> Result<String, String> {
    let row = client
        .query_one(
            r#"
            INSERT INTO images (
                id, root_path, path, thumb, size, mtime, width, height, hidden
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, false)
            ON CONFLICT (root_path, path) DO UPDATE SET
                thumb = EXCLUDED.thumb,
                size = EXCLUDED.size,
                mtime = EXCLUDED.mtime,
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                hidden = false,
                updated_at = now()
            RETURNING id
            "#,
            &[
                &image_id,
                &root_path,
                &image.rel,
                &thumb_rel,
                &image.source_bytes,
                &image.mtime,
                &image.width,
                &image.height,
            ],
        )
        .await
        .map_err(|e| format!("upsert image {}: {e}", image.rel))?;
    Ok(row.get("id"))
}

async fn clear_auto_tags_for_image(client: &Client, image_id: &str) -> Result<(), String> {
    client
        .execute(
            "DELETE FROM image_tags WHERE image_id = $1 AND kind = 'auto'",
            &[&image_id],
        )
        .await
        .map_err(|e| format!("clear auto tags for image {image_id}: {e}"))?;
    Ok(())
}

async fn cleanup_hidden_image_tag_data(client: &Client) -> Result<(), String> {
    client
        .execute(
            r#"
            DELETE FROM image_tags it
            USING images i
            WHERE it.image_id = i.id
              AND i.hidden = true
            "#,
            &[],
        )
        .await
        .map_err(|e| format!("cleanup hidden image tags: {e}"))?;
    client
        .execute(
            r#"
            DELETE FROM tags t
            WHERE t.user_defined = false
              AND NOT EXISTS (
                  SELECT 1
                  FROM image_tags it
                  WHERE it.tag_id = t.id
              )
            "#,
            &[],
        )
        .await
        .map_err(|e| format!("cleanup orphan auto tags: {e}"))?;
    Ok(())
}

async fn enqueue_thumb_job(
    client: &mut Client,
    image_id: &str,
    root_path: &str,
    image: &ScannedImage,
    thumb_rel: &str,
) -> Result<bool, String> {
    let dedupe_key = format!("thumb:{}:{}", image_id, image.mtime);
    let max_attempts = parse_i32_env("IMGVIEWER_THUMB_MAX_ATTEMPTS", 5, 1);
    let job_id = uuid_hex();
    let payload = json!({
        "image_id": image_id,
        "root_path": root_path,
        "path": image.rel,
        "thumb": thumb_rel,
        "mtime": image.mtime,
        "max_size": THUMB_MAX_SIZE,
    });

    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin thumb enqueue tx: {e}"))?;

    let existing = tx
        .query_opt(
            r#"
            SELECT id
            FROM jobs
            WHERE job_type = 'thumb'
              AND dedupe_key = $1
              AND state IN ('queued', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            "#,
            &[&dedupe_key],
        )
        .await
        .map_err(|e| format!("check thumb dedupe: {e}"))?;
    if existing.is_some() {
        tx.commit()
            .await
            .map_err(|e| format!("commit deduped thumb enqueue: {e}"))?;
        return Ok(false);
    }

    let inserted = tx
        .execute(
            r#"
            INSERT INTO jobs (id, job_type, payload, state, priority, max_attempts, scheduled_at, dedupe_key)
            VALUES ($1, 'thumb', $2, 'queued', $3, $4, now(), $5)
            "#,
            &[&job_id, &payload, &THUMB_PRIORITY, &max_attempts, &dedupe_key],
        )
        .await;

    if let Err(err) = inserted {
        tx.rollback()
            .await
            .map_err(|e| format!("rollback failed thumb enqueue: {e}; insert error: {err}"))?;
        let existing_after_race = client
            .query_opt(
                r#"
                SELECT id
                FROM jobs
                WHERE job_type = 'thumb'
                  AND dedupe_key = $1
                  AND state IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                "#,
                &[&dedupe_key],
            )
            .await
            .map_err(|e| format!("check thumb dedupe after race: {e}"))?;
        if existing_after_race.is_some() {
            return Ok(false);
        }
        return Err(format!("insert thumb job: {err}"));
    }

    let event_data = json!({"job_type": "thumb"});
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'enqueued', $2)",
        &[&job_id, &event_data],
    )
    .await
    .map_err(|e| format!("insert thumb enqueued event: {e}"))?;
    tx.commit()
        .await
        .map_err(|e| format!("commit thumb enqueue: {e}"))?;
    Ok(true)
}

async fn process_authoritative_image(
    client: &mut Client,
    root: &Path,
    root_path: &str,
    image: &ScannedImage,
    existing_by_rel: &HashMap<String, String>,
    queue_thumbs: bool,
) -> Result<(), String> {
    let image_id = existing_by_rel
        .get(&image.rel)
        .cloned()
        .unwrap_or_else(new_image_id);
    let thumb_rel = thumb_rel_for(&image_id);
    let db_image_id = upsert_image_row(client, root_path, image, &image_id, &thumb_rel).await?;
    clear_auto_tags_for_image(client, &db_image_id).await?;

    if queue_thumbs && should_regenerate_thumb(image.mtime, &root.join(&thumb_rel)) {
        enqueue_thumb_job(client, &db_image_id, root_path, image, &thumb_rel).await?;
    }

    Ok(())
}

async fn build_shadow_scan(client: &Client, payload: Value) -> Result<(Value, i32), String> {
    let parsed: ScannerShadowJobPayload =
        serde_json::from_value(payload).map_err(|e| format!("invalid scanner payload: {e}"))?;
    let root = PathBuf::from(&parsed.root_path);
    let started = Instant::now();
    let existing_by_path = load_existing_images(client, &parsed.root_path).await?;
    let image_paths = scan_image_paths(&root)?;

    let mut scanned_paths = HashSet::new();
    let mut paths = Vec::new();
    let mut images = Vec::new();
    let mut thumb_job_candidates = Vec::new();

    for image_path in image_paths {
        let image = build_scanned_image(&root, &image_path)?;
        let existing = existing_by_path.get(&image.rel);
        let existing_id = existing.map(|item| item.id.clone());
        let thumb_rel = existing_id.as_deref().map(thumb_rel_for);
        let thumb_job_candidate = if let Some(thumb) = thumb_rel.as_deref() {
            should_regenerate_thumb(image.mtime, &root.join(thumb))
        } else {
            true
        };

        if thumb_job_candidate {
            thumb_job_candidates.push(image.rel.clone());
        }
        scanned_paths.insert(image.rel.clone());
        paths.push(image.rel.clone());
        images.push(json!({
            "path": image.rel,
            "ext": image.ext,
            "source_bytes": image.source_bytes,
            "mtime": image.mtime,
            "width": image.width,
            "height": image.height,
            "existing_id": existing_id,
            "is_new": existing.is_none(),
            "thumb": thumb_rel,
            "thumb_job_candidate": thumb_job_candidate,
        }));
    }

    let mut missing_candidates = Vec::new();
    let mut hidden_candidates = Vec::new();
    for existing in existing_by_path.values() {
        if scanned_paths.contains(&existing.path) {
            continue;
        }
        missing_candidates.push(existing.path.clone());
        if !existing.hidden {
            hidden_candidates.push(existing.path.clone());
        }
    }
    sort_rel_paths(&mut missing_candidates);
    sort_rel_paths(&mut hidden_candidates);
    sort_rel_paths(&mut thumb_job_candidates);

    let total = images.len().min(i32::MAX as usize) as i32;
    let scan_json = json!({
        "root_path": parsed.root_path,
        "total": images.len(),
        "paths": paths,
        "images": images,
        "missing_candidates": missing_candidates,
        "hidden_candidates": hidden_candidates,
        "thumb_job_candidates": thumb_job_candidates,
        "shadow": true,
        "duration_ms": started.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
    });

    Ok((scan_json, total))
}

async fn run_authoritative_scan(
    client: &mut Client,
    payload: Value,
    job_id: &str,
) -> Result<i32, String> {
    let parsed: RescanJobPayload =
        serde_json::from_value(payload).map_err(|e| format!("invalid rescan payload: {e}"))?;
    let root = PathBuf::from(&parsed.root_path);
    let image_paths = scan_image_paths(&root)?;
    let total = image_paths.len().min(i32::MAX as usize) as i32;
    let queue_thumbs = thumb_queue_enabled();

    touch_job_progress(client, job_id, 0, Some(total)).await?;
    mark_images_hidden_for_root(client, &parsed.root_path).await?;
    let existing_by_rel = load_existing_image_ids(client, &parsed.root_path).await?;

    let mut done = 0_i32;
    for image_path in image_paths {
        let image = build_scanned_image(&root, &image_path)?;
        process_authoritative_image(
            client,
            &root,
            &parsed.root_path,
            &image,
            &existing_by_rel,
            queue_thumbs,
        )
        .await?;

        done = done.saturating_add(1);
        if done % 20 == 0 || done == total {
            touch_job_progress(client, job_id, done, Some(total)).await?;
        }
    }

    cleanup_hidden_image_tag_data(client).await?;
    Ok(total)
}

async fn run_worker_loop(
    db_url: String,
    worker_id: String,
    mode: WorkerMode,
    poll_ms: u64,
    slow_ms: u128,
) -> Result<(), String> {
    let (mut client, connection) = tokio_postgres::connect(&db_url, NoTls)
        .await
        .map_err(|e| format!("connect postgres: {e}"))?;

    let connection_worker_id = worker_id.clone();
    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!(
                "[rust-scanner-worker] postgres connection error worker={}: {e}",
                connection_worker_id
            );
        }
    });

    loop {
        let claimed = match mode {
            WorkerMode::Shadow => claim_next_scanner_shadow_job(&mut client, &worker_id).await?,
            WorkerMode::Authoritative => claim_next_rescan_job(&mut client, &worker_id).await?,
        };
        let Some(job) = claimed else {
            sleep(Duration::from_millis(poll_ms)).await;
            continue;
        };

        let started_at = Instant::now();
        match mode {
            WorkerMode::Shadow => match build_shadow_scan(&client, job.payload.clone()).await {
                Ok((scan_json, total)) => {
                    let total_ms = started_at.elapsed().as_millis();
                    let root = scan_json
                        .get("root_path")
                        .and_then(|value| value.as_str())
                        .unwrap_or("unknown")
                        .to_string();
                    if let Err(e) =
                        mark_scanner_shadow_succeeded(&mut client, &job, scan_json, total).await
                    {
                        eprintln!(
                            "[rust-scanner-worker] mark success failed worker={} job={} error={}",
                            worker_id, job.id, e
                        );
                    }
                    eprintln!(
                        "[rust-scanner-worker] job_done worker={} job={} root={} total={} total_ms={}",
                        worker_id, job.id, root, total, total_ms
                    );
                    if total_ms >= slow_ms {
                        eprintln!(
                            "[rust-scanner-worker] slow_job worker={} job={} root={} total={} total_ms={}",
                            worker_id, job.id, root, total, total_ms
                        );
                    }
                }
                Err(err) => {
                    let total_ms = started_at.elapsed().as_millis();
                    eprintln!(
                        "[rust-scanner-worker] job_failed worker={} job={} total_ms={} error={}",
                        worker_id, job.id, total_ms, err
                    );
                    if let Err(e) = mark_scanner_shadow_failed(
                        &mut client,
                        &job,
                        &err,
                        Some(total_ms),
                        SCANNER_MAX_BACKOFF_SEC,
                    )
                    .await
                    {
                        eprintln!(
                            "[rust-scanner-worker] mark fail failed worker={} job={} error={}",
                            worker_id, job.id, e
                        );
                    }
                }
            },
            WorkerMode::Authoritative => {
                match run_authoritative_scan(&mut client, job.payload.clone(), &job.id).await {
                    Ok(total) => {
                        let total_ms = started_at.elapsed().as_millis();
                        if let Err(e) = mark_rescan_succeeded(&mut client, &job, total).await {
                            eprintln!(
                            "[rust-scanner-worker] mark rescan success failed worker={} job={} error={}",
                            worker_id, job.id, e
                        );
                        }
                        eprintln!(
                        "[rust-scanner-worker] rescan_done worker={} job={} total={} total_ms={}",
                        worker_id, job.id, total, total_ms
                    );
                        if total_ms >= slow_ms {
                            eprintln!(
                            "[rust-scanner-worker] slow_rescan worker={} job={} total={} total_ms={}",
                            worker_id, job.id, total, total_ms
                        );
                        }
                    }
                    Err(err) => {
                        let total_ms = started_at.elapsed().as_millis();
                        eprintln!(
                        "[rust-scanner-worker] rescan_failed worker={} job={} total_ms={} error={}",
                        worker_id, job.id, total_ms, err
                    );
                        if let Err(e) =
                            mark_rescan_failed(&mut client, &job, &err, SCANNER_MAX_BACKOFF_SEC)
                                .await
                        {
                            eprintln!(
                            "[rust-scanner-worker] mark rescan fail failed worker={} job={} error={}",
                            worker_id, job.id, e
                        );
                        }
                    }
                }
            }
        }
    }
}

async fn run() -> Result<(), String> {
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| {
        "postgresql://imgviewer:imgviewer@127.0.0.1:55432/imgviewer".to_string()
    });
    let mode = worker_mode();
    let worker_id = match mode {
        WorkerMode::Shadow => format!("rust-scanner-shadow-{}", now_unix()),
        WorkerMode::Authoritative => format!("rust-scanner-rescan-{}", now_unix()),
    };
    let poll_ms = parse_poll_ms();
    let slow_ms = parse_u64_env("IMGVIEWER_SCANNER_SLOW_MS", 2000) as u128;

    eprintln!(
        "[rust-scanner-worker] started as {} mode={:?}",
        worker_id, mode
    );
    run_worker_loop(db_url, worker_id, mode, poll_ms, slow_ms).await
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("[rust-scanner-worker] fatal: {e}");
        std::process::exit(1);
    }
}
