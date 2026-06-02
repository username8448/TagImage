# Tauri Migration Plan

This document defines architecture guardrails for moving TagImage toward a Tauri desktop app without breaking current behavior.

## 1. Target architecture

Target shape:

```text
Tauri shell
  -> frontend UI
  -> local backend sidecar / Rust backend
  -> local DB
  -> Rust workers for thumbnails/metadata/scanner/hash
```

The migration is incremental. Current Python and PostgreSQL development mode stays valid until parity is proven.

## 2. Runtime rule

- PostgreSQL may remain in current development/tests.
- Packaged desktop runtime should not require user-installed PostgreSQL.

## 3. Database direction

Current DB:

- PostgreSQL

Target desktop DB:

- SQLite (preferred for packaged Tauri runtime)

Rationale:

- single local file
- easier packaging
- no server process
- no external database server requirement
- suitable for local image index/cache/session/jobs

Important constraint:

- Do not migrate DB immediately.
- First define compatibility layer and a migration path.

## 4. Sidecar strategy

Tauri can run packaged sidecar binaries. Candidate sidecars:

- `tagimage-backend`
- `tagimage-thumb-worker`
- `tagimage-metadata-worker`
- `tagimage-scanner-worker`

Current stage keeps existing `start.sh` development mode.

## 5. Migration order

1. Keep current Python/PostgreSQL dev mode stable.
2. Continue Rust workers on PostgreSQL while behavior is validated.
3. Add DB abstraction and migration plan.
4. Introduce SQLite schema equivalent.
5. Add SQLite support to Rust workers.
6. Build Rust backend/API compatible with current FastAPI endpoints.
7. Add Tauri shell.
8. Package Rust backend/workers as sidecars or integrate into `src-tauri`.
9. Keep Python path as dev/reference fallback until Rust parity is proven.
10. Remove Python only after tests and runtime validation.

## 6. What stays reference

`app/services/scanner.py` remains reference implementation until Rust scanner has:

- parity tests
- runtime validation
- fallback path
- same public API behavior

## 7. What not to do

- Do not require PostgreSQL in packaged app.
- Do not delete Python until Rust parity is proven.
- Do not rewrite scanner from scratch without matching Python behavior.
- Do not expose DB directly to frontend as the primary architecture unless explicitly decided.
- Do not change public API endpoints during migration.

## 8. Development modes

Mode A: current dev mode

- `start.sh`
- native local PostgreSQL
- Python API
- Rust workers

Mode B: Rust migration dev mode

- Python API
- native local PostgreSQL
- Rust thumb/metadata workers

Mode C: future Tauri mode

- Tauri shell
- Rust backend/sidecar
- SQLite
- Rust workers

## 9. Immediate next steps

1. Finish metadata-worker runtime validation when DB is stable.
2. Add metadata parity comparison against Python scanner output.
3. Plan SQLite schema compatibility.
4. Add DB backend abstraction decision doc.
5. Only then start Tauri shell implementation.

## 10. Contract priority

If a migration shortcut conflicts with behavior parity, parity wins:

- Keep current API behavior stable.
- Keep Python reference path available until Rust parity is verified.
- Keep native local PostgreSQL as temporary development runtime until SQLite is wired.
