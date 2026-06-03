# SQLite Runtime Cutover Notes

SQLite schema, isolated init, jobs queue helpers, and runtime-parity DB helpers now live in `rust/crates/tagimage-db`.

Current runtime is still PostgreSQL through native local PostgreSQL at `127.0.0.1:55432`. This is temporary legacy runtime state while SQLite parity is prepared.

Implemented in this stage:

- SQLite health/init/table verification helpers.
- SQLite image, tag, folder tree, session, and pagination helpers.
- SQLite runtime-facing jobs wrappers on top of the existing queue semantics.
- SQLite metadata/thumb/rescan source and enqueue helpers.

Not done in this stage:

- No runtime cutover.
- No runtime backend switch.
- No Docker fallback.
- No frontend, Tauri, API, or worker wiring changes.

Next stage: directly cut over API, workers, and start scripts to file-based SQLite as the only runtime DB.
