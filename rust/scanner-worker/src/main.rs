use image::image_dimensions;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tagimage_core::{parse_u64_env, ScannerShadowJobPayload};
use tagimage_db::{
    claim_next_scanner_shadow_job, mark_scanner_shadow_failed, mark_scanner_shadow_succeeded,
};
use tokio::time::sleep;
use tokio_postgres::{Client, NoTls};

const INDEX_DIR_NAME: &str = ".imgindex";
const THUMBS_DIR_NAME: &str = "thumbs";
const SCANNER_POLL_MS_DEFAULT: u64 = 750;
const SCANNER_MAX_BACKOFF_SEC: i64 = 120;

#[derive(Debug, Clone)]
struct ExistingImage {
    id: String,
    path: String,
    hidden: bool,
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn parse_poll_ms() -> u64 {
    let parsed = parse_u64_env("IMGVIEWER_SCANNER_POLL_MS", SCANNER_POLL_MS_DEFAULT);
    if parsed == 0 {
        SCANNER_POLL_MS_DEFAULT
    } else {
        parsed
    }
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
        let rel = path_to_rel(&root, &image_path)?;
        let meta = fs::metadata(&image_path)
            .map_err(|e| format!("read metadata {}: {e}", image_path.display()))?;
        let source_bytes = meta.len();
        let mtime = file_mtime(&meta, &image_path)?;
        let (width, height) = image_dimensions_like_python(&image_path);
        let ext = ext_lower(&image_path);
        let existing = existing_by_path.get(&rel);
        let existing_id = existing.map(|item| item.id.clone());
        let thumb_rel = existing_id.as_deref().map(thumb_rel_for);
        let thumb_job_candidate = if let Some(thumb) = thumb_rel.as_deref() {
            should_regenerate_thumb(mtime, &root.join(thumb))
        } else {
            true
        };

        if thumb_job_candidate {
            thumb_job_candidates.push(rel.clone());
        }
        scanned_paths.insert(rel.clone());
        paths.push(rel.clone());
        images.push(json!({
            "path": rel,
            "ext": ext,
            "source_bytes": source_bytes,
            "mtime": mtime,
            "width": width,
            "height": height,
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

async fn run_worker_loop(
    db_url: String,
    worker_id: String,
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
        let claimed = claim_next_scanner_shadow_job(&mut client, &worker_id).await?;
        let Some(job) = claimed else {
            sleep(Duration::from_millis(poll_ms)).await;
            continue;
        };

        let started_at = Instant::now();
        match build_shadow_scan(&client, job.payload.clone()).await {
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
        }
    }
}

async fn run() -> Result<(), String> {
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| {
        "postgresql://imgviewer:imgviewer@127.0.0.1:5432/imgviewer".to_string()
    });
    let worker_id = format!("rust-scanner-shadow-{}", now_unix());
    let poll_ms = parse_poll_ms();
    let slow_ms = parse_u64_env("IMGVIEWER_SCANNER_SLOW_MS", 2000) as u128;

    eprintln!("[rust-scanner-worker] started as {}", worker_id);
    run_worker_loop(db_url, worker_id, poll_ms, slow_ms).await
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("[rust-scanner-worker] fatal: {e}");
        std::process::exit(1);
    }
}
