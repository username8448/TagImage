#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR="$SCRIPT_DIR/.run"
LOG_DIR="$SCRIPT_DIR/.logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

API_PID_FILE="$RUN_DIR/api.pid"
RESCAN_PID_FILE="$RUN_DIR/rescan-worker.pid"
THUMB_PID_FILE="$RUN_DIR/thumb-worker.pid"

API_LOG="$LOG_DIR/api.log"
RESCAN_LOG="$LOG_DIR/rescan-worker.log"
THUMB_LOG="$LOG_DIR/thumb-worker.log"

PORT="${PORT:-8000}"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"
BUILD_RUST=0
RUST_BIN_PATH=""
ACTION="status"

usage() {
  cat <<EOF
Usage: $0 [start|stop|restart|status] [--build-rust] [--rust-bin-path /path/to/bin]

Examples:
  $0 start
  $0 start --build-rust
  $0 start --rust-bin-path ./rust/thumb-worker/target/release/imgviewer-thumb-worker
  $0 status
EOF
}

is_running_pid() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then
    cat "$file"
  fi
}

write_pid() {
  local file="$1"
  local pid="$2"
  echo "$pid" > "$file"
}

start_process() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  local cmd="$4"

  local pid
  pid="$(read_pid "$pid_file" || true)"
  if [[ -n "${pid:-}" ]] && is_running_pid "$pid"; then
    echo "[$name] already running pid=$pid"
    return 0
  fi

  : > "$log_file"
  nohup setsid bash -lc "$cmd" >> "$log_file" 2>&1 < /dev/null &
  local new_pid=$!
  write_pid "$pid_file" "$new_pid"
  sleep 0.2
  if is_running_pid "$new_pid"; then
    echo "[$name] started pid=$new_pid"
  else
    echo "[$name] failed to start, check $log_file"
    return 1
  fi
}

stop_process() {
  local name="$1"
  local pid_file="$2"
  local pid
  pid="$(read_pid "$pid_file" || true)"
  if [[ -z "${pid:-}" ]]; then
    echo "[$name] not running"
    return 0
  fi
  if ! is_running_pid "$pid"; then
    rm -f "$pid_file"
    echo "[$name] stale pid removed ($pid)"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! is_running_pid "$pid"; then
      rm -f "$pid_file"
      echo "[$name] stopped"
      return 0
    fi
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  echo "[$name] force stopped"
}

build_rust_if_requested() {
  if [[ "$BUILD_RUST" -ne 1 ]]; then
    return 0
  fi
  if [[ -n "$RUST_BIN_PATH" ]]; then
    return 0
  fi
  if ! command -v cargo >/dev/null 2>&1; then
    echo "[thumb-worker] cargo not found; cannot build rust worker"
    return 1
  fi
  echo "[thumb-worker] building rust worker"
  (cd "$SCRIPT_DIR/rust/thumb-worker" && cargo build --release)
}

resolve_rust_worker_cmd() {
  if [[ "${IMGVIEWER_THUMB_JOB_MODE:-sync}" != "queue" ]]; then
    echo ""
    return 0
  fi

  if [[ -n "$RUST_BIN_PATH" ]]; then
    echo "$RUST_BIN_PATH"
    return 0
  fi

  local default_bin="$SCRIPT_DIR/rust/thumb-worker/target/release/imgviewer-thumb-worker"
  if [[ -x "$default_bin" ]]; then
    echo "$default_bin"
    return 0
  fi

  if command -v cargo >/dev/null 2>&1; then
    echo "cd '$SCRIPT_DIR/rust/thumb-worker' && cargo run --release"
    return 0
  fi

  echo ""
}

status_all() {
  local api_pid rescan_pid thumb_pid
  api_pid="$(read_pid "$API_PID_FILE" || true)"
  rescan_pid="$(read_pid "$RESCAN_PID_FILE" || true)"
  thumb_pid="$(read_pid "$THUMB_PID_FILE" || true)"

  if [[ -n "${api_pid:-}" ]] && is_running_pid "$api_pid"; then echo "[api] running pid=$api_pid"; else echo "[api] stopped"; fi
  if [[ -n "${rescan_pid:-}" ]] && is_running_pid "$rescan_pid"; then echo "[rescan-worker] running pid=$rescan_pid"; else echo "[rescan-worker] stopped"; fi
  if [[ -n "${thumb_pid:-}" ]] && is_running_pid "$thumb_pid"; then echo "[thumb-worker] running pid=$thumb_pid"; else echo "[thumb-worker] stopped"; fi

  if command -v curl >/dev/null 2>&1; then
    echo "[health] GET http://127.0.0.1:${PORT}/api/status"
    curl -fsS "http://127.0.0.1:${PORT}/api/status" || echo "[health] unavailable"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|restart|status)
      ACTION="$1"; shift ;;
    --build-rust)
      BUILD_RUST=1; shift ;;
    --rust-bin-path)
      RUST_BIN_PATH="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

case "$ACTION" in
  start)
    if [[ ! -x "$PYTHON_BIN" ]]; then
      echo "python not found: $PYTHON_BIN"
      exit 1
    fi
    build_rust_if_requested
    start_process "api" "$API_PID_FILE" "$API_LOG" "cd '$SCRIPT_DIR' && '$PYTHON_BIN' -m uvicorn main:app --host 0.0.0.0 --port '$PORT' --log-level warning"
    start_process "rescan-worker" "$RESCAN_PID_FILE" "$RESCAN_LOG" "cd '$SCRIPT_DIR' && '$PYTHON_BIN' worker.py"

    rust_cmd="$(resolve_rust_worker_cmd)"
    if [[ -n "$rust_cmd" ]]; then
      start_process "thumb-worker" "$THUMB_PID_FILE" "$THUMB_LOG" "cd '$SCRIPT_DIR' && $rust_cmd"
    else
      echo "[thumb-worker] skipped (mode=${IMGVIEWER_THUMB_JOB_MODE:-sync}, rust binary unavailable)"
      rm -f "$THUMB_PID_FILE"
    fi
    ;;
  stop)
    stop_process "thumb-worker" "$THUMB_PID_FILE"
    stop_process "rescan-worker" "$RESCAN_PID_FILE"
    stop_process "api" "$API_PID_FILE"
    ;;
  restart)
    "$0" stop
    RESTART_ARGS=(start)
    if [[ "$BUILD_RUST" -eq 1 ]]; then
      RESTART_ARGS+=(--build-rust)
    fi
    if [[ -n "$RUST_BIN_PATH" ]]; then
      RESTART_ARGS+=(--rust-bin-path "$RUST_BIN_PATH")
    fi
    "$0" "${RESTART_ARGS[@]}"
    ;;
  status)
    status_all
    ;;
  *)
    usage
    exit 1
    ;;
esac
