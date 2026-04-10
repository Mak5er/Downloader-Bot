![Downloader-Bot](https://socialify.git.ci/Mak5er/Downloader-Bot/image?description=1&language=1&name=1&owner=1&theme=Dark)

# Python Downloader-Bot

This code is a Python Telegram Bot for downloading content from social media.

### Functionality

- Downloading media from Tik-Tok, Twitter, YouTube, Pinterest, SoundCloud and Instagram.
- Admin functionality for viewing user information and sending messages to all users.
- Managing user bans and unbans.

### Installation

Clone the repository by running the following command:

    git clone https://github.com/Mak5er/Downloader-Bot.git

Navigate to the cloned repository:

    cd Downloader-Bot

Install the required Python packages using pip:

    pip install -r requirements.txt

For local development and tests use:

    pip install -r requirements-dev.txt

Before running the script, you also need to set up your custom Telegram API node by
using [this repository](https://github.com/aiogram/telegram-bot-api).

Set up the necessary configuration by creating a  `.env`  file and defining the required variables.

Example  `.env`  file:

    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    ADMIN_ID = BOT_ADMIN_ID
    DATABASE_URL = Pgsql connection uri
    CUSTOM_API_URL = YOUR_CUSTOM_TELEGRAM_API_URL
    CHANNEL_ID = YOUR_CHANNEL_ID_FOR_INLINE_QUERY_VIDEOS
    COBALT_API_URL = YOUR_COBALT_NODE_URL
    COBALT_API_KEY = YOUR_COBALT_API_KEY
    DOWNLOAD_SUBPROCESS_THRESHOLD_MB = 0  # set >0 to run large downloads in a separate worker process

You can check how to run your own cobalt instance [HERE](https://github.com/imputnet/cobalt/blob/main/docs/run-an-instance.md) 

Run the script using Python:

    python main.py

Or using Docker with the published GHCR image:

    docker compose pull
    docker compose up -d

If you want to rebuild the container locally from the repository `Dockerfile`, use:

    docker compose up -d --build

For the GHCR image you only need `docker-compose.yml` and `.env`.
For a local build you also need the repository sources and `Dockerfile`.
You do not need to create the log or download directories manually: Docker named volumes are created automatically, and the app also creates these directories on startup when needed.

Notes about performance:

- Docker build is faster with the included `.dockerignore`, because `.venv`, tests, logs and downloads are no longer sent into the build context.
- `docker-compose.yml` now uses named volumes for `downloads` and `logs` instead of bind-mounting the whole repo, which is noticeably faster on Docker Desktop/Windows.

### Logging

- Console logs are colorized and now include `service`, `flow` and `request_id`.
- General text logs are written to `logs/bot_log.log` and `logs/error_log.log`.
- Structured JSONL logs are written to `logs/events_log.jsonl` and `logs/perf_log.jsonl`.
- Main handlers attach request-scoped logging context automatically, so one user request can be traced across fetch, queue, download and upload stages.

### Database Tables

The PostgreSQL database used by the bot includes the following tables:

- `users` : Stores user information, including user ID, username, chat type, language, status, and referrer ID.
- `downloaded_files` : Stores file_id of downloaded videos, urls, save date and tags.

### Usage

Once the bot is running, it will start listening for incoming messages and commands from users.

Commands:

- /start : Start the conversation with the bot.
- /setting : Change the bot settings.
- /stats : View bot statistics.
- /remove_keyboard : Remove reply keyboard.
- /perf : (admin) queue performance p50/p95 per platform.
- /session : (admin) current bot-session downloads and traffic.

### Performance Queue

- Heavy downloads now run through a shared priority queue with per-user rate limiting.
- Queue workers auto-scale based on real load (queue wait and backlog).
- Progress messages include percent, speed and ETA when source supports streaming progress.
- Optional worker-process mode: set `DOWNLOAD_SUBPROCESS_THRESHOLD_MB` to move large downloads into a separate process.

### Telegram Bot Link

You can access the Telegram bot by clicking [here](https://t.me/MaxLoadBot).

### Contributions

Contributions to this project are welcome. If you encounter any issues or have suggestions for improvements, please open
an issue or submit a pull request.

### License

This code is licensed under the [MIT License](https://opensource.org/licenses/MIT).

Feel free to modify and use this code for your own Telegram bot projects.
