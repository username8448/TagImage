#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$SCRIPT_DIR/rust/thumb-worker"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Usage: ./worker-rust-thumb.sh

Manual debug helper for Rust thumb worker.

Regular app start should use:
  ./start.sh
or strict Rust queue check mode:
  ./start.sh start --build-rust --strict-rust
USAGE
  exit 0
fi

echo "worker-rust-thumb.sh is a manual debug helper." >&2
echo "For normal app startup use ./start.sh." >&2

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found. Install Rust toolchain first: https://rustup.rs" >&2
  exit 1
fi

cd "$WORKER_DIR"
exec cargo run --release
