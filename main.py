import asyncio
import logging
import os
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import CommandStart
from aiogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден токен: задайте переменную окружения BOT_TOKEN")

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! Бот работает 🏁")


async def health_handler(request):
    """Простой health-check, чтобы Airlok не считал приложение мёртвым."""
    return web.Response(text="ok")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", os.getenv("WEB_PORT", 8080)))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check сервер запущен на порту %s", port)


async def main():
    bot = Bot(token=TOKEN)

    # Пытаемся удалить webhook, но не падаем и не ждём при лимитах
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён (если был)")
    except TelegramRetryAfter as e:
        logger.warning("Лимит при удалении webhook (ждём не будем): %s сек", e.retry_after)
    except Exception as e:
        logger.warning("Не удалось удалить webhook: %s", e)

    # Сразу запускаем health-check
    await start_health_server()

    # Подключаемся к Telegram
    while True:
        try:
            me = await bot.get_me()
            logger.info("Подключён как @%s (id=%s)", me.username, me.id)
            break
        except TelegramUnauthorizedError:
            logger.error("Неверный токен бота (Unauthorized). Проверьте BOT_TOKEN.")
            await asyncio.sleep(60)
        except TelegramRetryAfter as e:
            wait = min(e.retry_after + 5, 60)  # максимум 60 сек за раз
            logger.warning("Лимит Telegram, ждём %s сек...", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error("Ошибка при подключении: %s", e)
            await asyncio.sleep(15)

    logger.info("Запускаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
