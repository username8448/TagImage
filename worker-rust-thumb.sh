#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/rust/thumb-worker"

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found. Install Rust toolchain first: https://rustup.rs"
  exit 1
fi

exec cargo run --release
