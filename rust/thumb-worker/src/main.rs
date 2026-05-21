use image::codecs::jpeg::JpegEncoder;
use image::ColorType;
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::time::sleep;
use tokio_postgres::{Client, NoTls};

#[derive(Debug, Clone)]
struct ClaimedJob {
    id: String,
    attempt: i32,
    max_attempts: i32,
    payload: Value,
}

#[derive(Debug, Deserialize)]
struct ThumbPayload {
    image_id: Option<String>,
    root_path: String,
    path: String,
    thumb: String,
    mtime: Option<i64>,
    max_size: Option<Vec<u32>>,
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn file_mtime(path: &Path) -> Option<i64> {
    let meta = fs::metadata(path).ok()?;
    let modified = meta.modified().ok()?;
    modified.duration_since(UNIX_EPOCH).ok().map(|d| d.as_secs() as i64)
}

fn parse_max_size(payload: &ThumbPayload) -> (u32, u32) {
    if let Some(parts) = &payload.max_size {
        if parts.len() >= 2 {
            let w = parts[0].max(32);
            let h = parts[1].max(32);
            return (w, h);
        }
    }
    (640, 640)
}

fn render_thumb(source: &Path, target: &Path, max_size: (u32, u32)) -> Result<(), String> {
    let dyn_img = image::open(source).map_err(|e| format!("open image {}: {e}", source.display()))?;
    let thumb = dyn_img.thumbnail(max_size.0, max_size.1).to_rgb8();

    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create dir {}: {e}", parent.display()))?;
    }

    let file = fs::File::create(target).map_err(|e| format!("create thumb {}: {e}", target.display()))?;
    let mut writer = BufWriter::new(file);
    let mut encoder = JpegEncoder::new_with_quality(&mut writer, 86);
    encoder
        .encode(
            &thumb,
            thumb.width(),
            thumb.height(),
            ColorType::Rgb8.into(),
        )
        .map_err(|e| format!("encode jpeg {}: {e}", target.display()))?;
    writer
        .flush()
        .map_err(|e| format!("flush thumb {}: {e}", target.display()))?;
    Ok(())
}

async fn claim_next_thumb_job(client: &mut Client, worker_id: &str) -> Result<Option<ClaimedJob>, String> {
    let tx = client.transaction().await.map_err(|e| format!("begin tx: {e}"))?;

    let rows = tx
        .query(
            r#"
            WITH picked AS (
                SELECT j.id
                FROM jobs j
                WHERE j.state = 'queued'
                  AND j.scheduled_at <= now()
                  AND j.job_type = 'thumb'
                ORDER BY j.priority DESC, j.scheduled_at, j.created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs j
            SET state = 'running',
                worker_id = $1,
                started_at = COALESCE(j.started_at, now()),
                attempt = j.attempt + 1,
                progress_done = 0,
                progress_total = GREATEST(j.progress_total, 1),
                updated_at = now(),
                error = NULL
            FROM picked
            WHERE j.id = picked.id
            RETURNING j.id, j.attempt, j.max_attempts, j.payload
            "#,
            &[&worker_id],
        )
        .await
        .map_err(|e| format!("claim update: {e}"))?;

    if rows.is_empty() {
        tx.rollback().await.map_err(|e| format!("rollback: {e}"))?;
        return Ok(None);
    }

    let row = &rows[0];
    let job_id: String = row.get("id");
    let attempt: i32 = row.get("attempt");
    let max_attempts: i32 = row.get("max_attempts");
    let payload: Value = row.get("payload");

    tx.execute(
        "INSERT INTO job_attempts (job_id, attempt, worker_id, state) VALUES ($1, $2, $3, 'running')",
        &[&job_id, &attempt, &worker_id],
    )
    .await
    .map_err(|e| format!("insert attempt: {e}"))?;

    let event_data = json!({"attempt": attempt, "worker_id": worker_id, "claimed_at": now_unix()});
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'started', $2)",
        &[&job_id, &event_data],
    )
    .await
    .map_err(|e| format!("insert event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit claim: {e}"))?;

    Ok(Some(ClaimedJob {
        id: job_id,
        attempt,
        max_attempts,
        payload,
    }))
}

async fn mark_succeeded(client: &mut Client, job: &ClaimedJob) -> Result<(), String> {
    let tx = client.transaction().await.map_err(|e| format!("begin tx succeed: {e}"))?;
    tx.execute(
        "UPDATE jobs SET state = 'succeeded', progress_done = 1, progress_total = GREATEST(progress_total, 1), finished_at = now(), error = NULL, updated_at = now() WHERE id = $1",
        &[&job.id],
    )
    .await
    .map_err(|e| format!("update succeed: {e}"))?;

    tx.execute(
        r#"
        UPDATE job_attempts
        SET finished_at = now(), state = 'succeeded', error = NULL
        WHERE id = (
            SELECT id
            FROM job_attempts
            WHERE job_id = $1
            ORDER BY started_at DESC
            LIMIT 1
        )
        "#,
        &[&job.id],
    )
    .await
    .map_err(|e| format!("update attempt succeed: {e}"))?;

    let event_data = json!({"attempt": job.attempt, "completed_at": now_unix()});
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'succeeded', $2)",
        &[&job.id, &event_data],
    )
    .await
    .map_err(|e| format!("insert success event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit succeed: {e}"))
}

async fn mark_failed(client: &mut Client, job: &ClaimedJob, error: &str, max_backoff_sec: i64) -> Result<(), String> {
    let tx = client.transaction().await.map_err(|e| format!("begin tx fail: {e}"))?;

    let retries_left = (job.max_attempts - job.attempt).max(0);
    let (next_state, event_name, backoff) = if retries_left > 0 {
        let secs = 2_i64.pow(job.attempt.max(1) as u32).min(max_backoff_sec.max(1));
        ("queued", "retry_scheduled", secs)
    } else {
        ("failed", "failed", 0)
    };

    if next_state == "queued" {
        tx.execute(
            "UPDATE jobs SET state = 'queued', error = $2, scheduled_at = now() + ($3 * INTERVAL '1 second'), worker_id = NULL, updated_at = now() WHERE id = $1",
            &[&job.id, &error, &backoff],
        )
        .await
        .map_err(|e| format!("update retry: {e}"))?;
    } else {
        tx.execute(
            "UPDATE jobs SET state = 'failed', error = $2, finished_at = now(), updated_at = now() WHERE id = $1",
            &[&job.id, &error],
        )
        .await
        .map_err(|e| format!("update failed: {e}"))?;
    }

    tx.execute(
        r#"
        UPDATE job_attempts
        SET finished_at = now(), state = $2, error = $3
        WHERE id = (
            SELECT id
            FROM job_attempts
            WHERE job_id = $1
            ORDER BY started_at DESC
            LIMIT 1
        )
        "#,
        &[&job.id, &next_state, &error],
    )
    .await
    .map_err(|e| format!("update attempt fail: {e}"))?;

    let event_data = json!({
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error": error,
        "next_state": next_state,
        "backoff": backoff,
    });
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, $2, $3)",
        &[&job.id, &event_name, &event_data],
    )
    .await
    .map_err(|e| format!("insert fail event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit fail: {e}"))
}

fn process_payload(payload: Value) -> Result<(PathBuf, PathBuf, i64, (u32, u32), Option<String>), String> {
    let parsed: ThumbPayload = serde_json::from_value(payload).map_err(|e| format!("invalid payload: {e}"))?;
    let source = Path::new(&parsed.root_path).join(&parsed.path);
    let target = Path::new(&parsed.root_path).join(&parsed.thumb);
    let source_mtime = parsed
        .mtime
        .or_else(|| file_mtime(&source))
        .ok_or_else(|| format!("cannot read source mtime: {}", source.display()))?;
    let max_size = parse_max_size(&parsed);
    Ok((source, target, source_mtime, max_size, parsed.image_id))
}

async fn run() -> Result<(), String> {
    let db_url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgresql://imgviewer:imgviewer@127.0.0.1:5432/imgviewer".to_string());
    let poll_ms = std::env::var("IMGVIEWER_THUMB_WORKER_POLL_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(750)
        .max(50);
    let worker_id = std::env::var("IMGVIEWER_THUMB_WORKER_ID")
        .unwrap_or_else(|_| format!("rust-thumb-{}", now_unix()));
    let max_backoff_sec = std::env::var("IMGVIEWER_THUMB_MAX_BACKOFF_SEC")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(300)
        .max(1);

    let (mut client, connection) = tokio_postgres::connect(&db_url, NoTls)
        .await
        .map_err(|e| format!("connect postgres: {e}"))?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("[rust-thumb-worker] postgres connection error: {e}");
        }
    });

    eprintln!("[rust-thumb-worker] started as {}", worker_id);

    loop {
        let claimed = claim_next_thumb_job(&mut client, &worker_id).await?;
        let Some(job) = claimed else {
            sleep(Duration::from_millis(poll_ms)).await;
            continue;
        };

        let result = (|| {
            let (source, target, source_mtime, max_size, image_id) = process_payload(job.payload.clone())?;
            if !source.exists() {
                return Err(format!("source not found: {}", source.display()));
            }
            if let Some(current_mtime) = file_mtime(&target) {
                if current_mtime >= source_mtime {
                    return Ok(());
                }
            }
            render_thumb(&source, &target, max_size)?;
            let _ = image_id; // reserved for future metrics/grouping
            Ok(())
        })();

        match result {
            Ok(()) => {
                if let Err(e) = mark_succeeded(&mut client, &job).await {
                    eprintln!("[rust-thumb-worker] mark success failed for {}: {}", job.id, e);
                }
            }
            Err(err) => {
                if let Err(e) = mark_failed(&mut client, &job, &err, max_backoff_sec).await {
                    eprintln!("[rust-thumb-worker] mark fail failed for {}: {}", job.id, e);
                }
            }
        }
    }
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("[rust-thumb-worker] fatal: {e}");
        std::process::exit(1);
    }
}
