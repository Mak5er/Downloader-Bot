import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
admin_id = int(os.getenv("admin_id"))
custom_api_url = str(os.getenv("custom_api_url"))
MEASUREMENT_ID = str(os.getenv("MEASUREMENT_ID"))
API_SECRET = str(os.getenv("API_SECRET"))
CHANNEL_ID = str(os.getenv("CHANNEL_ID"))
OUTPUT_DIR = "downloads"
COBALT_API_URL = os.getenv("COBALT_API_URL")

BOT_COMMANDS = [
    {'command': 'start', 'description': '🚀 Get started'},
    {'command': 'settings', 'description': '⚙️ Settings'},
    {'command': 'stats', 'description': '📊 Statistics'},

]
ADMINS_UID = [admin_id]
