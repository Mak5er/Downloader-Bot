import os

import httpx
from aiocron import crontab
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums.parse_mode import ParseMode

from config import BOT_TOKEN, BOT_COMMANDS, OUTPUT_DIR, custom_api_url, MEASUREMENT_ID, API_SECRET
from log.logger import logger as logging
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


async def send_analytics(user_id, chat_type, action_name):
    try:
        chat_type_value = chat_type.value if hasattr(chat_type, 'value') else str(chat_type)

        params = {
            'client_id': str(user_id),
            'user_id': str(user_id),
            'events': [{
                'name': action_name,
                'params': {
                    'chat_type': chat_type_value,
                    'session_id': str(user_id),
                    'engagement_time_msec': '1000'
                }
            }],
        }

        async with httpx.AsyncClient() as client:
            await client.post(
                f'https://www.google-analytics.com/mp/collect?measurement_id={MEASUREMENT_ID}&api_secret={API_SECRET}',
                json=params,
                timeout=10,
            )

        async with db.SessionLocal() as session:
            event = AnalyticsEvent(
                user_id=user_id,
                chat_type=chat_type_value,
                action_name=action_name
            )
            session.add(event)
            await session.commit()
    except Exception as error:
        logging.error(f"Failed to record analytics event '{action_name}' for user {user_id}: {error}")



async def main():
    logging.info(f"Starting {(await bot.get_me()).username} bot initialisation")
    await db.init_db()

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
        await close_http_session()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
