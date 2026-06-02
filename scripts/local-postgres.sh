#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PGDATA="${PGDATA:-$REPO_ROOT/.run/local-postgres}"
LOG_FILE="${LOG_FILE:-$REPO_ROOT/.logs/local-postgres.log}"
PGHOST="${IMGVIEWER_LOCAL_PG_HOST:-127.0.0.1}"
PGPORT="${IMGVIEWER_LOCAL_PG_PORT:-55432}"
PGUSER="${IMGVIEWER_LOCAL_PG_USER:-imgviewer}"
PGDATABASE="${IMGVIEWER_LOCAL_PG_DB:-imgviewer}"
PGPASSWORD="${IMGVIEWER_LOCAL_PG_PASSWORD:-imgviewer}"

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/local-postgres.sh init
  ./scripts/local-postgres.sh start
  ./scripts/local-postgres.sh stop
  ./scripts/local-postgres.sh restart
  ./scripts/local-postgres.sh status
  ./scripts/local-postgres.sh url
USAGE
}

info() {
  echo "[local-postgres] $*"
}

warn() {
  echo "[local-postgres] WARN: $*" >&2
}

die() {
  echo "[local-postgres] ERROR: $*" >&2
  exit 1
}

ensure_tools() {
  command -v initdb >/dev/null 2>&1 || die "initdb not found"
  command -v pg_ctl >/dev/null 2>&1 || die "pg_ctl not found"
  command -v psql >/dev/null 2>&1 || die "psql not found"
  command -v createdb >/dev/null 2>&1 || die "createdb not found"
}

ensure_dirs() {
  mkdir -p "$REPO_ROOT/.run" "$REPO_ROOT/.logs"
}

db_url() {
  echo "postgresql://${PGUSER}:${PGPASSWORD}@${PGHOST}:${PGPORT}/${PGDATABASE}"
}

is_initialized() {
  [[ -f "$PGDATA/PG_VERSION" ]]
}

ensure_initialized() {
  if is_initialized; then
    return 0
  fi
  init_cluster
}

is_running() {
  pg_ctl -D "$PGDATA" status >/dev/null 2>&1
}

ensure_database_exists() {
  if PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -c "select 1" >/dev/null 2>&1; then
    return 0
  fi
  info "creating database: $PGDATABASE"
  PGPASSWORD="$PGPASSWORD" createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE"
}

sql_escape_literal() {
  printf "%s" "$1" | sed "s/'/''/g"
}

sql_escape_identifier() {
  printf "%s" "$1" | sed 's/"/""/g'
}

ensure_role_password() {
  local role password
  role="$(sql_escape_identifier "$PGUSER")"
  password="$(sql_escape_literal "$PGPASSWORD")"
  if PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 \
    -c "ALTER ROLE \"$role\" WITH PASSWORD '$password'" >/dev/null 2>&1; then
    info "role password ensured: $PGUSER"
    return 0
  fi
  warn "could not ensure password for role $PGUSER; continuing with existing local auth"
}

init_cluster() {
  ensure_tools
  ensure_dirs
  if is_initialized; then
    info "already initialized: $PGDATA"
    info "log: $LOG_FILE"
    info "url: $(db_url)"
    return 0
  fi
  info "initdb at $PGDATA"
  initdb -D "$PGDATA" --username="$PGUSER" --auth=trust >/dev/null
  info "initialized"
  info "pgdata: $PGDATA"
  info "log: $LOG_FILE"
  info "url: $(db_url)"
}

start_cluster() {
  ensure_tools
  ensure_dirs
  ensure_initialized

  if is_running; then
    info "already running"
  else
    local socket_dir
    socket_dir="$(cd "$PGDATA" && pwd)"
    info "starting on ${PGHOST}:${PGPORT}"
    pg_ctl -D "$PGDATA" -l "$LOG_FILE" -o "-p $PGPORT -h $PGHOST -k $socket_dir" start >/dev/null
  fi

  ensure_role_password
  ensure_database_exists
  info "url: $(db_url)"
}

stop_cluster() {
  ensure_tools
  if ! is_initialized; then
    info "not initialized"
    return 0
  fi
  if ! is_running; then
    info "already stopped"
    return 0
  fi
  info "stopping"
  pg_ctl -D "$PGDATA" stop >/dev/null
  info "stopped"
}

status_cluster() {
  ensure_tools
  if ! is_initialized; then
    info "status: not initialized ($PGDATA)"
    info "pgdata: $PGDATA"
    info "log: $LOG_FILE"
    info "url: $(db_url)"
    return 1
  fi
  if ! is_running; then
    info "status: stopped"
    info "pgdata: $PGDATA"
    info "log: $LOG_FILE"
    info "url: $(db_url)"
    return 1
  fi

  info "status: running"
  info "pgdata: $PGDATA"
  info "log: $LOG_FILE"
  info "url: $(db_url)"
  if PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -c "select 1" >/dev/null 2>&1; then
    info "select 1: ok"
    return 0
  fi
  warn "select 1 failed for $(db_url)"
  return 1
}

restart_cluster() {
  stop_cluster || true
  start_cluster
}

main() {
  cd "$REPO_ROOT"
  local action="${1:-}"
  case "$action" in
    init)
      init_cluster
      ;;
    start)
      start_cluster
      ;;
    stop)
      stop_cluster
      ;;
    restart)
      restart_cluster
      ;;
    status)
      status_cluster
      ;;
    url)
      db_url
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
