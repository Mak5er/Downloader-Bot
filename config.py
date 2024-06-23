import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
INST_LOGIN = str(os.getenv("INST_LOGIN"))
INST_PASS = str(os.getenv("INST_PASS"))
db_auth = str(os.getenv("db_auth"))
admin_id = int(os.getenv("admin_id"))
OUTPUT_DIR = "downloads"

BOT_COMMANDS = [
    {'command': 'start', 'description': '🚀Початок роботи / Get started 🔥'},
]

ADMINS_UID = [admin_id]
