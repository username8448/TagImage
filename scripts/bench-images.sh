#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
LIMIT="${LIMIT:-48}"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

if [[ "$PYTHON_BIN" == */* ]]; then
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[bench] python not found or not executable: $PYTHON_BIN" >&2
    exit 1
  fi
else
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[bench] python interpreter '$PYTHON_BIN' not found in PATH" >&2
    exit 1
  fi
fi

echo "[bench] base=$BASE_URL limit=$LIMIT repo_root=$REPO_ROOT python=$PYTHON_BIN"

bench_url() {
  local label="$1"
  local url="$2"
  local count="${3:-5}"
  for i in $(seq 1 "$count"); do
    curl -o /dev/null -s -w "${label}_%{http_code}_%{time_total}\n" "$url"
  done
}

bench_url "api_images_no_total" "$BASE_URL/api/images?limit=$LIMIT&sort=path_asc&include_total=0"
bench_url "api_images_total" "$BASE_URL/api/images?limit=$LIMIT&sort=path_asc&include_total=1" 3

IDS="$(cd "$REPO_ROOT" && "$PYTHON_BIN" - <<'PY'
from app.repo.db import db_connect
with db_connect() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM images WHERE hidden=false ORDER BY lower(path), path LIMIT 12")
        print(" ".join(row[0] for row in cur.fetchall()))
PY
)"

for id in $IDS; do
  curl -o /dev/null -s -w "thumb_file_%{http_code}_%{time_total}\n" "$BASE_URL/thumb-file/$id.jpg"
done

for id in $IDS; do
  curl -o /dev/null -s -w "thumb_compat_%{http_code}_%{time_total}\n" "$BASE_URL/thumb/$id"
done
