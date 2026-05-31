#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN=""
LAST_DB_CHECK_OUT=""

info() {
  echo "[repair-db] $*"
}

warn() {
  echo "[repair-db] WARN: $*" >&2
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

container_exists() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "imgviewer-postgres"
}

container_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq "imgviewer-postgres"
}

compose_postgres_running_healthy() {
  local cid state health
  cid="$(docker compose ps -q postgres 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    return 1
  fi
  state="$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null || true)"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)"
  [[ "$state" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]
}

wait_for_compose_health() {
  local timeout_sec="${1:-60}"
  local elapsed=0
  local cid state health

  while (( elapsed < timeout_sec )); do
    cid="$(docker compose ps -q postgres 2>/dev/null || true)"
    if [[ -n "$cid" ]]; then
      state="$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null || true)"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)"
      info "compose postgres state=${state:-unknown} health=${health:-unknown}"
      if [[ "$state" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
        return 0
      fi
    else
      info "compose postgres container not yet available"
    fi

    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

wait_for_pg_isready_in_container() {
  local timeout_sec="${1:-40}"
  local elapsed=0
  while (( elapsed < timeout_sec )); do
    if docker exec imgviewer-postgres pg_isready -U imgviewer -d imgviewer >/dev/null 2>&1; then
      info "pg_isready inside container: accepting connections"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

python_db_check() {
  local rc
  set +e
  LAST_DB_CHECK_OUT="$("$PYTHON_BIN" - <<'PY' 2>&1
import psycopg
from app.config import DATABASE_URL

print(f"[python] app.config.DATABASE_URL={DATABASE_URL}")
with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
    with conn.cursor() as cur:
        cur.execute("select 1")
        print(f"[python] select_1={cur.fetchone()}")
PY
)"
  rc=$?
  set -e
  echo "$LAST_DB_CHECK_OUT"
  return $rc
}

show_failure_diagnostics() {
  echo
  warn "final diagnostics after failed repair attempt"
  docker compose ps || true
  docker ps -a --filter name=imgviewer-postgres || true
  if container_exists; then
    docker logs --tail 120 imgviewer-postgres || true
    docker inspect --format '{{json .State.Health}}' imgviewer-postgres || true
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ':5432' || true
  fi
  warn "Volume was not deleted. If the database volume is corrupted, ask before running docker compose down -v."
}

main() {
  info "repo root: $REPO_ROOT"
  load_env_file
  if ! pick_python; then
    exit 1
  fi
  info "python: $PYTHON_BIN"

  echo
  info "docker compose services"
  docker compose config --services || true

  echo
  info "docker compose ps"
  docker compose ps || true

  # A) compose reports running/healthy
  if compose_postgres_running_healthy; then
    info "compose reports postgres running/healthy; skipping recreation"
    if python_db_check; then
      info "Python DB check passed."
      exit 0
    fi
    warn "Python DB check failed even though compose reports healthy."
  fi

  # B) container exists but compose does not show running service
  if container_exists && ! compose_postgres_running_healthy; then
    info "container exists but compose does not report healthy running service; trying docker start"
    docker start imgviewer-postgres || true
    wait_for_pg_isready_in_container 40 || warn "pg_isready did not become healthy after docker start"
    if python_db_check; then
      info "Python DB check passed after docker start."
      exit 0
    fi
    warn "Python DB check still failing after docker start."
  fi

  # D) container missing
  if ! container_exists; then
    info "container not found; running docker compose up -d postgres"
    docker compose up -d postgres
    if ! wait_for_compose_health 60; then
      warn "compose postgres did not become healthy in time"
    fi
    wait_for_pg_isready_in_container 40 || warn "pg_isready did not become ready in time"
    if python_db_check; then
      info "Python DB check passed after compose up."
      exit 0
    fi
    warn "Python DB check still failing after compose up."
  fi

  # C) container exists but host-side python check still fails
  if container_exists; then
    warn "attempting container-only recreation (volume preserved)"
    docker logs --tail 80 imgviewer-postgres || true
    docker rm -f imgviewer-postgres
    docker compose up -d postgres
    if ! wait_for_compose_health 60; then
      warn "compose postgres did not become healthy in time after recreation"
    fi
    wait_for_pg_isready_in_container 40 || warn "pg_isready did not become ready in time after recreation"
    if python_db_check; then
      info "Python DB check passed after container recreation."
      exit 0
    fi
  fi

  # E) still failing
  show_failure_diagnostics
  exit 1
}

main "$@"
