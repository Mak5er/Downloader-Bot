import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
db_auth = str(os.getenv("db_auth"))
admin_id = int(os.getenv("admin_id"))
custom_api_url = str(os.getenv("custom_api_url"))
MEASUREMENT_ID = str(os.getenv("MEASUREMENT_ID"))
API_SECRET = str(os.getenv("API_SECRET"))
CHANNEL_ID = str(os.getenv("CHANNEL_ID"))
OUTPUT_DIR = "downloads"
INSTAGRAM_RAPID_API_HOST = str(os.getenv("INSTAGRAM_RAPID_API_HOST"))
INSTAGRAM_RAPID_API_KEY1 = str(os.getenv("INSTAGRAM_RAPID_API_KEY1"))
INSTAGRAM_RAPID_API_KEY2 = str(os.getenv("INSTAGRAM_RAPID_API_KEY2"))


BOT_COMMANDS = [
    {'command': 'start', 'description': '🚀Початок роботи / Get started🔥'},
    {'command': 'settings', 'description': '⚙️Налаштування / Settings🛠'},
    {'command': 'stats', 'description': '📊Статистика / Statistics📈'},
]

ADMINS_UID = [admin_id]
