# Rust Migration Contracts (Python Backend ↔ Workers)

## 1) Python Role
Python backend remains the API and orchestration layer.

## 2) Integration Boundary
PostgreSQL jobs queue is the boundary between Python and Rust workers.

## 3) Public HTTP API Invariant
Rust workers must not change public HTTP API behavior or endpoints.

## 4) ThumbJobPayload Contract
Thumb job payload fields are fixed:

- `image_id`
- `root_path`
- `path`
- `thumb`
- `mtime`
- `max_size`

## 5) Thumb Dedupe Key Invariant
Thumb dedupe key format is fixed:

- `thumb:{image_id}:{mtime}`

## 6) RescanJobPayload Contract
Rescan job payload fields are fixed:

- `root_path`

## 7) What Must Not Be Renamed

- payload fields
- env variables
- job type names
- public HTTP endpoints

## 8) Important Env Variables

- `IMGVIEWER_THUMB_JOB_MODE`
- `IMGVIEWER_THUMB_SYNC_FALLBACK`
- `IMGVIEWER_INLINE_WORKER`
- `IMGVIEWER_THUMB_WAIT_MS`
- `IMGVIEWER_THUMB_POLL_MS`

## 9) Candidates for Future Rust Migration

- thumbnail generation
- metadata reading
- scanner/rescan
- hash/dedup

## 10) Parts That Stay in Python for Now

- FastAPI
- session
- tags
- file serving
- job orchestration

## Contract Priority
If migration implementation conflicts with this document, contracts above take priority.
