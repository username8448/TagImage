#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "start-webapp.sh is deprecated; use ./start.sh instead." >&2
exec "$SCRIPT_DIR/start.sh" "$@"
