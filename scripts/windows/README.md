# Windows Launcher Foundation (Planned)

Текущий production launcher реализован как `start-webapp.sh` (Linux-first).

Для Windows в следующем инкременте нужно добавить `start-webapp.ps1` с эквивалентным поведением:

- команды: `start`, `stop`, `restart`, `status`;
- отдельные процессы:
  - API (`uvicorn main:app`),
  - Python rescan worker (`python worker.py`),
  - Rust thumb worker (prebuilt `.exe` или `cargo run --release`);
- pid/state файлы (или ProcessId registry в `.run`);
- отдельные log файлы в `.logs`;
- поддержка параметров:
  - `-BuildRust`,
  - `-RustBinPath <path>`.

## Требования к parity

- Контракты API не отличаются между Linux/Windows.
- `IMGVIEWER_THUMB_JOB_MODE=queue` включает Rust thumb очередь одинаково.
- Поведение `/thumb/{id}` (`200`/`202`) полностью идентично.

## Минимальные зависимости

- PowerShell 7+
- Python 3.9+
- Rust toolchain (опционально, если нет prebuilt бинарника)
