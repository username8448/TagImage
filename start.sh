#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  ImgViewer — launcher for Linux
#  Usage:
#    ./start.sh                     # manual folder input in browser
#    ./start.sh /path/to/photos     # pre-load folder
#    ./start.sh --port 9000         # custom port
#    ./start.sh /photos --port 9000 # both
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colors ─────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Defaults ────────────────────────────────────────────────────────
PORT=8000
FOLDER=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Parse args ──────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port|-p)
      PORT="$2"; shift 2 ;;
    --help|-h)
      echo -e "${BOLD}ImgViewer launcher${RESET}"
      echo ""
      echo "  ./start.sh                        open browser, enter path manually"
      echo "  ./start.sh /path/to/photos        pre-load folder"
      echo "  ./start.sh /path/to/photos -p 9000"
      exit 0 ;;
    -*)
      echo -e "${RED}Unknown option: $1${RESET}"; exit 1 ;;
    *)
      FOLDER="$1"; shift ;;
  esac
done

# ── Banner ──────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}${BOLD}  ╔══════════════════════════════╗"
echo -e "  ║   ImgViewer  📷             ║"
echo -e "  ╚══════════════════════════════╝${RESET}"
echo ""

cd "$SCRIPT_DIR"

# ── Optional local environment ──────────────────────────────────────
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
  echo -e "${GREEN}✓${RESET}  .env загружен"
fi

# ── Ensure static/ dir and index.html exist ─────────────────────────────────
mkdir -p "$SCRIPT_DIR/static"

# If index.html sits next to start.sh (flat layout), move it into static/
if [[ -f "$SCRIPT_DIR/index.html" && ! -f "$SCRIPT_DIR/static/index.html" ]]; then
  cp "$SCRIPT_DIR/index.html" "$SCRIPT_DIR/static/index.html"
  echo -e "${GREEN}✓${RESET}  index.html → static/index.html"
fi

if [[ ! -f "$SCRIPT_DIR/static/index.html" ]]; then
  echo -e "${RED}✗  Файл static/index.html не найден!${RESET}"
  echo -e "   Убедись, что index.html лежит в папке ${YELLOW}static/${RESET} рядом с main.py"
  exit 1
fi

# ── Check Python ────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    VER=$("$cmd" -c "import sys; print(sys.version_info >= (3,9))")
    if [[ "$VER" == "True" ]]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo -e "${RED}✗  Python 3.9+ не найден.${RESET}"
  echo -e "   Установи: ${YELLOW}sudo apt install python3 python3-pip${RESET}"
  exit 1
fi

echo -e "${GREEN}✓${RESET}  Python: $($PYTHON --version)"

# ── Virtual environment ─────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
  echo -e "${CYAN}→${RESET}  Создаю виртуальное окружение (.venv)…"
  "$PYTHON" -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"
PYTHON="$VENV_DIR/bin/python"

# ── Install / check dependencies ────────────────────────────────────
REQ_FILE="$SCRIPT_DIR/requirements.txt"
STAMP_FILE="$VENV_DIR/.installed_stamp"

# Reinstall if requirements.txt is newer than stamp
NEED_INSTALL=false
if [[ ! -f "$STAMP_FILE" ]]; then
  NEED_INSTALL=true
elif [[ "$REQ_FILE" -nt "$STAMP_FILE" ]]; then
  NEED_INSTALL=true
fi

# Quick sanity check: can we import runtime deps?
if ! "$PYTHON" -c "import fastapi, uvicorn, PIL, psycopg" &>/dev/null 2>&1; then
  NEED_INSTALL=true
fi

if $NEED_INSTALL; then
  echo -e "${CYAN}→${RESET}  Устанавливаю зависимости (requirements.txt)…"
  pip install -q --upgrade pip
  pip install -q -r "$REQ_FILE"
  touch "$STAMP_FILE"
  echo -e "${GREEN}✓${RESET}  Зависимости установлены"
else
  echo -e "${GREEN}✓${RESET}  Зависимости актуальны"
fi

# ── Check port availability ─────────────────────────────────────────
if ss -tlnH "sport = :$PORT" 2>/dev/null | grep -q ":$PORT"; then
  echo -e "${YELLOW}⚠${RESET}  Порт $PORT занят. Ищу свободный…"
  for P in $(seq $((PORT+1)) $((PORT+20))); do
    if ! ss -tlnH "sport = :$P" 2>/dev/null | grep -q ":$P"; then
      PORT=$P; break
    fi
  done
  echo -e "${GREEN}✓${RESET}  Буду использовать порт $PORT"
fi

# ── Validate folder ─────────────────────────────────────────────────
if [[ -n "$FOLDER" ]]; then
  FOLDER="$(realpath "$FOLDER" 2>/dev/null || echo "$FOLDER")"
  if [[ ! -d "$FOLDER" ]]; then
    echo -e "${RED}✗  Папка не найдена: $FOLDER${RESET}"
    exit 1
  fi
  echo -e "${GREEN}✓${RESET}  Папка: $FOLDER"
fi

# ── Open browser ────────────────────────────────────────────────────
URL="http://localhost:$PORT"

open_browser() {
  sleep 1.5
  if command -v xdg-open &>/dev/null; then
    xdg-open "$URL" &>/dev/null &
  elif command -v gnome-open &>/dev/null; then
    gnome-open "$URL" &>/dev/null &
  elif command -v sensible-browser &>/dev/null; then
    sensible-browser "$URL" &>/dev/null &
  fi
}

open_browser &

# ── Start server ────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Сервер запущен:${RESET}  ${CYAN}${URL}${RESET}"
echo -e "  Остановить:      ${YELLOW}Ctrl + C${RESET}"
echo ""

# Build launch command
LAUNCH_ARGS=()
if [[ -n "$FOLDER" ]]; then
  LAUNCH_ARGS+=("$FOLDER")
fi

# Use run.py if folder given (it pre-loads), else start uvicorn directly
if [[ ${#LAUNCH_ARGS[@]} -gt 0 ]]; then
  exec "$PYTHON" run.py "${LAUNCH_ARGS[@]}" --port "$PORT" 2>/dev/null || \
  exec "$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --log-level warning
else
  exec "$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --log-level warning
fi
