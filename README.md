# ImgViewer

Локальная галерея для просмотра и тегирования изображений. Проект работает как локальное приложение: файлы не загружаются наружу, а данные хранятся в PostgreSQL.

## Быстрый Старт

1. Поднять PostgreSQL:

```bash
docker compose up -d postgres
```

2. Установить зависимости:

```bash
pip install -r requirements.txt
npm install
```

3. Запустить:

```bash
./start.sh /путь/к/фото
# или
./start.sh
```

`./start.sh` проверит frontend-зависимости и соберет TS bundle перед запуском. Если запускаешь backend напрямую через `python run.py`, сначала выполни `npm run build:frontend`.

По умолчанию используется:

```bash
DATABASE_URL=postgresql://imgviewer:imgviewer@127.0.0.1:5432/imgviewer
```

Можно скопировать `.env.example` в `.env`; `start.sh` подхватит его автоматически.

### Development DB options

A) Docker PostgreSQL (optional dev tooling):

```bash
docker compose up -d postgres
```

B) Native local PostgreSQL (without Docker):

```bash
./scripts/local-postgres.sh init
./scripts/local-postgres.sh start
./scripts/local-postgres.sh url
```

Use `DATABASE_URL` in `.env` for the selected DB backend. Example for native mode:

```bash
DATABASE_URL=postgresql://imgviewer:imgviewer@127.0.0.1:55432/imgviewer
```

`.env` must not be committed.
Docker is optional for development, and packaged Tauri runtime must not depend on Docker.

### Database troubleshooting

Быстрая диагностика:

```bash
./scripts/check-db.sh
```

Безопасное восстановление контейнера PostgreSQL (без удаления volume):

```bash
./scripts/repair-db.sh
```

Полезные команды:

```bash
docker compose up -d postgres
./scripts/check-db.sh
```

`repair-db.sh` не удаляет volume и не выполняет `docker compose down -v`.
`docker compose down -v` удаляет данные БД и должен запускаться только по явному решению пользователя.

### Tauri direction note

Текущий Docker/PostgreSQL путь нужен для разработки и тестов.
Целевой packaged runtime для Tauri не должен зависеть от Docker.
План и ограничения: `docs/tauri-migration-plan.md`.

## Возможности

| Функция | Описание |
|---|---|
| Галерея | Masonry-сетка с миниатюрами в исходных пропорциях |
| PostgreSQL | Изображения, теги и сессия хранятся в БД |
| Авто-теги папок | Все родительские папки изображения становятся тегами |
| Ручные теги | Добавляются в canvas-preview и сохраняются между перезапусками |
| Поиск | Одно поле: `tag` добавляет обычный тег, `!tag` или `-tag` добавляет анти-тег |
| Режимы | `Любой` показывает фото хотя бы с одним тегом, `Все` требует все выбранные теги |
| Вкладки | Каждая вкладка хранит include/exclude теги, режим совпадения и последнее фото |
| Сессия | Запоминаются папка, вкладки, фильтры и открытое изображение |
| PreviewModal | Canvas-просмотрщик с нижним dock, zoom/pan, навигацией и тегами |
| Пересканирование | Новые файлы добавляются, пропавшие скрываются, ручные теги сохраняются |

## Файлы

```
выбранная_папка/
├── .imgindex/
│   └── thumbs/         # сгенерированные миниатюры
└── ваши фото...
```

`index.json` больше не создается и не обновляется. Источник истины теперь PostgreSQL.

## API

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/folder` | Установить рабочую папку и запустить сканирование |
| `GET` | `/api/images` | Список изображений; поддерживает `include_tags`, `exclude_tags`, `match_mode` |
| `GET` | `/api/tags` | Весь созданный пул тегов |
| `POST` | `/api/tags` | Создать тег без привязки к фото |
| `POST` | `/api/tag/{id}` | Сохранить ручные теги изображения |
| `GET` | `/api/session` | Получить локальную сессию |
| `PATCH` | `/api/session` | Обновить вкладки, фильтры, папку или последнее изображение |
| `GET` | `/thumb-file/{id}.jpg` | Быстрый путь готовой миниатюры без DB lookup |
| `GET` | `/thumb/{id}` | Миниатюра (`200` если готова, `202` если в очереди) |
| `GET` | `/file/{id}` | Оригинальный файл |
| `POST` | `/api/rescan` | Поставить пересканирование в очередь, вернуть `job_id` |
| `POST` | `/api/thumbs/rebuild` | Поставить генерацию миниатюр в очередь (`thumb` jobs) |
| `GET` | `/api/status` | Статус сканирования, подключения и блоки `workers/queues` |
| `GET` | `/api/jobs/{job_id}` | Статус задачи очереди |
| `GET` | `/api/jobs?type=rescan&state=running` | Список задач по фильтрам |

## Локальная Модель

Приложение не содержит регистрации и не рассчитано на публикацию как сайт. Это локальный Python/FastAPI backend с HTML UI, подготовленный к будущей упаковке в Tauri.

## Worker

Очередь задач хранится в PostgreSQL (`jobs`, `job_attempts`, `job_events`).

Запуск отдельного воркера:

```bash
python worker.py
```

Для вынесения тяжелой генерации миниатюр в очередь включите режим:

```bash
export IMGVIEWER_THUMB_JOB_MODE=queue
```

После этого `rescan` будет enqueue-ить `thumb` задачи вместо синхронной генерации.

`GET /thumb/{id}` в queue-режиме:

- `200 image/jpeg`, если файл уже готов;
- `202 application/json`, если миниатюра еще обрабатывается:

```json
{
  "ok": false,
  "pending": true,
  "job_id": "....",
  "retry_after_ms": 120,
  "thumb_url": "/thumb/<id>"
}
```

`POST /api/thumbs/rebuild` теперь дополнительно возвращает `queued_existing` (сколько задач не создали новый job из-за dedupe).

Галерея использует `/thumb-file/{id}.jpg` для готовых миниатюр и обращается к `/thumb/{id}` только как fallback, если файл еще не создан.

По умолчанию в API включен inline fallback-воркер (`IMGVIEWER_INLINE_WORKER=1`), чтобы приложение работало даже без отдельного процесса.

Для строгого режима с отдельным воркером:

```bash
IMGVIEWER_INLINE_WORKER=0 python run.py
```

### Rust thumb worker

Отдельный Rust-воркер для `thumb` задач находится в `rust/thumb-worker`.

Обычный запуск теперь через основной launcher:

```bash
./start.sh start --build-rust --strict-rust
# или
IMGVIEWER_THUMB_JOB_MODE=queue ./start.sh start
```

`worker-rust-thumb.sh` оставлен как ручной debug helper для запуска только Rust thumb worker.

## Unified Launcher (non-Docker)

Основной launcher проекта:

```bash
./start.sh
./start.sh /path/to/images
./start.sh start
./start.sh stop
./start.sh restart
./start.sh status
./start.sh logs
./start.sh logs api
./start.sh logs rescan
./start.sh logs thumb
./start.sh foreground
```

Проверка Rust thumbnails в строгом режиме:

```bash
./start.sh start --build-rust --strict-rust
```

Скрипт поднимает:

- API (`uvicorn`)
- Python rescan worker
- Rust thumb worker (в режиме `IMGVIEWER_THUMB_JOB_MODE=queue`, если доступен бинарник/cargo)

Логи: `.logs/`, pid-файлы: `.run/`.

`start-webapp.sh` теперь deprecated compatibility wrapper, который просто прокидывает аргументы в `./start.sh`.

## Runtime env

- `IMGVIEWER_THUMB_WAIT_MS` (default `1200`)
- `IMGVIEWER_THUMB_POLL_MS` (default `120`)
- `IMGVIEWER_THUMB_SYNC_FALLBACK` (default `0`)
- `IMGVIEWER_JOB_TTL_HOURS` (default `168`)
- `IMGVIEWER_JOB_CLEANUP_INTERVAL_SEC` (default `3600`)
- `IMGVIEWER_THUMB_MAX_ATTEMPTS` (default `5`)
- `IMGVIEWER_RESCAN_MAX_ATTEMPTS` (default `3`)
- `IMGVIEWER_THUMB_MAX_BACKOFF_SEC` (default `300`)
- `IMGVIEWER_RESCAN_MAX_BACKOFF_SEC` (default `120`)

## Performance Check

Повторяемый замер списка и миниатюр:

```bash
BASE_URL=http://127.0.0.1:8000 ./scripts/bench-images.sh
```

`/api/images` возвращает `elapsed_ms` и `Server-Timing`. Совместимый `/thumb/{id}` возвращает `X-Elapsed-Ms` и `X-Db-Lookup-Ms`.

## Frontend Build

Фронт собирается из TS/CSS исходников в `static/src/`. JS-бандл создается локально как `static/dist/app.js` и не хранится в git.

```bash
npm install
npm run typecheck:frontend
npm run build:frontend
```

`./start.sh` перед запуском backend проверяет наличие npm/node_modules и собирает frontend. Скрипт не запускает `npm install` автоматически: для офлайн-запуска зависимости должны быть установлены заранее.

## Safe Tests

Рекомендуемый запуск интеграционных тестов:

```bash
TEST_DATABASE_URL=postgresql://imgviewer:imgviewer@127.0.0.1:5432/tagimage_test pytest
```

Тесты поддерживают `TEST_DATABASE_URL` и в тестовом контексте используют его вместо обычного `DATABASE_URL`.
Без `TEST_DATABASE_URL` включен безопасный fallback: интеграционные тесты восстанавливают `app_session` и чистят только test-данные (`/tmp/pytest-*`, `pytest-*`), чтобы не оставлять временные пути в рабочем состоянии приложения.
