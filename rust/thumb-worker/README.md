# Rust Thumb Worker

Обрабатывает задачи типа `thumb` из таблицы `jobs` (PostgreSQL), генерирует JPEG миниатюры и обновляет статусы/ретраи.

## Запуск

```bash
cd rust/thumb-worker
cargo run --release
```

Или из корня проекта:

```bash
./worker-rust-thumb.sh
```

## Переменные среды

- `DATABASE_URL` — строка подключения к PostgreSQL.
- `IMGVIEWER_THUMB_WORKER_POLL_MS` — интервал опроса очереди в мс (default `750`).
- `IMGVIEWER_THUMB_WORKER_ID` — идентификатор воркера (optional).
- `IMGVIEWER_THUMB_MAX_BACKOFF_SEC` — верхняя граница retry backoff (default `300`).

## Интеграция с API

В Python включите enqueue режима миниатюр:

```bash
export IMGVIEWER_THUMB_JOB_MODE=queue
```

Тогда `rescan` будет ставить задачи `thumb` в очередь вместо синхронной генерации.
