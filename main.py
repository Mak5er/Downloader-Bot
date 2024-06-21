import logging

from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, BOT_COMMANDS

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def main():
    import handlers
    dp.include_router(handlers.router)

    await bot.set_my_commands(commands=BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
