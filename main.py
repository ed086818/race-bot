import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import Message

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден токен: задайте BOT_TOKEN")

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! Бот работает 🏁")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TOKEN)

    while True:
        try:
            me = await bot.get_me()
            logging.info("Подключён как @%s", me.username)
            break
        except TelegramRetryAfter as e:
            logging.warning("Лимит Telegram, ждём %s сек...", e.retry_after)
            await asyncio.sleep(e.retry_after + 5)

    await dp.start_polling(bot)


asyncio.run(main())
