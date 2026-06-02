# Rust-first Runtime Profile

TagImage can run as a Rust-first development profile, but this profile is still opt-in.
Python remains the reference implementation and fallback until the final legacy cleanup decision.

## Default/dev fallback

Default `./start.sh` keeps the Python API and Python scanner authoritative.
Rust components run only when their flags are enabled.

Use this mode when checking Python reference behavior or when bisecting Rust parity issues.

```bash
IMGVIEWER_RUST_API=0 IMGVIEWER_RUST_SCANNER=0 ./start.sh start --build-rust --strict-rust --no-open
```

## Rust-first dev profile

Enable Rust API, Rust scanner, and queued Rust thumbnails explicitly through `.env` or shell env vars:

```bash
IMGVIEWER_RUST_API=1
IMGVIEWER_RUST_SCANNER=1
IMGVIEWER_THUMB_JOB_MODE=queue
IMGVIEWER_INLINE_WORKER=0
IMGVIEWER_THUMB_SYNC_FALLBACK=0
IMGVIEWER_THUMB_WORKERS=4
```

Then run:

```bash
./start.sh start --build-rust --strict-rust --no-open
./start.sh status
```

This is not hardcoded as the default. To return to Python fallback, set:

```bash
IMGVIEWER_RUST_API=0
IMGVIEWER_RUST_SCANNER=0
```

## Optional metadata shadow validation

`IMGVIEWER_METADATA_WORKER=1` enables the Rust metadata worker as an optional shadow validation tool.
It is not part of the required Rust-first runtime yet, and metadata is not authoritative.

```bash
IMGVIEWER_METADATA_WORKER=1 ./start.sh start --build-rust --strict-rust --no-open
./start.sh logs metadata
```

This may add background database and filesystem load.

## Cleanup status

Do not remove the Python API or Python scanner yet.
Stage 7 cleanup must wait until metadata is authoritative and parity/runtime checks are green.
