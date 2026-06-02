#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN=""

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

  info "normal repair path: native local PostgreSQL"
  "$REPO_ROOT/scripts/local-postgres.sh" init || exit 1
  "$REPO_ROOT/scripts/local-postgres.sh" start || exit 1
  "$REPO_ROOT/scripts/local-postgres.sh" status || true

  echo
  info "Python psycopg check via app.config.DATABASE_URL"
  if python_db_check; then
    info "DB repair/check passed."
    exit 0
  fi

  warn "DB check still failed after local PostgreSQL start."
  warn "Expected DATABASE_URL:"
  warn "  postgresql://imgviewer:imgviewer@127.0.0.1:55432/imgviewer"
  warn "Run:"
  warn "  ./scripts/local-postgres.sh status"
  warn "  ./scripts/check-db.sh"
  exit 1
}

main "$@"
