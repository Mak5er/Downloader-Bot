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


def _read_float_env(name: str, *, required: bool = False, aliases: tuple[str, ...] = ()) -> float | None:
    value = _read_env(name, required=required, aliases=aliases)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a float.") from exc


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
BOT_POLLING_TASKS_CONCURRENCY_LIMIT = _read_int_env("BOT_POLLING_TASKS_CONCURRENCY_LIMIT") or 256
BOT_SESSION_CONNECTION_LIMIT = _read_int_env("BOT_SESSION_CONNECTION_LIMIT") or 400
DB_POOL_SIZE = _read_int_env("DB_POOL_SIZE") or 32
DB_MAX_OVERFLOW = _read_int_env("DB_MAX_OVERFLOW") or 64
DB_POOL_TIMEOUT = _read_float_env("DB_POOL_TIMEOUT") or 30.0
ANTIFLOOD_MESSAGE_LIMIT = _read_int_env("ANTIFLOOD_MESSAGE_LIMIT") or 4
ANTIFLOOD_MESSAGE_WINDOW_SECONDS = _read_float_env("ANTIFLOOD_MESSAGE_WINDOW_SECONDS") or 2.0
ANTIFLOOD_CALLBACK_LIMIT = _read_int_env("ANTIFLOOD_CALLBACK_LIMIT") or 6
ANTIFLOOD_CALLBACK_WINDOW_SECONDS = _read_float_env("ANTIFLOOD_CALLBACK_WINDOW_SECONDS") or 2.0
ANTIFLOOD_INLINE_LIMIT = _read_int_env("ANTIFLOOD_INLINE_LIMIT") or 4
ANTIFLOOD_INLINE_WINDOW_SECONDS = _read_float_env("ANTIFLOOD_INLINE_WINDOW_SECONDS") or 3.0
ANTIFLOOD_GLOBAL_LIMIT = _read_int_env("ANTIFLOOD_GLOBAL_LIMIT") or 8
ANTIFLOOD_GLOBAL_WINDOW_SECONDS = _read_float_env("ANTIFLOOD_GLOBAL_WINDOW_SECONDS") or 3.0
ANTIFLOOD_COOLDOWN_SECONDS = _read_float_env("ANTIFLOOD_COOLDOWN_SECONDS") or 6.0
ANTIFLOOD_USER_TTL_SECONDS = _read_float_env("ANTIFLOOD_USER_TTL_SECONDS") or 180.0
ANTIFLOOD_MAX_TRACKED_USERS = _read_int_env("ANTIFLOOD_MAX_TRACKED_USERS") or 50000
REQUEST_DEDUPE_ACTIVE_TTL_SECONDS = _read_float_env("REQUEST_DEDUPE_ACTIVE_TTL_SECONDS") or 900.0
REQUEST_DEDUPE_COMPLETED_TTL_SECONDS = _read_float_env("REQUEST_DEDUPE_COMPLETED_TTL_SECONDS") or 12.0
REQUEST_DEDUPE_MAX_ENTRIES = _read_int_env("REQUEST_DEDUPE_MAX_ENTRIES") or 50000

BOT_COMMANDS = [
    {"command": "start", "description": "Get started"},
    {"command": "settings", "description": "Settings"},
    {"command": "stats", "description": "Statistics"},
]
ADMINS_UID = [ADMIN_ID]
