import os
import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
from aiocron import crontab
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums.parse_mode import ParseMode

from config import (
    BOT_TOKEN,
    BOT_COMMANDS,
    OUTPUT_DIR,
    custom_api_url,
    MEASUREMENT_ID,
    API_SECRET,
)
from log.logger import logger as logging
from services.download_queue import shutdown_download_queue
from services.db import DataBase, AnalyticsEvent
from utils.http_client import close_http_session

custom_timeout = 600
session = AiohttpSession(
    api=TelegramAPIServer.from_base(custom_api_url),
    timeout=custom_timeout
)
default = DefaultBotProperties(parse_mode=ParseMode.HTML)
bot = Bot(token=BOT_TOKEN, default=default, session=session)
dp = Dispatcher()

db = DataBase()

os.makedirs("downloads", exist_ok=True)


@dataclass(slots=True)
class _AnalyticsPayload:
    user_id: int
    chat_type: str
    action_name: str


_ANALYTICS_QUEUE_MAXSIZE = 2048
_ANALYTICS_WORKERS = 2
_ANALYTICS_BATCH_SIZE = 25
_ANALYTICS_BATCH_TIMEOUT = 0.5

_analytics_queue: Optional[asyncio.Queue[Optional[_AnalyticsPayload]]] = None
_analytics_worker_tasks: list[asyncio.Task] = []
_analytics_http_client: Optional[httpx.AsyncClient] = None


async def _get_analytics_http_client() -> httpx.AsyncClient:
    global _analytics_http_client
    if _analytics_http_client is None:
        _analytics_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
    return _analytics_http_client


async def _close_analytics_http_client() -> None:
    global _analytics_http_client
    if _analytics_http_client is not None:
        try:
            await _analytics_http_client.aclose()
        except Exception as error:  # pragma: no cover - defensive close
            logging.debug("Failed to close analytics HTTP client: %s", error)
    _analytics_http_client = None


async def _send_to_google_analytics(payload: _AnalyticsPayload) -> None:
    if not MEASUREMENT_ID or not API_SECRET:
        return

    params = {
        'client_id': str(payload.user_id),
        'user_id': str(payload.user_id),
        'events': [{
            'name': payload.action_name,
            'params': {
                'chat_type': payload.chat_type,
                'session_id': str(payload.user_id),
                'engagement_time_msec': '1000'
            }
        }],
    }

    client = await _get_analytics_http_client()
    await client.post(
        f'https://www.google-analytics.com/mp/collect?measurement_id={MEASUREMENT_ID}&api_secret={API_SECRET}',
        json=params,
        timeout=10,
    )


async def _persist_analytics_batch(batch: list[_AnalyticsPayload]) -> None:
    if not batch:
        return

    async with db.SessionLocal() as session:
        for payload in batch:
            session.add(
                AnalyticsEvent(
                    user_id=payload.user_id,
                    chat_type=payload.chat_type,
                    action_name=payload.action_name,
                )
            )
        await session.commit()


async def _flush_analytics_batch(batch: list[_AnalyticsPayload]) -> None:
    if not batch:
        return

    for payload in batch:
        try:
            await _send_to_google_analytics(payload)
        except Exception as error:
            logging.debug(
                "Failed to send analytics to GA: user_id=%s action=%s error=%s",
                payload.user_id,
                payload.action_name,
                error,
            )

    await _persist_analytics_batch(batch)


async def _analytics_worker(worker_id: int) -> None:
    queue = _analytics_queue
    if queue is None:
        return

    loop = asyncio.get_running_loop()
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break

        batch = [item]
        stop_requested = False
        deadline = loop.time() + _ANALYTICS_BATCH_TIMEOUT

        while len(batch) < _ANALYTICS_BATCH_SIZE:
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            try:
                next_item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break

            if next_item is None:
                stop_requested = True
                queue.task_done()
                break

            batch.append(next_item)

        try:
            await _flush_analytics_batch(batch)
        except Exception as error:
            logging.error("Analytics worker failed: worker=%s error=%s", worker_id, error)
        finally:
            for _ in batch:
                queue.task_done()

        if stop_requested:
            break


async def start_analytics_workers() -> None:
    global _analytics_queue, _analytics_worker_tasks
    if _analytics_queue is not None and _analytics_worker_tasks:
        return

    _analytics_queue = asyncio.Queue(maxsize=_ANALYTICS_QUEUE_MAXSIZE)
    _analytics_worker_tasks = [
        asyncio.create_task(_analytics_worker(idx), name=f"analytics-worker-{idx}")
        for idx in range(_ANALYTICS_WORKERS)
    ]
    logging.info("Analytics workers started: count=%s", _ANALYTICS_WORKERS)


async def stop_analytics_workers() -> None:
    global _analytics_queue, _analytics_worker_tasks
    queue = _analytics_queue
    if queue is not None:
        await queue.join()
        for _ in _analytics_worker_tasks:
            queue.put_nowait(None)
        if _analytics_worker_tasks:
            await asyncio.gather(*_analytics_worker_tasks, return_exceptions=True)

    _analytics_worker_tasks = []
    _analytics_queue = None
    await _close_analytics_http_client()


async def send_analytics(user_id, chat_type, action_name):
    try:
        payload = _AnalyticsPayload(
            user_id=user_id,
            chat_type=chat_type.value if hasattr(chat_type, 'value') else str(chat_type),
            action_name=action_name,
        )

        queue = _analytics_queue
        if queue is not None:
            try:
                queue.put_nowait(payload)
                return
            except asyncio.QueueFull:
                logging.warning(
                    "Analytics queue is full, dropping event: user_id=%s action=%s",
                    user_id,
                    action_name,
                )
                return

        await _flush_analytics_batch([payload])
    except Exception as error:
        logging.error(f"Failed to record analytics event '{action_name}' for user {user_id}: {error}")



async def main():
    logging.info(f"Starting {(await bot.get_me()).username} bot initialisation")
    await db.init_db()
    await start_analytics_workers()

    import handlers
    import middlewares
    from handlers.admin import clear_downloads_and_notify

    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    dp.include_router(handlers.router)

    # Додаємо інші мідлвейри
    for middleware in middlewares.__all__:
        dp.message.outer_middleware(middleware())
        dp.callback_query.outer_middleware(middleware())
        dp.inline_query.outer_middleware(middleware())

    await bot.set_my_commands(commands=BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)

    crontab('0 0 * * *', func=clear_downloads_and_notify, start=True)

    logging.info("Launching polling loop")
    try:
        await dp.start_polling(bot)
    finally:
        await stop_analytics_workers()
        await shutdown_download_queue()
        await close_http_session()


if __name__ == "__main__":
    asyncio.run(main())
