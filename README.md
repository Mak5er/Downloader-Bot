![Downloader-Bot](https://socialify.git.ci/Mak5er/Downloader-Bot/image?description=1&language=1&name=1&owner=1&theme=Dark)

# Python Downloader-Bot

This code is a Python Telegram Bot for downloading content from social media.

### Functionality

- Downloading media from Tik-Tok, Twitter, YouTube and Instagram.
- Admin functionality for viewing user information and sending messages to all users.
- Managing user bans and unbans.

### Installation

Clone the repository by running the following command:

    git clone https://github.com/Mak5er/Downloader-Bot.git

Navigate to the cloned repository:

    cd Downloader-Bot

Install the required Python packages using pip:

    pip install -r requirements.txt

Set up the necessary configuration by creating a  `.env`  file and defining the required variables.

Example  `.env`  file:

    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    INST_LOGIN = INSTAGRAM_LOGIN
    INST_PASS = INSTAGRAM_PASSWORD
    db_auth = DATABASE_CONNECT_URL
    admin_id = BOT_ADMIN_ID
    custom_api_url = YOUR_CUSTOM_TELEGRAM_API_URL


Run the script using Python:

    python main.py

Or using Docker:

    docker compose up -d

To use your local image, update `docker-compose.yml` to:

```yaml
version: '3.9'

services:
  downloader-bot:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - .:/app
    container_name: downloader-bot
    ports:
      - '4040:4040'
    restart: always
```

### Database Tables

The PostgreSQL database used by the bot includes the following tables:

- `users` : Stores user information, including user ID, username, chat type, language, status, and referrer ID.
- `downloaded_files` : Stores file_id of downloaded videos.
  and tags.

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
