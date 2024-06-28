import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums.parse_mode import ParseMode

from config import BOT_TOKEN, BOT_COMMANDS, OUTPUT_DIR, custom_api_url
from services.db import DataBase

logging.basicConfig(level=logging.INFO)

custom_timeout = 600  # 10 minutes

session = AiohttpSession(
    api=TelegramAPIServer.from_base(custom_api_url),
    timeout=custom_timeout
)

default = DefaultBotProperties(parse_mode=ParseMode.HTML)
bot = Bot(token=BOT_TOKEN, default=default, session=session)

dp = Dispatcher()

db = DataBase()

os.makedirs("downloads", exist_ok=True)


async def main():
    import handlers
    import middlewares

    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    dp.include_router(handlers.router)
    for middleware in middlewares.__all__:
        dp.message.middleware(middleware())
        dp.callback_query.middleware(middleware())
    await bot.set_my_commands(commands=BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
