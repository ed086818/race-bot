"""
Телеграм-бот для учёта времени круга на Нюрбургринге (Nordschleife).

Запуск:
    pip install "aiogram>=3.7" aiosqlite
    export BOT_TOKEN="123456:ABC..."     # токен от @BotFather
    python main.py

Структура файла:
    1. Настройки
    2. Работа с базой данных (SQLite)
    3. Вспомогательные функции (разбор и вывод времени)
    4. Состояния диалога (FSM)
    5. Обработчики команд
    6. Запуск
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------
# 1. Настройки
# --------------------------------------------------------------------------

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DB_NAME = os.getenv("DB_NAME", "nurburgring.db")

# Допустимый диапазон времени круга (секунды). Рекорды трассы ~5:00–5:20,
# верхняя граница нужна только чтобы отсечь явные опечатки.
MIN_LAP = 5 * 60
MAX_LAP = 30 * 60

LEADERBOARD_SIZE = 10
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

router = Router()


# --------------------------------------------------------------------------
# 2. База данных
# --------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cars (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                key  TEXT NOT NULL UNIQUE,   -- нормализованное название (для поиска)
                name TEXT NOT NULL           -- как показываем пользователям
            );
            CREATE TABLE IF NOT EXISTS laps (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                car_id       INTEGER NOT NULL REFERENCES cars(id),
                lap_time     REAL NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_laps_user ON laps(user_id);
            CREATE INDEX IF NOT EXISTS idx_laps_car  ON laps(car_id, lap_time);
            """
        )
        await db.commit()


def car_key(name: str) -> str:
    """'  Porsche   911 GT3 ' и 'porsche 911 gt3' -> один и тот же ключ."""
    return re.sub(r"\s+", " ", name.strip()).casefold()


async def add_lap(user_id: int, display_name: str, car_name: str, lap_time: float) -> int:
    """Сохраняет заезд, возвращает id автомобиля."""
    clean_name = re.sub(r"\s+", " ", car_name.strip())
    key = car_key(clean_name)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO cars (key, name) VALUES (?, ?)", (key, clean_name)
        )
        cur = await db.execute("SELECT id FROM cars WHERE key = ?", (key,))
        car_id = (await cur.fetchone())[0]
        await db.execute(
            "INSERT INTO laps (user_id, display_name, car_id, lap_time, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, display_name, car_id, lap_time, datetime.now().isoformat()),
        )
        await db.commit()
    return car_id


async def get_user_best_rank(car_id: int, user_id: int):
    """Лучшее время пользователя на авто и его место в таблице лидеров."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT MIN(lap_time) FROM laps WHERE car_id = ? AND user_id = ?",
            (car_id, user_id),
        )
        best = (await cur.fetchone())[0]
        if best is None:
            return None, None
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id FROM laps
                WHERE car_id = ?
                GROUP BY user_id
                HAVING MIN(lap_time) < ?
            )
            """,
            (car_id, best),
        )
        rank = (await cur.fetchone())[0] + 1
        return best, rank


async def get_user_stats(user_id: int):
    """Общая статистика и лучший круг по каждому авто пользователя."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT COUNT(*) AS total, COUNT(DISTINCT car_id) AS cars_count,
                   MIN(lap_time) AS best, AVG(lap_time) AS avg
            FROM laps WHERE user_id = ?
            """,
            (user_id,),
        )
        total = await cur.fetchone()
        cur = await db.execute(
            """
            SELECT c.id AS car_id, c.name AS car_name,
                   COUNT(*) AS laps_count, MIN(l.lap_time) AS best
            FROM laps l JOIN cars c ON c.id = l.car_id
            WHERE l.user_id = ?
            GROUP BY c.id
            ORDER BY best
            """,
            (user_id,),
        )
        per_car = await cur.fetchall()
        return total, per_car


async def get_last_laps(user_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT c.name AS car_name, l.lap_time, l.created_at
            FROM laps l JOIN cars c ON c.id = l.car_id
            WHERE l.user_id = ?
            ORDER BY l.id DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return await cur.fetchall()


async def get_leaderboard(car_id: int, limit: int = LEADERBOARD_SIZE):
    """Лучшее время каждого пользователя на этом авто."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT name FROM cars WHERE id = ?", (car_id,))
        car = await cur.fetchone()
        if car is None:
            return None, []
        cur = await db.execute(
            """
            SELECT user_id, MAX(display_name) AS display_name, MIN(lap_time) AS best
            FROM laps WHERE car_id = ?
            GROUP BY user_id
            ORDER BY best
            LIMIT ?
            """,
            (car_id, limit),
        )
        return car["name"], await cur.fetchall()


async def get_popular_cars(limit: int = 20):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT c.id, c.name, COUNT(DISTINCT l.user_id) AS drivers
            FROM cars c JOIN laps l ON l.car_id = c.id
            GROUP BY c.id
            ORDER BY drivers DESC, MAX(l.id) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return await cur.fetchall()


async def find_car_by_name(name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM cars WHERE key = ?", (car_key(name),))
        exact = await cur.fetchone()
        if exact:
            return exact
        # Поиск по части названия
        cur = await db.execute(
            "SELECT id, name FROM cars WHERE key LIKE ? ORDER BY id LIMIT 1",
            (f"%{car_key(name)}%",),
        )
        return await cur.fetchone()


# --------------------------------------------------------------------------
# 3. Вспомогательные функции
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,4}(?:[.,]\d{1,3})?)$")


def parse_lap_time(text: str):
    """
    Понимает форматы: '7:45.32', '7:45', '7:45,3', '465.3'.
    Возвращает секунды (float) или None, если формат неверный.
    """
    m = _TIME_RE.match(text.strip().replace(" ", ""))
    if not m:
        return None
    minutes = int(m.group(1)) if m.group(1) else 0
    seconds = float(m.group(2).replace(",", "."))
    if m.group(1) and seconds >= 60:
        return None
    return minutes * 60 + seconds


def fmt_time(seconds: float) -> str:
    """465.32 -> '7:45.32'"""
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}:{rest:05.2f}"


def fmt_gap(seconds: float) -> str:
    return f"+{seconds:.2f}"


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def user_display_name(message_or_user) -> str:
    user = getattr(message_or_user, "from_user", message_or_user)
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"id{user.id}"


def format_leaderboard(car_name: str, rows) -> str:
    if not rows:
        return f"По автомобилю <b>{esc(car_name)}</b> пока нет заездов."
    leader = rows[0]["best"]
    lines = [f"🏆 <b>{esc(car_name)}</b> — таблица лидеров", ""]
    for i, r in enumerate(rows, start=1):
        place = MEDALS.get(i, f"{i}.")
        gap = "" if i == 1 else f"  <i>{fmt_gap(r['best'] - leader)}</i>"
        lines.append(f"{place} {esc(r['display_name'])} — <b>{fmt_time(r['best'])}</b>{gap}")
    return "\n".join(lines)


def cars_keyboard(cars, prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{c['name']} ({c['drivers']})", callback_data=f"{prefix}:{c['id']}")]
        for c in cars
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# 4. Состояния диалога
# --------------------------------------------------------------------------

class LapForm(StatesGroup):
    waiting_for_car = State()
    waiting_for_time = State()


# --------------------------------------------------------------------------
# 5. Обработчики
# --------------------------------------------------------------------------

HELP_TEXT = (
    "🏁 <b>Бот для учёта кругов на Нюрбургринге</b>\n\n"
    "/add — записать заезд\n"
    "/stats — моя статистика\n"
    "/last — мои последние заезды\n"
    "/top — таблица лидеров по автомобилю\n"
    "/cancel — отменить ввод"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Ввод отменён.")


# --- Добавление заезда ---------------------------------------------------

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(LapForm.waiting_for_car)
    await message.answer(
        "На каком автомобиле ехали?\nНапример: <i>Porsche 911 GT3 RS (992)</i>\n\n"
        "Отмена — /cancel"
    )


@router.message(LapForm.waiting_for_car, F.text, ~F.text.startswith("/"))
async def process_car(message: Message, state: FSMContext):
    car = re.sub(r"\s+", " ", message.text.strip())
    if len(car) < 2 or len(car) > 60:
        await message.answer("Название должно быть от 2 до 60 символов. Попробуйте ещё раз.")
        return
    await state.update_data(car=car)
    await state.set_state(LapForm.waiting_for_time)
    await message.answer(
        "Какое время круга?\nФормат: <code>7:45.32</code> (минуты:секунды) "
        "или просто секунды, например <code>465.3</code>"
    )


@router.message(LapForm.waiting_for_time, F.text, ~F.text.startswith("/"))
async def process_time(message: Message, state: FSMContext):
    lap_time = parse_lap_time(message.text)
    if lap_time is None:
        await message.answer("Не понял время. Пример: <code>7:45.32</code> или <code>465.3</code>")
        return
    if not (MIN_LAP <= lap_time <= MAX_LAP):
        await message.answer(
            f"Время должно быть между {fmt_time(MIN_LAP)} и {fmt_time(MAX_LAP)}. "
            "Проверьте, нет ли опечатки."
        )
        return

    data = await state.get_data()
    car = data["car"]
    await state.clear()

    uid = message.from_user.id
    car_id = await add_lap(uid, user_display_name(message), car, lap_time)
    best, rank = await get_user_best_rank(car_id, uid)

    text = f"✅ Сохранено: <b>{esc(car)}</b> — <b>{fmt_time(lap_time)}</b>\n"
    if abs(best - lap_time) < 1e-9:
        text += f"🔥 Это ваш лучший результат на этой машине! Место в рейтинге: <b>{rank}</b>"
    else:
        text += f"Ваш рекорд на этой машине: {fmt_time(best)} (место {rank})"
    await message.answer(text)


# --- Статистика ----------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    total, per_car = await get_user_stats(message.from_user.id)
    if not total["total"]:
        await message.answer("У вас пока нет заездов. Добавьте первый: /add")
        return

    lines = [
        "📊 <b>Ваша статистика</b>",
        f"Заездов: {total['total']}  |  Автомобилей: {total['cars_count']}",
        f"Лучший круг: <b>{fmt_time(total['best'])}</b>",
        f"Среднее время: {fmt_time(total['avg'])}",
        "",
        "<b>По автомобилям</b> (лучшее время и место):",
    ]
    for c in per_car:
        _, rank = await get_user_best_rank(c["car_id"], message.from_user.id)
        lines.append(
            f"• {esc(c['car_name'])} — <b>{fmt_time(c['best'])}</b> "
            f"(место {rank}, заездов: {c['laps_count']})"
        )
    await message.answer("\n".join(lines))


@router.message(Command("last"))
async def cmd_last(message: Message):
    rows = await get_last_laps(message.from_user.id, limit=5)
    if not rows:
        await message.answer("Нет сохранённых заездов.")
        return
    lines = ["🕘 <b>Последние заезды</b>"]
    for r in rows:
        date = r["created_at"][:10]
        lines.append(f"• {esc(r['car_name'])} — {fmt_time(r['lap_time'])} ({date})")
    await message.answer("\n".join(lines))


# --- Таблица лидеров -----------------------------------------------------

@router.message(Command("top"))
async def cmd_top(message: Message):
    parts = message.text.split(maxsplit=1)

    # /top Porsche 911 GT3 — поиск по названию
    if len(parts) == 2:
        car = await find_car_by_name(parts[1])
        if car is None:
            await message.answer("Такого автомобиля пока нет в базе. Список: /top")
            return
        name, rows = await get_leaderboard(car["id"])
        await message.answer(format_leaderboard(name, rows))
        return

    # /top без аргумента — выбор из списка
    cars = await get_popular_cars()
    if not cars:
        await message.answer("Пока нет ни одного заезда. Добавьте первый: /add")
        return
    await message.answer(
        "Выберите автомобиль (в скобках — число пилотов).\n"
        "Или напишите: <code>/top название</code>",
        reply_markup=cars_keyboard(cars, "top"),
    )


@router.callback_query(F.data.startswith("top:"))
async def cb_top(callback: CallbackQuery):
    car_id = int(callback.data.split(":", 1)[1])
    name, rows = await get_leaderboard(car_id)
    if name is None:
        await callback.answer("Автомобиль не найден", show_alert=True)
        return
    await callback.message.answer(format_leaderboard(name, rows))
    await callback.answer()


# --------------------------------------------------------------------------
# 6. Запуск
# --------------------------------------------------------------------------

async def main() -> None:
    if not TOKEN:
        raise SystemExit("Не найден токен. Задайте переменную окружения BOT_TOKEN.")

    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()  # FSM по умолчанию хранится в памяти
    dp.include_router(router)

    # aiogram сам переживает временные сетевые ошибки и лимиты Telegram,
    # поэтому свой цикл перезапуска не нужен.
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
