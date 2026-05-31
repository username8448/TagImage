#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN=""

info() {
  echo "[check-db] $*"
}

warn() {
  echo "[check-db] WARN: $*" >&2
}

load_env_file() {
  if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
    info "loaded .env"
  fi
}

pick_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    if command -v "$PYTHON" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$PYTHON")"
      return 0
    fi
    if [[ -x "$PYTHON" ]]; then
      PYTHON_BIN="$PYTHON"
      return 0
    fi
    warn "PYTHON is set but not executable: $PYTHON"
  fi

  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    return 0
  fi

  warn "Python not found (expected PYTHON, .venv/bin/python, or python3)."
  return 1
}

run_step() {
  local title="$1"
  shift
  echo
  info "$title"
  if ! "$@"; then
    warn "command failed: $*"
    return 1
  fi
  return 0
}

container_exists() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "imgviewer-postgres"
}

container_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq "imgviewer-postgres"
}

read_effective_database_url() {
  "$PYTHON_BIN" - <<'PY'
from app.config import DATABASE_URL
print(DATABASE_URL)
PY
}

python_db_check() {
  "$PYTHON_BIN" - <<'PY'
import psycopg
from app.config import DATABASE_URL

print(f"[python] app.config.DATABASE_URL={DATABASE_URL}")
with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
    with conn.cursor() as cur:
        cur.execute("select 1")
        print(f"[python] select_1={cur.fetchone()}")
PY
}

main() {
  info "repo root: $REPO_ROOT"
  load_env_file

  if ! pick_python; then
    exit 1
  fi
  info "python: $PYTHON_BIN"

  echo
  info "effective DATABASE_URL from app.config"
  if ! read_effective_database_url; then
    warn "failed to read app.config.DATABASE_URL"
  fi

  run_step "docker compose services" docker compose config --services || true
  run_step "docker compose ps" docker compose ps || true
  run_step "docker ps -a --filter name=imgviewer-postgres" \
    docker ps -a --filter name=imgviewer-postgres || true

  if container_exists; then
    run_step "docker port imgviewer-postgres" docker port imgviewer-postgres || true
    run_step "docker logs --tail=80 imgviewer-postgres" \
      docker logs --tail 80 imgviewer-postgres || true
  else
    warn "container imgviewer-postgres does not exist"
  fi

  if container_running; then
    run_step "pg_isready inside container" \
      docker exec imgviewer-postgres pg_isready -U imgviewer -d imgviewer || true

    if docker exec imgviewer-postgres sh -lc "command -v psql >/dev/null 2>&1"; then
      run_step "psql select 1 inside container" \
        docker exec imgviewer-postgres sh -lc \
          "PGPASSWORD=imgviewer psql -h 127.0.0.1 -U imgviewer -d imgviewer -c 'select 1'" || true
    else
      warn "psql is not available inside imgviewer-postgres container"
    fi
  else
    warn "container imgviewer-postgres is not running"
  fi

  echo
  info "Python psycopg check via app.config.DATABASE_URL"
  if python_db_check; then
    info "DB check passed."
    exit 0
  fi

  warn "DB check failed."
  warn "Try: ./scripts/repair-db.sh"
  warn "If Docker/5432 is unstable, use native local PostgreSQL:"
  warn "  ./scripts/local-postgres.sh init"
  warn "  ./scripts/local-postgres.sh start"
  warn "Then set DATABASE_URL to:"
  warn "  postgresql://imgviewer:imgviewer@127.0.0.1:55432/imgviewer"
  exit 1
}

main "$@"
