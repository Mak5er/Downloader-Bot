import os

from dotenv import load_dotenv

load_dotenv()


def _read_env(name: str, *, required: bool = False) -> str | None:
    value = os.getenv(name)
    if value is not None:
        value = value.strip()
    if not value:
        if required:
            raise RuntimeError(f"Required environment variable {name} is not set.")
        return None
    return value


def _read_int_env(name: str, *, required: bool = False) -> int | None:
    value = _read_env(name, required=required)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


BOT_TOKEN = _read_env("BOT_TOKEN", required=True)
DATABASE_URL = _read_env("DATABASE_URL", required=True)
ADMIN_ID = _read_int_env("admin_id", required=True)
CUSTOM_API_URL = _read_env("custom_api_url", required=True)
MEASUREMENT_ID = _read_env("MEASUREMENT_ID")
API_SECRET = _read_env("API_SECRET")
CHANNEL_ID = _read_env("CHANNEL_ID")
OUTPUT_DIR = "downloads"
COBALT_API_URL = _read_env("COBALT_API_URL")
COBALT_API_KEY = _read_env("COBALT_API_KEY")

# Backward-compatible aliases while the rest of the codebase still imports these names.
admin_id = ADMIN_ID
custom_api_url = CUSTOM_API_URL

BOT_COMMANDS = [
    {"command": "start", "description": "Get started"},
    {"command": "settings", "description": "Settings"},
    {"command": "stats", "description": "Statistics"},
]
ADMINS_UID = [ADMIN_ID]
