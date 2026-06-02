pub mod sqlite;
mod sqlite_schema;

use serde_json::{json, Value};
use tokio_postgres::Client;

#[derive(Debug, Clone)]
pub struct ClaimedJob {
    pub id: String,
    pub attempt: i32,
    pub max_attempts: i32,
    pub payload: Value,
}

fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn ms_to_u64(value: u128) -> u64 {
    value.min(u64::MAX as u128) as u64
}

pub async fn claim_next_thumb_job(
    client: &mut Client,
    worker_id: &str,
) -> Result<Option<ClaimedJob>, String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx: {e}"))?;

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

    tx.commit()
        .await
        .map_err(|e| format!("commit claim: {e}"))?;

    Ok(Some(ClaimedJob {
        id: job_id,
        attempt,
        max_attempts,
        payload,
    }))
}

pub async fn claim_next_rescan_job(
    client: &mut Client,
    worker_id: &str,
) -> Result<Option<ClaimedJob>, String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx: {e}"))?;

    let rows = tx
        .query(
            r#"
            WITH picked AS (
                SELECT j.id
                FROM jobs j
                WHERE j.state = 'queued'
                  AND j.scheduled_at <= now()
                  AND j.job_type = 'rescan'
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
                progress_total = 0,
                updated_at = now(),
                error = NULL
            FROM picked
            WHERE j.id = picked.id
            RETURNING j.id, j.attempt, j.max_attempts, j.payload
            "#,
            &[&worker_id],
        )
        .await
        .map_err(|e| format!("claim rescan update: {e}"))?;

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
    .map_err(|e| format!("insert rescan attempt: {e}"))?;

    let event_data = json!({"attempt": attempt, "worker_id": worker_id});
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'started', $2)",
        &[&job_id, &event_data],
    )
    .await
    .map_err(|e| format!("insert rescan started event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit rescan claim: {e}"))?;

    Ok(Some(ClaimedJob {
        id: job_id,
        attempt,
        max_attempts,
        payload,
    }))
}

pub async fn touch_job_progress(
    client: &Client,
    job_id: &str,
    done: i32,
    total: Option<i32>,
) -> Result<(), String> {
    if let Some(total) = total {
        client
            .execute(
                "UPDATE jobs SET progress_done = GREATEST(0, $2), progress_total = GREATEST(0, $3), updated_at = now() WHERE id = $1",
                &[&job_id, &done, &total],
            )
            .await
            .map_err(|e| format!("touch progress total: {e}"))?;
    } else {
        client
            .execute(
                "UPDATE jobs SET progress_done = GREATEST(0, $2), updated_at = now() WHERE id = $1",
                &[&job_id, &done],
            )
            .await
            .map_err(|e| format!("touch progress: {e}"))?;
    }
    Ok(())
}

pub async fn mark_rescan_succeeded(
    client: &mut Client,
    job: &ClaimedJob,
    total: i32,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx rescan succeed: {e}"))?;

    tx.execute(
        "UPDATE jobs SET state = 'succeeded', progress_done = GREATEST($2, 0), progress_total = GREATEST($2, 0), finished_at = now(), error = NULL, updated_at = now() WHERE id = $1",
        &[&job.id, &total],
    )
    .await
    .map_err(|e| format!("update rescan succeed: {e}"))?;

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
    .map_err(|e| format!("update rescan attempt succeed: {e}"))?;

    let event_data = json!({"total": total});
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'succeeded', $2)",
        &[&job.id, &event_data],
    )
    .await
    .map_err(|e| format!("insert rescan success event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit rescan succeed: {e}"))
}

pub async fn mark_rescan_failed(
    client: &mut Client,
    job: &ClaimedJob,
    error: &str,
    max_backoff_sec: i64,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx rescan fail: {e}"))?;
    let error_text = error.to_string();

    let retries_left = (job.max_attempts - job.attempt).max(0);
    let (next_state, event_name, backoff) = if retries_left > 0 {
        let secs = 2_i64
            .pow(job.attempt.max(1) as u32)
            .min(max_backoff_sec.max(1));
        ("queued", "retry_scheduled", secs)
    } else {
        ("failed", "failed", 0)
    };

    if next_state == "queued" {
        tx.execute(
            "UPDATE jobs SET state = 'queued', error = $2, scheduled_at = now() + ($3::bigint * INTERVAL '1 second'), worker_id = NULL, updated_at = now() WHERE id = $1",
            &[&job.id, &error_text, &backoff],
        )
        .await
        .map_err(|e| format!("update rescan retry: {e}"))?;
    } else {
        tx.execute(
            "UPDATE jobs SET state = 'failed', error = $2, finished_at = now(), updated_at = now() WHERE id = $1",
            &[&job.id, &error_text],
        )
        .await
        .map_err(|e| format!("update rescan failed: {e}"))?;
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
        &[&job.id, &next_state, &error_text],
    )
    .await
    .map_err(|e| format!("update rescan attempt fail: {e}"))?;

    let event_data = json!({
        "error": error,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
    });
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, $2, $3)",
        &[&job.id, &event_name, &event_data],
    )
    .await
    .map_err(|e| format!("insert rescan failure event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit rescan fail: {e}"))
}

pub async fn mark_thumb_succeeded(
    client: &mut Client,
    job: &ClaimedJob,
    metrics: Option<Value>,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx succeed: {e}"))?;

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

    let mut event_data = json!({"attempt": job.attempt, "completed_at": now_unix()});
    if let Some(metrics_data) = metrics {
        if let Some(obj) = event_data.as_object_mut() {
            obj.insert("metrics".to_string(), metrics_data);
        }
    }

    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'succeeded', $2)",
        &[&job.id, &event_data],
    )
    .await
    .map_err(|e| format!("insert success event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit succeed: {e}"))
}

pub async fn mark_thumb_failed(
    client: &mut Client,
    job: &ClaimedJob,
    error: &str,
    max_backoff_sec: i64,
    total_ms: Option<u128>,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx fail: {e}"))?;
    let error_text = error.to_string();

    let retries_left = (job.max_attempts - job.attempt).max(0);
    let (next_state, event_name, backoff) = if retries_left > 0 {
        let secs = 2_i64
            .pow(job.attempt.max(1) as u32)
            .min(max_backoff_sec.max(1));
        ("queued", "retry_scheduled", secs)
    } else {
        ("failed", "failed", 0)
    };
    let next_state_text = next_state.to_string();
    let event_name_text = event_name.to_string();

    if next_state == "queued" {
        tx.execute(
            "UPDATE jobs SET state = 'queued', error = $2, scheduled_at = now() + ($3::bigint * INTERVAL '1 second'), worker_id = NULL, updated_at = now() WHERE id = $1",
            &[&job.id, &error_text, &backoff],
        )
        .await
        .map_err(|e| format!("update retry: {e}"))?;
    } else {
        tx.execute(
            "UPDATE jobs SET state = 'failed', error = $2, finished_at = now(), updated_at = now() WHERE id = $1",
            &[&job.id, &error_text],
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
        &[&job.id, &next_state_text, &error_text],
    )
    .await
    .map_err(|e| format!("update attempt fail: {e}"))?;

    let event_data = json!({
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error": error,
        "next_state": next_state,
        "backoff": backoff,
        "total_ms": total_ms.map(ms_to_u64),
    });
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, $2, $3)",
        &[&job.id, &event_name_text, &event_data],
    )
    .await
    .map_err(|e| format!("insert fail event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit fail: {e}"))
}

pub async fn claim_next_metadata_job(
    client: &mut Client,
    worker_id: &str,
) -> Result<Option<ClaimedJob>, String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx: {e}"))?;

    let rows = tx
        .query(
            r#"
            WITH picked AS (
                SELECT j.id
                FROM jobs j
                WHERE j.state = 'queued'
                  AND j.scheduled_at <= now()
                  AND j.job_type = 'metadata'
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

    tx.commit()
        .await
        .map_err(|e| format!("commit claim: {e}"))?;

    Ok(Some(ClaimedJob {
        id: job_id,
        attempt,
        max_attempts,
        payload,
    }))
}

pub async fn mark_metadata_succeeded(
    client: &mut Client,
    job: &ClaimedJob,
    metadata_json: Value,
    authoritative: bool,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx succeed: {e}"))?;

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

    let event_data = metadata_success_event_data(job.attempt, metadata_json, authoritative);

    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'succeeded', $2)",
        &[&job.id, &event_data],
    )
    .await
    .map_err(|e| format!("insert success event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit succeed: {e}"))
}

pub fn metadata_success_event_data(
    attempt: i32,
    metadata_json: Value,
    authoritative: bool,
) -> Value {
    json!({
        "attempt": attempt,
        "completed_at": now_unix(),
        "metadata": metadata_json,
        "authoritative": authoritative,
        "shadow": !authoritative,
    })
}

pub async fn claim_next_scanner_shadow_job(
    client: &mut Client,
    worker_id: &str,
) -> Result<Option<ClaimedJob>, String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx: {e}"))?;

    let rows = tx
        .query(
            r#"
            WITH picked AS (
                SELECT j.id
                FROM jobs j
                WHERE j.state = 'queued'
                  AND j.scheduled_at <= now()
                  AND j.job_type = 'scanner_shadow'
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
                progress_total = 0,
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

    tx.commit()
        .await
        .map_err(|e| format!("commit claim: {e}"))?;

    Ok(Some(ClaimedJob {
        id: job_id,
        attempt,
        max_attempts,
        payload,
    }))
}

pub async fn mark_scanner_shadow_succeeded(
    client: &mut Client,
    job: &ClaimedJob,
    scan_json: Value,
    total: i32,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx succeed: {e}"))?;

    tx.execute(
        "UPDATE jobs SET state = 'succeeded', progress_done = GREATEST($2, 0), progress_total = GREATEST($2, 0), finished_at = now(), error = NULL, updated_at = now() WHERE id = $1",
        &[&job.id, &total],
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

    let event_data = json!({
        "attempt": job.attempt,
        "completed_at": now_unix(),
        "scanner_shadow": scan_json,
        "shadow": true,
    });

    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, 'succeeded', $2)",
        &[&job.id, &event_data],
    )
    .await
    .map_err(|e| format!("insert success event: {e}"))?;

    tx.commit()
        .await
        .map_err(|e| format!("commit succeed: {e}"))
}

pub async fn mark_scanner_shadow_failed(
    client: &mut Client,
    job: &ClaimedJob,
    error: &str,
    total_ms: Option<u128>,
    max_backoff_sec: i64,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx fail: {e}"))?;
    let error_text = error.to_string();

    let retries_left = (job.max_attempts - job.attempt).max(0);
    let (next_state, event_name, backoff) = if retries_left > 0 {
        let secs = 2_i64
            .pow(job.attempt.max(1) as u32)
            .min(max_backoff_sec.max(1));
        ("queued", "retry_scheduled", secs)
    } else {
        ("failed", "failed", 0)
    };
    let next_state_text = next_state.to_string();
    let event_name_text = event_name.to_string();

    if next_state == "queued" {
        tx.execute(
            "UPDATE jobs SET state = 'queued', error = $2, scheduled_at = now() + ($3::bigint * INTERVAL '1 second'), worker_id = NULL, updated_at = now() WHERE id = $1",
            &[&job.id, &error_text, &backoff],
        )
        .await
        .map_err(|e| format!("update retry: {e}"))?;
    } else {
        tx.execute(
            "UPDATE jobs SET state = 'failed', error = $2, finished_at = now(), updated_at = now() WHERE id = $1",
            &[&job.id, &error_text],
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
        &[&job.id, &next_state_text, &error_text],
    )
    .await
    .map_err(|e| format!("update attempt fail: {e}"))?;

    let event_data = json!({
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error": error,
        "next_state": next_state,
        "backoff": backoff,
        "total_ms": total_ms.map(ms_to_u64),
    });
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, $2, $3)",
        &[&job.id, &event_name_text, &event_data],
    )
    .await
    .map_err(|e| format!("insert fail event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit fail: {e}"))
}

pub async fn mark_metadata_failed(
    client: &mut Client,
    job: &ClaimedJob,
    error: &str,
    total_ms: Option<u128>,
    max_backoff_sec: i64,
) -> Result<(), String> {
    let tx = client
        .transaction()
        .await
        .map_err(|e| format!("begin tx fail: {e}"))?;
    let error_text = error.to_string();

    let retries_left = (job.max_attempts - job.attempt).max(0);
    let (next_state, event_name, backoff) = if retries_left > 0 {
        let secs = 2_i64
            .pow(job.attempt.max(1) as u32)
            .min(max_backoff_sec.max(1));
        ("queued", "retry_scheduled", secs)
    } else {
        ("failed", "failed", 0)
    };
    let next_state_text = next_state.to_string();
    let event_name_text = event_name.to_string();

    if next_state == "queued" {
        tx.execute(
            "UPDATE jobs SET state = 'queued', error = $2, scheduled_at = now() + ($3::bigint * INTERVAL '1 second'), worker_id = NULL, updated_at = now() WHERE id = $1",
            &[&job.id, &error_text, &backoff],
        )
        .await
        .map_err(|e| format!("update retry: {e}"))?;
    } else {
        tx.execute(
            "UPDATE jobs SET state = 'failed', error = $2, finished_at = now(), updated_at = now() WHERE id = $1",
            &[&job.id, &error_text],
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
        &[&job.id, &next_state_text, &error_text],
    )
    .await
    .map_err(|e| format!("update attempt fail: {e}"))?;

    let event_data = json!({
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error": error,
        "next_state": next_state,
        "backoff": backoff,
        "total_ms": total_ms.map(ms_to_u64),
    });
    tx.execute(
        "INSERT INTO job_events (job_id, event, data) VALUES ($1, $2, $3)",
        &[&job.id, &event_name_text, &event_data],
    )
    .await
    .map_err(|e| format!("insert fail event: {e}"))?;

    tx.commit().await.map_err(|e| format!("commit fail: {e}"))
}

#[cfg(test)]
mod tests {
    use super::metadata_success_event_data;
    use serde_json::json;

    #[test]
    fn metadata_success_event_marks_authoritative_mode() {
        let event = metadata_success_event_data(2, json!({"image_id": "img-1"}), true);

        assert_eq!(event["attempt"], 2);
        assert_eq!(event["metadata"]["image_id"], "img-1");
        assert_eq!(event["authoritative"], true);
        assert_eq!(event["shadow"], false);
    }

    #[test]
    fn metadata_success_event_keeps_shadow_mode_available() {
        let event = metadata_success_event_data(1, json!({"image_id": "img-2"}), false);

        assert_eq!(event["authoritative"], false);
        assert_eq!(event["shadow"], true);
    }
}
