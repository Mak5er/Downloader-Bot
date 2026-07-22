import os
import sys
import time
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

HEARTBEAT_FILE = "/tmp/bot_heartbeat"
MAX_HEARTBEAT_AGE_SECONDS = 60.0


async def check_database() -> bool:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return False
    try:
        engine = create_async_engine(db_url, connect_args={"timeout": 5} if "sqlite" in db_url else {})
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception as exc:
        print(f"Database check failed: {exc}", file=sys.stderr)
        return False


def check_heartbeat() -> bool:
    if not os.path.exists(HEARTBEAT_FILE):
        print(f"Heartbeat file {HEARTBEAT_FILE} does not exist", file=sys.stderr)
        return False
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        age = time.time() - mtime
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                ts = float(content)
                age = min(age, time.time() - ts)
        if age > MAX_HEARTBEAT_AGE_SECONDS:
            print(f"Heartbeat stale: age={age:.1f}s > {MAX_HEARTBEAT_AGE_SECONDS}s", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"Heartbeat check failed: {exc}", file=sys.stderr)
        return False


async def main():
    hb_ok = check_heartbeat()
    db_ok = await check_database()
    if hb_ok and db_ok:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
