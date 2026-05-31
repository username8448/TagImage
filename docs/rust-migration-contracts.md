# Rust Migration Contracts (Python Backend ↔ Workers)

## 1) Python Role
Python backend remains the API and orchestration layer. The Python implementation is the reference implementation for behavior while Rust workers are introduced gradually.

## 2) Integration Boundary
PostgreSQL jobs queue is the boundary between Python and Rust workers.

## 3) Public HTTP API Invariant
Rust workers must not change public HTTP API behavior or endpoints.

## 4) Rust Workspace Structure

```text
rust/
  Cargo.toml
  thumb-worker/
  crates/
    tagimage-core/
```

The existing `rust/thumb-worker/Cargo.toml` path remains supported for direct thumb worker builds.

## 5) tagimage-core Responsibility
`tagimage-core` contains shared Rust contracts that are safe to reuse across future workers:

- shared payload contracts
- shared job enums
- shared env helpers

`tagimage-core` must not contain DB access, job queue mutation, HTTP API code, worker loops, or PostgreSQL queries.

## 6) ThumbJobPayload Contract
Thumb job payload fields are fixed:

- `image_id`
- `root_path`
- `path`
- `thumb`
- `mtime`
- `max_size`

`max_size` remains a JSON array/list of two numbers.

Python-created thumb jobs must include all fields.

Rust currently tolerates legacy/missing `image_id`, `mtime`, and `max_size` only for runtime compatibility with existing jobs.

Future Rust workers must create complete ThumbJobPayload values with all fields present.

## 7) Thumb Dedupe Key Invariant
Thumb dedupe key format is fixed:

- `thumb:{image_id}:{mtime}`

## 8) RescanJobPayload Contract
Rescan job payload fields are fixed:

- `root_path`

## 9) Python Reference Implementation Rule
`app/services/scanner.py` is the reference implementation for any future Rust scanner. It must not be deleted during migration.

A future Rust scanner must prove parity with at least:

- `scan_image_paths` ordering
- supported extensions behavior
- `INDEX_DIR_NAME` exclusion
- image metadata extraction
- `upsert_image_row`-compatible data
- hidden/missing behavior
- auto tag behavior
- thumbnail job enqueue behavior
- progress updates
- job success/failure behavior

Rust scanner code cannot replace the Python path until compatibility tests exist, runtime validation exists, Python fallback remains available, and the public API remains compatible.

## 10) Migration Order

1. `tagimage-core`
2. `tagimage-db`
3. `metadata-worker`
4. `scanner-worker`
5. `hash-worker`
6. Rust API

## 11) What Must Not Be Renamed

- payload fields
- env variables
- job type names
- public HTTP endpoints

## 12) Important Env Variables

- `IMGVIEWER_THUMB_JOB_MODE`
- `IMGVIEWER_THUMB_SYNC_FALLBACK`
- `IMGVIEWER_INLINE_WORKER`
- `IMGVIEWER_THUMB_WAIT_MS`
- `IMGVIEWER_THUMB_POLL_MS`

## 13) Future Rust Migration Candidates

- thumbnail generation
- metadata reading
- scanner/rescan
- hash/dedup

## 14) Parts That Stay in Python for Now

- FastAPI
- session
- tags
- file serving
- job orchestration
- scanner/rescan reference behavior

## 15) Explicit Prohibition
Do not delete Python implementation until:

- Rust implementation has parity tests
- Rust implementation has runtime validation
- Python fallback remains available
- public HTTP API remains compatible

## 16) Contract Priority
If migration implementation conflicts with this document, contracts above take priority.
