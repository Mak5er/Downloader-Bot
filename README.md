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

# Optional YouTube access helpers for yt-dlp
YTDLP_YOUTUBE_COOKIES_FILE=cookies/youtube.txt
# Alternative to a cookie file, useful only when running on the same machine as the browser:
# YTDLP_YOUTUBE_COOKIES_FROM_BROWSER=firefox:Profile 1
# YTDLP_YOUTUBE_REMOTE_COMPONENTS=ejs:github
# YTDLP_YOUTUBE_PLAYER_CLIENT=web,android
# YTDLP_YOUTUBE_PO_TOKEN=web.gvs+your_token

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

# Download queue tuning
DOWNLOAD_QUEUE_MIN_WORKERS=3
DOWNLOAD_QUEUE_MAX_WORKERS=7
DOWNLOAD_QUEUE_MAX_SIZE=250
DOWNLOAD_QUEUE_PER_USER_RATE_LIMIT=4
DOWNLOAD_QUEUE_PER_USER_WINDOW_SECONDS=10
DOWNLOAD_QUEUE_PER_USER_MAX_PENDING=3
DOWNLOAD_QUEUE_PER_USER_PENDING_TIMEOUT_SECONDS=0
DOWNLOAD_QUEUE_SCALE_COOLDOWN_SECONDS=8
DOWNLOAD_QUEUE_IDLE_SCALE_DOWN_SECONDS=35
DOWNLOAD_MAX_WORKERS_CAP=6

# Batch link tuning
BATCH_LINKS_MAX_ITEMS=6
BATCH_LINKS_MIN_CONCURRENCY=1
BATCH_LINKS_MAX_CONCURRENCY=2
BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD=2
BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD=3
```

### 4. Run locally

```bash
python main.py
```

On startup the bot initializes the database schema via Alembic migrations automatically.

## YouTube Cookies

Some YouTube videos need an authenticated browser session for `yt-dlp` to work correctly. The bot automatically uses `cookies/youtube.txt` when that file exists. You can also override the path with `YTDLP_YOUTUBE_COOKIES_FILE`.

Recommended setup:

1. Install a browser extension that exports cookies in Netscape format, for example "Get cookies.txt LOCALLY".
2. Open `youtube.com` in the browser profile that has a normal signed-in YouTube session.
3. Export only YouTube cookies for `.youtube.com` / `youtube.com` in Netscape `cookies.txt` format.
4. Put the exported file at `cookies/youtube.txt`.
5. Keep `YTDLP_YOUTUBE_COOKIES_FILE=cookies/youtube.txt` in `.env`, or omit it and let the default path be used.
6. If `yt-dlp` logs `n challenge solving failed` or only shows storyboard/image formats, set `YTDLP_YOUTUBE_REMOTE_COMPONENTS=ejs:github`.
7. Restart the bot after replacing cookies or changing YouTube `yt-dlp` options.

The `cookies` directory is kept in git with `cookies/.gitkeep`, but real cookie files are ignored by git and Docker builds. Treat `cookies/youtube.txt` like a password: do not commit it, paste it in chats, or bake it into images.

For Docker Compose, the repository mounts `./cookies` into the container as `/app/cookies:ro`, so the same `cookies/youtube.txt` path works inside the container.

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

## Performance Profile

For a 4-core i5-7500, 16 GB RAM, and gigabit network, start with:

```env
BOT_POLLING_TASKS_CONCURRENCY_LIMIT=192
BOT_SESSION_CONNECTION_LIMIT=300
DB_POOL_SIZE=24
DB_MAX_OVERFLOW=48
DB_POOL_TIMEOUT=30

DOWNLOAD_QUEUE_MIN_WORKERS=3
DOWNLOAD_QUEUE_MAX_WORKERS=7
DOWNLOAD_QUEUE_MAX_SIZE=250
DOWNLOAD_QUEUE_PER_USER_RATE_LIMIT=4
DOWNLOAD_QUEUE_PER_USER_WINDOW_SECONDS=10
DOWNLOAD_QUEUE_PER_USER_MAX_PENDING=3
DOWNLOAD_QUEUE_PER_USER_PENDING_TIMEOUT_SECONDS=0
DOWNLOAD_QUEUE_SCALE_COOLDOWN_SECONDS=8
DOWNLOAD_QUEUE_IDLE_SCALE_DOWN_SECONDS=35
DOWNLOAD_MAX_WORKERS_CAP=6

BATCH_LINKS_MAX_ITEMS=6
BATCH_LINKS_MIN_CONCURRENCY=1
BATCH_LINKS_MAX_CONCURRENCY=2
BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD=2
BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD=3
```

This keeps CPU-heavy downloader work bounded while still letting the queue scale up under real demand. Batch links run with limited parallelism only when the queue is healthy; under load they fall back to sequential processing automatically.

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

## Suggested Improvements

- Add a small health/readiness endpoint or admin health command that checks PostgreSQL connectivity, Telegram Bot API reachability, queue depth, and downloader worker status.
- Expand platform regression tests with saved extractor fixtures for TikTok, Instagram, YouTube, SoundCloud, Pinterest, and X/Twitter so platform-specific breakages are easier to catch before deployment.

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

## Support the Project

If Downloader-Bot saves you time, you can support ongoing development and infrastructure costs:

- PayPal: [Donate](https://www.paypal.com/donate/?hosted_button_id=98QRTC2HFRA4Y)
- TRC20: `TS4Ktovpwz9n2Ws8q9YXC3npW8gXi4QyYi`
- BEP20: `0xE8F613484f84F1B70A777325771d3A3Ca33979Ab`
- Solana: `8pfgWjfvDUpmeszVXbRzbifFzUDzeNeGWuJf6HCcjAF7`
- ERC20: `0x9b38804F07A4ca4381a6Ef7F0022a3F4caBc5b6F`
- TON: `UQBm9KPhtMw-XVVjirUoa09wzrlyWsbeZhKfefl1Uw-qNZ-r`

## License

Released under the [MIT License](./LICENCE).
