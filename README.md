# Downloader-Bot

A production-oriented Telegram bot for downloading media from major social platforms, with inline delivery, PostgreSQL-backed state, structured logging, and a queue designed for real-world load.

The project is built on `aiogram 3`, uses `SQLAlchemy + Alembic` for persistence, and supports both local development and Docker-based deployment.

![Downloader-Bot](https://socialify.git.ci/Mak5er/Downloader-Bot/image?description=1&language=1&name=1&owner=1&theme=Dark)

## Features

- Download media from TikTok, Instagram, YouTube, SoundCloud, Pinterest, and X/Twitter
- Support direct bot usage and Telegram inline mode flows
- Per-user settings for captions, info buttons, URL buttons, MP3 buttons, and auto-delete
- Shared download queue with backpressure, retries, and large-download worker handoff
- PostgreSQL persistence for users, settings, analytics, and file cache
- Admin tools for runtime stats, session metrics, user management, and log access
- Structured event and performance logging for easier debugging and operations

## Stack

- Python 3.14
- aiogram 3
- PostgreSQL
- SQLAlchemy 2 + Alembic
- yt-dlp
- aiohttp / httpx
- Docker / Docker Compose
- pytest

## Supported flows

### User-facing

- `/start` to initialize the bot
- `/settings` to configure delivery behavior
- `/stats` to view usage statistics
- Inline queries for supported services where Telegram inline delivery is available

### Admin

- `/admin`
- `/perf`
- `/session`

## Architecture

The codebase is split into a few clear layers:

- [`handlers`](./handlers) contains Telegram message, callback, and inline-query flows
- [`services/platforms`](./services/platforms) contains platform-specific media extraction and download logic
- [`services/download`](./services/download) contains the shared queue and worker logic
- [`services/storage`](./services/storage) contains the database models, repositories, caching, and schema bootstrap
- [`services/runtime`](./services/runtime) contains transient runtime state such as dedupe, pending requests, and live stats
- [`middlewares`](./middlewares) contains antiflood, chat tracking, ban checks, and private-chat guard behavior
- [`tests`](./tests) covers handlers, runtime services, startup, storage, queue behavior, and deployment helpers

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/Mak5er/Downloader-Bot.git
cd Downloader-Bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

For development and tests:

```bash
pip install -r requirements-dev.txt
```

### 3. Configure environment

Create a `.env` file in the project root:

```env
BOT_TOKEN=your_telegram_bot_token
DATABASE_URL=postgresql://user:password@host:5432/dbname
ADMIN_ID=123456789
CUSTOM_API_URL=http://your-telegram-bot-api:8081

# Optional but recommended depending on your deployment/features
MEASUREMENT_ID=
API_SECRET=
CHANNEL_ID=
COBALT_API_URL=
COBALT_API_KEY=

# Performance tuning
BOT_POLLING_TASKS_CONCURRENCY_LIMIT=256
BOT_SESSION_CONNECTION_LIMIT=400
DB_POOL_SIZE=32
DB_MAX_OVERFLOW=64
DB_POOL_TIMEOUT=30

# Anti-flood and dedupe
ANTIFLOOD_MESSAGE_LIMIT=4
ANTIFLOOD_MESSAGE_WINDOW_SECONDS=2
ANTIFLOOD_CALLBACK_LIMIT=6
ANTIFLOOD_CALLBACK_WINDOW_SECONDS=2
ANTIFLOOD_INLINE_LIMIT=4
ANTIFLOOD_INLINE_WINDOW_SECONDS=3
ANTIFLOOD_GLOBAL_LIMIT=8
ANTIFLOOD_GLOBAL_WINDOW_SECONDS=3
ANTIFLOOD_COOLDOWN_SECONDS=6
ANTIFLOOD_USER_TTL_SECONDS=180
ANTIFLOOD_MAX_TRACKED_USERS=50000
REQUEST_DEDUPE_ACTIVE_TTL_SECONDS=900
REQUEST_DEDUPE_COMPLETED_TTL_SECONDS=12
REQUEST_DEDUPE_MAX_ENTRIES=50000
```

### 4. Run locally

```bash
python main.py
```

On startup the bot initializes the database schema via Alembic migrations automatically.

## Docker

The repository includes a production-friendly multi-stage [`Dockerfile`](./Dockerfile) and a minimal [`docker-compose.yml`](./docker-compose.yml).

Run with the published image:

```bash
docker compose pull
docker compose up -d
```

Or build locally:

```bash
docker compose up -d --build
```

Notes:

- Downloads and logs are stored in named Docker volumes
- The container entrypoint prepares runtime paths and drops privileges before launching the app
- `ffmpeg` is installed in the runtime image

## Logging and Observability

The bot writes both human-readable and structured logs:

- `logs/bot_log.log`
- `logs/error_log.log`
- `logs/events_log.jsonl`
- `logs/perf_log.jsonl`

The logging layer adds request-scoped context such as `service`, `flow`, and `request_id`, which makes it easier to trace a single user action across fetch, queue, download, and delivery steps.

## Development Notes

- The project expects a custom Telegram Bot API endpoint via `CUSTOM_API_URL`
- PostgreSQL is required in normal operation
- Some platform flows rely on a Cobalt-compatible backend; set `COBALT_API_URL` and `COBALT_API_KEY` as needed
- Test coverage is centered around handlers, queue/runtime behavior, database access, migrations, and startup logic

Run tests with:

```bash
pytest
```

## Contributing

Issues and pull requests are welcome. If you change runtime behavior, settings, platform handling, or deployment flow, please update the README and related tests in the same change.

## Repository Layout

```text
.
|-- handlers/
|-- services/
|   |-- download/
|   |-- inline/
|   |-- links/
|   |-- media/
|   |-- platforms/
|   |-- runtime/
|   `-- storage/
|-- middlewares/
|-- keyboards/
|-- messages/
|-- filters/
|-- tests/
|-- main.py
|-- config.py
|-- Dockerfile
`-- docker-compose.yml
```

## Bot

Public bot: [@MaxLoadBot](https://t.me/MaxLoadBot)

## License

Released under the [MIT License](./LICENCE).
