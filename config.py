import os

from dotenv import load_dotenv

load_dotenv()


def _read_env(name: str, *, required: bool = False, aliases: tuple[str, ...] = ()) -> str | None:
    value = os.getenv(name)
    if value is None:
        for alias in aliases:
            value = os.getenv(alias)
            if value is not None:
                break
    if value is not None:
        value = value.strip()
    if not value:
        if required:
            raise RuntimeError(f"Required environment variable {name} is not set.")
        return None
    return value


def _read_int_env(name: str, *, required: bool = False, aliases: tuple[str, ...] = ()) -> int | None:
    value = _read_env(name, required=required, aliases=aliases)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


BOT_TOKEN = _read_env("BOT_TOKEN", required=True)
DATABASE_URL = _read_env("DATABASE_URL", required=True)
ADMIN_ID = _read_int_env("ADMIN_ID", required=True, aliases=("admin_id",))
CUSTOM_API_URL = _read_env("CUSTOM_API_URL", required=True, aliases=("custom_api_url",))
MEASUREMENT_ID = _read_env("MEASUREMENT_ID")
API_SECRET = _read_env("API_SECRET")
CHANNEL_ID = _read_env("CHANNEL_ID")
OUTPUT_DIR = "downloads"
COBALT_API_URL = _read_env("COBALT_API_URL")
COBALT_API_KEY = _read_env("COBALT_API_KEY")

BOT_COMMANDS = [
    {"command": "start", "description": "Get started"},
    {"command": "settings", "description": "Settings"},
    {"command": "stats", "description": "Statistics"},
]
ADMINS_UID = [ADMIN_ID]
