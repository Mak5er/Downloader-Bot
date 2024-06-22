import logging
import os

from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, BOT_COMMANDS, OUTPUT_DIR
from services.db import DataBase

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
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
