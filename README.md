![Downloader-Bot](https://socialify.git.ci/Mak5er/Downloader-Bot/image?description=1&language=1&name=1&owner=1&theme=Dark)

# Python Downloader-Bot

This code is a Python Telegram Bot for downloading content from social media.

### Functionality

- Downloading media from Tik-Tok, Twitter, YouTube and ~~Instagram~~(Currently not working well).
- Admin functionality for viewing user information and sending messages to all users.
- Managing user bans and unbans.

### Installation

Clone the repository by running the following command:

    git clone https://github.com/Mak5er/Downloader-Bot.git

Navigate to the cloned repository:

    cd Downloader-Bot

Install the required Python packages using pip:

    pip install -r requirements.txt

Before running the script, you also need to set up your custom Telegram API node by using [this repository](https://github.com/aiogram/telegram-bot-api). 

Set up the necessary configuration by creating a  `.env`  file and defining the required variables.

Example  `.env`  file:

    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    db_auth = DATABASE_CONNECT_URL
    admin_id = BOT_ADMIN_ID
    custom_api_url = YOUR_CUSTOM_TELEGRAM_API_URL
    INSTAGRAM_RAPID_API_HOST = INSTAGRAM_RAPID_API_HOST
    INSTAGRAM_RAPID_API_KEY = INSTAGRAM_RAPID_API_KEY
    CHANNEL_ID = Channel_For_Inline_Query_Vitedos 

Api keys can be obtained from [RapidAPI](https://rapidapi.com/social-api1-instagram/api/Instagram%20Scraper%20API).

Run the script using Python:

    python main.py

Or using Docker:

    docker compose up -d

To use your local image, update `docker-compose.yml` to:

```yaml
services:
  downloader-bot:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - .:/app
    container_name: downloader-bot
    restart: always
```

### Database Tables

The PostgreSQL database used by the bot includes the following tables:

- `users` : Stores user information, including user ID, username, chat type, language, status, and referrer ID.
- `downloaded_files` : Stores file_id of downloaded videos, urls, save date and tags.

### Usage

Once the bot is running, it will start listening for incoming messages and commands from users. 

Commands:

- /start : Start the conversation with the bot.
- /setting : Change the bot settings.

Users can also send feedback and the bot will provide answers.

### Telegram Bot Link

You can access the Telegram bot by clicking [here](https://t.me/MaxLoadBot).

### Contributions

Contributions to this project are welcome. If you encounter any issues or have suggestions for improvements, please open
an issue or submit a pull request.

### License

This code is licensed under the [MIT License](https://opensource.org/licenses/MIT).

Feel free to modify and use this code for your own Telegram bot projects.
