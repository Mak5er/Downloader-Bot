import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Ensure project root is on sys.path so imports like `main` and `middlewares.*` work in tests.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# Keep tests independent from the developer's local `.env`.
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_CI_ONLY")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost/testdb")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("CUSTOM_API_URL", "https://api.telegram.org")
os.environ.setdefault("MEASUREMENT_ID", "G-TEST123")
os.environ.setdefault("API_SECRET", "test-secret")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("COBALT_API_URL", "https://cobalt.example")
os.environ.setdefault("COBALT_API_KEY", "test-cobalt-key")


from app_context import set_app_context


class _PatchableAsyncNamespace(SimpleNamespace):
    def __getattr__(self, name):
        value = AsyncMock(return_value=None)
        setattr(self, name, value)
        return value


@pytest.fixture(autouse=True)
def reset_app_context():
    bot = _PatchableAsyncNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
    )
    set_app_context(
        bot=bot,
        db=_PatchableAsyncNamespace(),
        send_analytics=AsyncMock(),
    )
