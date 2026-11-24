import asyncio
import logging
import os
import re
from datetime import datetime
from aiohttp import web
import aiosqlite
from rapidfuzz import fuzz, process

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден токен: задайте переменную окружения BOT_TOKEN")

# На Airlok и большинстве PaaS пишем базу в /tmp
DB_PATH = os.getenv("DB_PATH", "/tmp/racebot.db")
TRACK = "Nürburgring"

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ==================== FSM ====================
class AddResult(StatesGroup):
    waiting_for_car = State()
    waiting_for_time = State()
    confirming_car = State()


# ==================== Утилиты ====================
def normalize_car_name(name: str) -> str:
    """Приводит название машины к единому виду для сравнения."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "", name)  # оставляем только буквы и цифры
    return name


def parse_time(time_str: str) -> int | None:
    """
    Парсит время в формате M:SS.mmm или SS.mmm
    Возвращает миллисекунды или None при ошибке.
    """
    time_str = time_str.strip().replace(",", ".")
    match = re.match(r"^(?:(\d+):)?(\d{1,2})\.(\d{1,3})$", time_str)
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    ms_str = match.group(3).ljust(3, "0")[:3]
    milliseconds = int(ms_str)
    return minutes * 60000 + seconds * 1000 + milliseconds


def format_time(ms: int) -> str:
    """Форматирует миллисекунды обратно в M:SS.mmm"""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    millis = ms % 1000
    if minutes > 0:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


# ==================== База данных ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_original TEXT NOT NULL,
                name_normalized TEXT NOT NULL UNIQUE,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                car_id INTEGER NOT NULL,
                time_ms INTEGER NOT NULL,
                time_display TEXT NOT NULL,
                track TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id),
                FOREIGN KEY (car_id) REFERENCES cars (id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована: %s", DB_PATH)


async def get_or_create_user(telegram_id: int, username: str | None, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, full_name, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, username, full_name, datetime.utcnow().isoformat())
        )
        await db.commit()


async def find_similar_cars(normalized: str, limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name_original, name_normalized FROM cars") as cursor:
            cars = await cursor.fetchall()

    if not cars:
        return []

    choices = {c["name_normalized"]: c for c in cars}
    results = process.extract(
        normalized,
        choices.keys(),
        scorer=fuzz.ratio,
        limit=limit,
        score_cutoff=70  # порог похожести
    )
    return [(choices[name], score) for name, score, _ in results]


async def get_or_create_car(name_original: str):
    normalized = normalize_car_name(name_original)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name_original FROM cars WHERE name_normalized = ?",
            (normalized,)
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return row["id"], row["name_original"], False  # уже существовала

        await db.execute(
            "INSERT INTO cars (name_original, name_normalized, created_at) VALUES (?, ?, ?)",
            (name_original.strip(), normalized, datetime.utcnow().isoformat())
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cursor:
            car_id = (await cursor.fetchone())[0]
        return car_id, name_original.strip(), True  # создана новая


async def add_result(user_id: int, car_id: int, time_ms: int, time_display: str):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO results (user_id, car_id, time_ms, time_display, track, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, car_id, time_ms, time_display, TRACK, now, now)
        )
        await db.commit()


async def get_top(limit: int = 15):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.time_ms, r.time_display, c.name_original as car, u.username, u.full_name
            FROM results r
            JOIN cars c ON c.id = r.car_id
            JOIN users u ON u.telegram_id = r.user_id
            WHERE r.track = ?
            ORDER BY r.time_ms ASC
            LIMIT ?
        """, (TRACK, limit)) as cursor:
            return await cursor.fetchall()


async def get_user_results(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.id, r.time_display, r.time_ms, c.name_original as car, r.created_at
            FROM results r
            JOIN cars c ON c.id = r.car_id
            WHERE r.user_id = ? AND r.track = ?
            ORDER BY r.time_ms ASC
        """, (user_id, TRACK)) as cursor:
            return await cursor.fetchall()


async def get_all_cars():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT c.name_original, COUNT(r.id) as count, MIN(r.time_ms) as best_ms
            FROM cars c
            LEFT JOIN results r ON r.car_id = c.id AND r.track = ?
            GROUP BY c.id
            ORDER BY count DESC, c.name_original
        """, (TRACK,)) as cursor:
            return await cursor.fetchall()


# ==================== Хендлеры ====================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name or ""
    )
    text = (
        f"Привет, {message.from_user.first_name}! 🏁\n\n"
        f"Это бот статистики для <b>{TRACK}</b> (CPM2).\n\n"
        "Доступные команды:\n"
        "/add — добавить результат\n"
        "/top — общий рейтинг\n"
        "/my — мои результаты\n"
        "/cars — список машин\n\n"
        "Формат времени: <code>1:23.456</code> или <code>83.456</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddResult.waiting_for_car)
    await message.answer(
        "Введи название машины:\n"
        "Пример: <code>Porsche 911 GT3</code>\n\n"
        "Постарайся писать название одинаково, чтобы не создавать дубликаты.",
        parse_mode="HTML"
    )


@dp.message(AddResult.waiting_for_car)
async def process_car(message: Message, state: FSMContext):
    car_name = message.text.strip()
    if len(car_name) < 2:
        await message.answer("Слишком короткое название. Попробуй ещё раз.")
        return

    normalized = normalize_car_name(car_name)
    similar = await find_similar_cars(normalized)

    # Если есть очень похожие (score >= 85) — предлагаем выбрать
    good_matches = [(car, score) for car, score in similar if score >= 85]

    if good_matches:
        await state.update_data(pending_car_name=car_name)
        buttons = []
        for car, score in good_matches[:4]:
            buttons.append([
                InlineKeyboardButton(
                    text=f"{car['name_original']} ({score}%)",
                    callback_data=f"use_car:{car['id']}"
                )
            ])
        buttons.append([
            InlineKeyboardButton(text="➕ Создать новую", callback_data="create_new_car")
        ])
        await state.set_state(AddResult.confirming_car)
        await message.answer(
            f"Нашёл похожие машины для «{car_name}»:\nВыбери существующую или создай новую:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    else:
        # Создаём сразу
        car_id, final_name, is_new = await get_or_create_car(car_name)
        await state.update_data(car_id=car_id, car_name=final_name)
        await state.set_state(AddResult.waiting_for_time)
        prefix = "Создана новая машина" if is_new else "Использую существующую"
        await message.answer(
            f"{prefix}: <b>{final_name}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>",
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("use_car:"))
async def use_existing_car(callback: CallbackQuery, state: FSMContext):
    car_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name_original FROM cars WHERE id = ?", (car_id,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        await callback.answer("Машина не найдена", show_alert=True)
        return

    await state.update_data(car_id=car_id, car_name=row["name_original"])
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(
        f"Выбрана машина: <b>{row['name_original']}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "create_new_car")
async def create_new_car(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    car_name = data.get("pending_car_name", "Unknown")
    car_id, final_name, _ = await get_or_create_car(car_name)
    await state.update_data(car_id=car_id, car_name=final_name)
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(
        f"Создана новая машина: <b>{final_name}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(AddResult.waiting_for_time)
async def process_time(message: Message, state: FSMContext):
    time_ms = parse_time(message.text)
    if time_ms is None:
        await message.answer(
            "Не понял формат времени.\n"
            "Используй: <code>1:23.456</code> или <code>83.456</code>",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    car_id = data["car_id"]
    car_name = data["car_name"]
    time_display = format_time(time_ms)

    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name or ""
    )
    await add_result(message.from_user.id, car_id, time_ms, time_display)
    await state.clear()

    await message.answer(
        f"✅ Результат сохранён!\n\n"
        f"🏎 <b>{car_name}</b>\n"
        f"⏱ {time_display}\n"
        f"📍 {TRACK}",
        parse_mode="HTML"
    )


@dp.message(Command("top"))
async def cmd_top(message: Message):
    rows = await get_top(20)
    if not rows:
        await message.answer("Пока нет ни одного результата. Будь первым — /add")
        return

    lines = [f"🏆 <b>Топ результатов — {TRACK}</b>\n"]
    for i, row in enumerate(rows, 1):
        name = f"@{row['username']}" if row["username"] else row["full_name"]
        lines.append(f"{i}. {row['time_display']} — {row['car']} ({name})")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("my"))
async def cmd_my(message: Message):
    rows = await get_user_results(message.from_user.id)
    if not rows:
        await message.answer("У тебя пока нет результатов. Добавь первый — /add")
        return

    lines = ["📋 <b>Твои результаты</b>\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['time_display']} — {row['car']}")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cars"))
async def cmd_cars(message: Message):
    rows = await get_all_cars()
    if not rows:
        await message.answer("Пока нет ни одной машины в базе.")
        return

    lines = ["🏎 <b>Машины в базе</b>\n"]
    for row in rows:
        best = format_time(row["best_ms"]) if row["best_ms"] else "—"
        lines.append(f"• {row['name_original']} — {row['count']} рез., лучшее: {best}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ==================== Health + Main ====================
async def health_handler(request):
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
    await init_db()
    bot = Bot(token=TOKEN)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён (если был)")
    except TelegramRetryAfter as e:
        logger.warning("Лимит при удалении webhook: %s сек", e.retry_after)
    except Exception as e:
        logger.warning("Не удалось удалить webhook: %s", e)

    await start_health_server()

    while True:
        try:
            me = await bot.get_me()
            logger.info("Подключён как @%s (id=%s)", me.username, me.id)
            break
        except TelegramUnauthorizedError:
            logger.error("Неверный токен бота")
            await asyncio.sleep(60)
        except TelegramRetryAfter as e:
            wait = min(e.retry_after + 5, 60)
            logger.warning("Лимит Telegram, ждём %s сек...", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error("Ошибка подключения: %s", e)
            await asyncio.sleep(15)

    logger.info("Запускаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
