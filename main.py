import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from aiohttp import web
import asyncpg
from rapidfuzz import fuzz, process

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден токен: задайте переменную окружения BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Не найден DATABASE_URL. Включите PostgreSQL в настройках Airlok.")

ADMIN_IDS = {1170169366}
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS.update(int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit())

TRACK = "Nürburgring"

SEASON_CURRENT = "after_patch"
SEASON_LEGACY = "before_patch"
SEASON_LABELS = {
    SEASON_CURRENT: "Обновленная физика",
    SEASON_LEGACY: "До обновления",
}

DAILY_CHAT_ID = -1004321825624
DAILY_THREAD_ID = 2179
DAILY_TZ = ZoneInfo("Europe/Moscow")
DAILY_HOUR = 15
DAILY_MINUTE = 0

MOTIVATION_LINES = [
    "Один чистый круг лучше десяти грязных.",
    "Зелёный ад не прощает ошибок — но награждает точных.",
    "Сегодняшний круг — завтрашний личный рекорд.",
    "Не забудь записать результат в бота ⏱",
    "Трасса ждёт. Пиши время в @Racer_cpm2_bot",
    "Каждый круг — шаг к вершине рейтинга.",
]

pool = None
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def is_whitelisted(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM whitelist WHERE telegram_id = $1", user_id)
        return row is not None

async def whitelist_add(telegram_id: int, added_by: int) -> bool:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO whitelist (telegram_id, added_by) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            telegram_id, added_by,
        )
        return True

async def whitelist_remove(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return False
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM whitelist WHERE telegram_id = $1", telegram_id)
        return res == "DELETE 1"

async def whitelist_list():
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT telegram_id, added_by, created_at FROM whitelist ORDER BY created_at"
        )

def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить результат"), KeyboardButton(text="🏆 Топ")],
            [KeyboardButton(text="👤 Пилоты"), KeyboardButton(text="🏎 Машины")],
            [KeyboardButton(text="📋 Мои результаты"), KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или напиши команду",
    )

class AddResult(StatesGroup):
    choosing_car_source = State()
    waiting_for_car = State()
    waiting_for_time = State()
    waiting_for_proof = State()
    confirming_car = State()

class EditCar(StatesGroup):
    choosing_car = State()
    waiting_photo = State()
    waiting_power = State()
    waiting_torque = State()
    waiting_transmission = State()
    waiting_drivetrain = State()

def normalize_car_name(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.casefold(), flags=re.UNICODE)

MIN_LAP_MS = 2 * 60 * 1000
MAX_LAP_MS = 30 * 60 * 1000

def parse_time(time_str: str):
    time_str = time_str.strip().replace(",", ".")
    match = re.match(r"^(?:(\d+):)?(\d{1,2})\.(\d{1,3})$", time_str)
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    if seconds >= 60:
        return None
    ms_str = match.group(3).ljust(3, "0")[:3]
    total = minutes * 60000 + seconds * 1000 + int(ms_str)
    if total < MIN_LAP_MS or total > MAX_LAP_MS:
        return None
    return total

def format_time(ms: int) -> str:
    minutes, seconds, millis = ms // 60000, (ms % 60000) // 1000, ms % 1000
    return f"{minutes}:{seconds:02d}.{millis:03d}" if minutes > 0 else f"{seconds}.{millis:03d}"

def format_delta(ms_diff: int) -> str:
    sign = "+" if ms_diff > 0 else "−"
    return f"{sign}{format_time(abs(ms_diff))}"

def praise_for_time(time_ms: int):
    if time_ms < 5 * 60000:
        return "🚀 Низкие 5 минут! Отличный результат!"
    if time_ms < 5 * 60000 + 30 * 1000:
        return "🔥 Вышел из 5:30 — очень сильно!"
    if time_ms < 6 * 60000:
        return "💪 Вышел из 6 минут — серьёзный темп!"
    if time_ms < 6 * 60000 + 30 * 1000:
        return "👍 Крепкий результат, есть куда расти."
    if time_ms < 7 * 60000:
        return "👌 Нормальный круг, можно быстрее."
    return None

async def build_result_facts(time_ms: int) -> str:
    lines = ["\n📌 <b>Немного контекста</b>"]
    leader = await get_leader_time_ms()
    if leader is not None:
        if time_ms <= leader:
            lines.append("🏆 Это лучшее время в боте прямо сейчас!")
        else:
            lines.append(f"📉 До лидера бота: <b>{format_delta(time_ms - leader)}</b>")
    praise = praise_for_time(time_ms)
    if praise:
        lines.append(praise)
    return "\n".join(lines)

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS cars (
            id SERIAL PRIMARY KEY, name_original TEXT NOT NULL, name_normalized TEXT NOT NULL UNIQUE,
            photo_file_id TEXT, power_hp INTEGER, torque_nm INTEGER, transmission TEXT, drivetrain TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        for col, typ in [("photo_file_id","TEXT"),("power_hp","INTEGER"),("torque_nm","INTEGER"),("transmission","TEXT"),("drivetrain","TEXT")]:
            await conn.execute(f"ALTER TABLE cars ADD COLUMN IF NOT EXISTS {col} {typ}")
        await conn.execute("""CREATE TABLE IF NOT EXISTS results (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(telegram_id),
            car_id INTEGER NOT NULL REFERENCES cars(id),
            time_ms INTEGER NOT NULL,
            time_display TEXT NOT NULL,
            track TEXT NOT NULL,
            proof_file_id TEXT,
            proof_type TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW())""")
        await conn.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS proof_file_id TEXT")
        await conn.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS proof_type TEXT")
        await conn.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS season TEXT")
        await conn.execute("UPDATE results SET season = $1 WHERE season IS NULL", SEASON_LEGACY)
        await conn.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            telegram_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        for aid in ADMIN_IDS:
            await conn.execute(
                "INSERT INTO whitelist (telegram_id, added_by) VALUES ($1, $1) ON CONFLICT DO NOTHING",
                aid,
            )
    logger.info("PostgreSQL база инициализирована")

async def get_or_create_user(telegram_id, username, full_name):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username, full_name) VALUES ($1,$2,$3) ON CONFLICT (telegram_id) DO NOTHING",
            telegram_id, username, full_name)

async def find_similar_cars(normalized, limit=5):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name_original, name_normalized FROM cars")
    if not rows:
        return []
    choices = {r["name_normalized"]: r for r in rows}
    results = process.extract(normalized, choices.keys(), scorer=fuzz.ratio, limit=limit, score_cutoff=70)
    return [(choices[name], score) for name, score, _ in results]

async def get_or_create_car(name_original):
    normalized = normalize_car_name(name_original)
    if not normalized:
        raise ValueError("empty normalized car name")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, name_original FROM cars WHERE name_normalized = $1", normalized)
        if row:
            return row["id"], row["name_original"], False
        row = await conn.fetchrow(
            "INSERT INTO cars (name_original, name_normalized) VALUES ($1,$2) RETURNING id, name_original",
            name_original.strip(), normalized)
        return row["id"], row["name_original"], True

async def add_result(user_id, car_id, time_ms, time_display, proof_file_id=None, proof_type=None, season=None):
    if season is None:
        season = SEASON_CURRENT
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO results (user_id, car_id, time_ms, time_display, track, proof_file_id, proof_type, season) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            user_id, car_id, time_ms, time_display, TRACK, proof_file_id, proof_type, season)

async def get_top(limit=15, season=None):
    if season is None:
        season = SEASON_CURRENT
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT r.time_ms, r.time_display, c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id=r.car_id JOIN users u ON u.telegram_id=r.user_id
               WHERE r.track=$1 AND r.season=$2 ORDER BY r.time_ms ASC LIMIT $3""", TRACK, season, limit)

async def get_user_results(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT r.id, r.time_display, r.time_ms, c.name_original as car, r.created_at, r.proof_file_id, r.proof_type, r.season
               FROM results r JOIN cars c ON c.id=r.car_id
               WHERE r.user_id=$1 AND r.track=$2 ORDER BY r.time_ms ASC""", user_id, TRACK)

async def get_result_by_id(result_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT r.id, r.time_display, r.time_ms, r.proof_file_id, r.proof_type,
                      c.name_original as car, u.username, u.full_name, r.user_id
               FROM results r JOIN cars c ON c.id=r.car_id JOIN users u ON u.telegram_id=r.user_id
               WHERE r.id=$1""", result_id)

async def get_pilots_ranking_sorted(limit=15, season=None):
    if season is None:
        season = SEASON_CURRENT
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (r.user_id) r.user_id, r.time_ms, r.time_display,
                      c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id=r.car_id JOIN users u ON u.telegram_id=r.user_id
               WHERE r.track=$1 AND r.season=$2 ORDER BY r.user_id, r.time_ms ASC""", TRACK, season)
    return sorted(rows, key=lambda r: r["time_ms"])[:limit]

async def get_leader_time_ms(season=None):
    if season is None:
        season = SEASON_CURRENT
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT MIN(time_ms) as best FROM results WHERE track=$1 AND season=$2", TRACK, season)
        return row["best"] if row else None

async def get_daily_stats():
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COUNT(*) as results_count, COUNT(DISTINCT user_id) as pilots_count, MIN(time_ms) as best_ms
               FROM results WHERE track=$1 AND season=$2 AND created_at >= NOW() - INTERVAL '24 hours'""",
            TRACK, SEASON_CURRENT)
        best = await conn.fetchrow(
            """SELECT r.time_display, c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id=r.car_id JOIN users u ON u.telegram_id=r.user_id
               WHERE r.track=$1 AND r.season=$2 AND r.created_at >= NOW() - INTERVAL '24 hours'
               ORDER BY r.time_ms ASC LIMIT 1""", TRACK, SEASON_CURRENT)
        return row, best

async def build_daily_message() -> str:
    row, best = await get_daily_stats()
    lines = [f"🏁 <b>Итоги суток — {TRACK}</b>\n<i>{SEASON_LABELS[SEASON_CURRENT]}</i>\n"]
    n = int(row["results_count"] or 0) if row else 0
    pilots = int(row["pilots_count"] or 0) if row else 0
    if n == 0:
        lines.append("За последние 24 часа результатов пока нет.")
        lines.append("\nБудь первым — добавь круг в @Racer_cpm2_bot ⏱")
    else:
        lines.append(f"• Результатов: <b>{n}</b>")
        lines.append(f"• Пилотов: <b>{pilots}</b>")
        if best:
            name = f"@{best['username']}" if best["username"] else best["full_name"]
            lines.append(f"• Лучшее: <b>{best['time_display']}</b> — {best['car']}")
            lines.append(f"  👤 {name}")
        lines.append("")
        lines.append(random.choice(MOTIVATION_LINES))
        lines.append("\nЗапиши свой круг в @Racer_cpm2_bot")
    return "\n".join(lines)

async def send_daily_digest(bot: Bot):
    text = await build_daily_message()
    try:
        await bot.send_message(chat_id=DAILY_CHAT_ID, text=text, parse_mode="HTML", message_thread_id=DAILY_THREAD_ID)
        logger.info("Ежедневная рассылка отправлена")
    except Exception as e:
        logger.error("Ошибка рассылки: %s", e)

async def daily_broadcast_loop(bot: Bot):
    while True:
        now = datetime.now(DAILY_TZ)
        target = now.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        logger.info("Следующая рассылка через %.0f сек (%s МСК)", wait_sec, target.strftime("%Y-%m-%d %H:%M"))
        await asyncio.sleep(wait_sec)
        await send_daily_digest(bot)
        await asyncio.sleep(60)

async def get_community_stats(season=None):
    if season is None:
        season = SEASON_CURRENT
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COUNT(*) as results_count, COUNT(DISTINCT user_id) as pilots_count,
                      COUNT(DISTINCT car_id) as cars_count, MIN(time_ms) as best_ms,
                      AVG(time_ms)::BIGINT as avg_ms FROM results WHERE track=$1 AND season=$2""",
            TRACK, season)
        popular = await conn.fetchrow(
            """SELECT c.name_original, COUNT(r.id) as cnt FROM results r
               JOIN cars c ON c.id=r.car_id WHERE r.track=$1 AND r.season=$2
               GROUP BY c.id, c.name_original ORDER BY cnt DESC LIMIT 1""", TRACK, season)
        leader = await conn.fetchrow(
            """SELECT r.time_display, c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id=r.car_id JOIN users u ON u.telegram_id=r.user_id
               WHERE r.track=$1 AND r.season=$2 ORDER BY r.time_ms ASC LIMIT 1""", TRACK, season)
        return row, popular, leader

async def get_pilot_stats(user_id, season=None):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT username, full_name FROM users WHERE telegram_id=$1", user_id)
        if season:
            results = await conn.fetch(
                """SELECT r.id, r.time_ms, r.time_display, c.name_original as car, r.created_at,
                          r.proof_file_id, r.proof_type, r.season
                   FROM results r JOIN cars c ON c.id=r.car_id
                   WHERE r.user_id=$1 AND r.track=$2 AND r.season=$3 ORDER BY r.time_ms ASC""",
                user_id, TRACK, season)
        else:
            results = await conn.fetch(
                """SELECT r.id, r.time_ms, r.time_display, c.name_original as car, r.created_at,
                          r.proof_file_id, r.proof_type, r.season
                   FROM results r JOIN cars c ON c.id=r.car_id
                   WHERE r.user_id=$1 AND r.track=$2 ORDER BY r.time_ms ASC""", user_id, TRACK)
        return user, results

async def get_all_cars(season=None):
    async with pool.acquire() as conn:
        if season:
            return await conn.fetch(
                """SELECT c.id, c.name_original, COUNT(r.id) as count, MIN(r.time_ms) as best_ms
                   FROM cars c LEFT JOIN results r ON r.car_id=c.id AND r.track=$1 AND r.season=$2
                   GROUP BY c.id, c.name_original ORDER BY best_ms ASC NULLS LAST, c.name_original""",
                TRACK, season)
        return await conn.fetch(
            """SELECT c.id, c.name_original, COUNT(r.id) as count, MIN(r.time_ms) as best_ms
               FROM cars c LEFT JOIN results r ON r.car_id=c.id AND r.track=$1
               GROUP BY c.id, c.name_original ORDER BY c.name_original""", TRACK)

async def get_car_details(car_id, season=None):
    async with pool.acquire() as conn:
        if season:
            return await conn.fetchrow(
                """SELECT c.*, COUNT(r.id) as results_count, MIN(r.time_ms) as best_ms
                   FROM cars c LEFT JOIN results r ON r.car_id=c.id AND r.track=$2 AND r.season=$3
                   WHERE c.id=$1 GROUP BY c.id""", car_id, TRACK, season)
        return await conn.fetchrow(
            """SELECT c.*, COUNT(r.id) as results_count, MIN(r.time_ms) as best_ms
               FROM cars c LEFT JOIN results r ON r.car_id=c.id AND r.track=$2
               WHERE c.id=$1 GROUP BY c.id""", car_id, TRACK)

async def get_car_results(car_id, season=None):
    async with pool.acquire() as conn:
        if season:
            return await conn.fetch(
                """SELECT r.id, r.time_ms, r.time_display, r.proof_file_id, r.proof_type,
                          u.username, u.full_name, u.telegram_id as user_id, r.created_at
                   FROM results r JOIN users u ON u.telegram_id=r.user_id
                   WHERE r.car_id=$1 AND r.track=$2 AND r.season=$3 ORDER BY r.time_ms ASC""",
                car_id, TRACK, season)
        return await conn.fetch(
            """SELECT r.id, r.time_ms, r.time_display, r.proof_file_id, r.proof_type,
                      u.username, u.full_name, u.telegram_id as user_id, r.created_at, r.season
               FROM results r JOIN users u ON u.telegram_id=r.user_id
               WHERE r.car_id=$1 AND r.track=$2 ORDER BY r.time_ms ASC""", car_id, TRACK)

async def update_car_info(car_id, photo_file_id=None, power_hp=None, torque_nm=None, transmission=None, drivetrain=None):
    async with pool.acquire() as conn:
        if photo_file_id is not None:
            await conn.execute("UPDATE cars SET photo_file_id=$1 WHERE id=$2", photo_file_id, car_id)
        if power_hp is not None:
            await conn.execute("UPDATE cars SET power_hp=$1 WHERE id=$2", power_hp, car_id)
        if torque_nm is not None:
            await conn.execute("UPDATE cars SET torque_nm=$1 WHERE id=$2", torque_nm, car_id)
        if transmission is not None:
            await conn.execute("UPDATE cars SET transmission=$1 WHERE id=$2", transmission, car_id)
        if drivetrain is not None:
            await conn.execute("UPDATE cars SET drivetrain=$1 WHERE id=$2", drivetrain, car_id)

async def delete_result(result_id, user_id=None):
    async with pool.acquire() as conn:
        if user_id is not None:
            result = await conn.execute("DELETE FROM results WHERE id=$1 AND user_id=$2", result_id, user_id)
        else:
            result = await conn.execute("DELETE FROM results WHERE id=$1", result_id)
        return result == "DELETE 1"

async def delete_car(car_id):
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM results WHERE car_id=$1", car_id)
        deleted = int(res.split()[-1]) if res.startswith("DELETE") else 0
        await conn.execute("DELETE FROM cars WHERE id=$1", car_id)
        return deleted

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    text = (
        f"Привет, {message.from_user.first_name}! 🏁\n\n"
        f"Бот статистики <b>{TRACK}</b> (CPM2).\n"
        f"Текущий сезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\n"
        "/add — добавить результат\n/top — топ\n/pilots — пилоты\n"
        "/my — мои результаты\n/cars — машины\n/stats — статистика\n/cancel — отмена\n\n"
        "Формат времени: <code>5:23.456</code>"
    )
    if is_admin(message.from_user.id):
        text += "\n\n🔧 Админ: /editcar /delcar /delresult /testdaily /allow /deny /allows"
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.", reply_markup=get_main_keyboard())
        return
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=get_main_keyboard())

@dp.message(Command("testdaily"))
async def cmd_testdaily(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для админов.")
        return
    await send_daily_digest(bot)
    await message.answer("✅ Тест отправлен.")

@dp.message(Command("allow"))
async def cmd_allow(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().lstrip("-").isdigit():
        await message.answer("Использование: <code>/allow TELEGRAM_ID</code>", parse_mode="HTML")
        return
    uid = int(args[1].strip())
    await whitelist_add(uid, message.from_user.id)
    await message.answer(f"✅ <code>{uid}</code> добавлен.", parse_mode="HTML")

@dp.message(Command("deny"))
async def cmd_deny(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().lstrip("-").isdigit():
        await message.answer("Использование: <code>/deny TELEGRAM_ID</code>", parse_mode="HTML")
        return
    uid = int(args[1].strip())
    if uid in ADMIN_IDS:
        await message.answer("Админа нельзя убрать.")
        return
    ok = await whitelist_remove(uid)
    await message.answer(f"✅ Убран." if ok else "Не был в списке.")

@dp.message(Command("allows"))
async def cmd_allows(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = await whitelist_list()
    if not rows:
        await message.answer("Список пуст.")
        return
    lines = ["<b>Пилоты</b>\n"] + [f"• <code>{r['telegram_id']}</code>" for r in rows]
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("add"))
@dp.message(F.text == "➕ Добавить результат")
async def cmd_add(message: Message, state: FSMContext):
    if not await is_whitelisted(message.from_user.id):
        await message.answer("⛔ Добавлять могут только пилоты из списка.")
        return
    await state.set_state(AddResult.choosing_car_source)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Выбрать из списка", callback_data="add_pick_car")],
        [InlineKeyboardButton(text="✏️ Новый автомобиль", callback_data="add_new_car")],
    ])
    await message.answer(
        f"Добавление результата\nСезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\nКак указать машину?\n/cancel — отмена.",
        parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_new_car")
async def add_new_car_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddResult.waiting_for_car)
    await callback.message.edit_text(
        "Введи название машины:\nПример: <code>Porsche 911 GT3</code>\n/cancel — отмена.",
        parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "add_pick_car")
async def add_pick_car_list(callback: CallbackQuery, state: FSMContext):
    rows = await get_all_cars()
    if not rows:
        await state.set_state(AddResult.waiting_for_car)
        await callback.message.edit_text("Машин пока нет. Введи название:\n/cancel — отмена.")
        await callback.answer()
        return
    buttons = [[InlineKeyboardButton(text=r["name_original"][:60], callback_data=f"pick_car:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="✏️ Новый автомобиль", callback_data="add_new_car")])
    await callback.message.edit_text("Выбери машину:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("pick_car:"))
async def add_pick_car_selected(callback: CallbackQuery, state: FSMContext):
    car_id = int(callback.data.split(":")[1])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name_original FROM cars WHERE id=$1", car_id)
    if not row:
        await callback.answer("Не найдена", show_alert=True)
        return
    await state.update_data(car_id=car_id, car_name=row["name_original"])
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(
        f"Выбрана: <b>{row['name_original']}</b>\nСезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\n"
        "Введи время:\nПример: <code>5:23.456</code>\n/cancel — отмена.", parse_mode="HTML")
    await callback.answer()

@dp.message(AddResult.waiting_for_car)
async def process_car(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст.\n/cancel — отмена.")
        return
    car_name = message.text.strip()
    if len(car_name) < 2 or not normalize_car_name(car_name):
        await message.answer("Некорректное название.\n/cancel — отмена.")
        return
    similar = await find_similar_cars(normalize_car_name(car_name))
    good = [(c, s) for c, s in similar if s >= 85]
    if good:
        await state.update_data(pending_car_name=car_name)
        buttons = [[InlineKeyboardButton(text=f"{c['name_original']} ({s}%)", callback_data=f"use_car:{c['id']}")] for c, s in good[:4]]
        buttons.append([InlineKeyboardButton(text="➕ Создать новую", callback_data="create_new_car")])
        await state.set_state(AddResult.confirming_car)
        await message.answer(f"Похожие для «{car_name}»:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        car_id, final_name, is_new = await get_or_create_car(car_name)
        await state.update_data(car_id=car_id, car_name=final_name)
        await state.set_state(AddResult.waiting_for_time)
        prefix = "Создана" if is_new else "Выбрана"
        await message.answer(
            f"{prefix}: <b>{final_name}</b>\nСезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\n"
            "Введи время:\nПример: <code>5:23.456</code>\n/cancel — отмена.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("use_car:"))
async def use_existing_car(callback: CallbackQuery, state: FSMContext):
    car_id = int(callback.data.split(":")[1])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name_original FROM cars WHERE id=$1", car_id)
    if not row:
        await callback.answer("Не найдена", show_alert=True)
        return
    await state.update_data(car_id=car_id, car_name=row["name_original"])
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(
        f"Выбрана: <b>{row['name_original']}</b>\nСезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\n"
        "Введи время:\nПример: <code>5:23.456</code>\n/cancel — отмена.", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "create_new_car")
async def create_new_car(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    car_id, final_name, _ = await get_or_create_car(data.get("pending_car_name", "Unknown"))
    await state.update_data(car_id=car_id, car_name=final_name)
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(
        f"Создана: <b>{final_name}</b>\nСезон: <b>{SEASON_LABELS[SEASON_CURRENT]}</b>\n\n"
        "Введи время:\nПример: <code>5:23.456</code>\n/cancel — отмена.", parse_mode="HTML")
    await callback.answer()

@dp.message(AddResult.waiting_for_time)
async def process_time(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст времени.\n/cancel — отмена.")
        return
    time_ms = parse_time(message.text)
    if time_ms is None:
        await message.answer("Не принял время. Формат <code>5:23.456</code>, диапазон 2:00–30:00.\n/cancel — отмена.", parse_mode="HTML")
        return
    await state.update_data(time_ms=time_ms, time_display=format_time(time_ms))
    await state.set_state(AddResult.waiting_for_proof)
    await message.answer("Отправь <b>фото или видео</b> доказательства.\n/cancel — отмена.", parse_mode="HTML")

@dp.message(AddResult.waiting_for_proof, F.photo)
async def process_proof_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    await add_result(message.from_user.id, data["car_id"], data["time_ms"], data["time_display"], message.photo[-1].file_id, "photo")
    await state.clear()
    facts = await build_result_facts(data["time_ms"])
    await message.answer(
        f"✅ Сохранено!\n🏎 <b>{data['car_name']}</b>\n⏱ {data['time_display']}\n"
        f"📂 {SEASON_LABELS[SEASON_CURRENT]}\n📎 фото{facts}",
        parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(AddResult.waiting_for_proof, F.video)
async def process_proof_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    await add_result(message.from_user.id, data["car_id"], data["time_ms"], data["time_display"], message.video.file_id, "video")
    await state.clear()
    facts = await build_result_facts(data["time_ms"])
    await message.answer(
        f"✅ Сохранено!\n🏎 <b>{data['car_name']}</b>\n⏱ {data['time_display']}\n"
        f"📂 {SEASON_LABELS[SEASON_CURRENT]}\n📎 видео{facts}",
        parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(AddResult.waiting_for_proof)
async def process_proof_invalid(message: Message, state: FSMContext):
    await message.answer("Нужно фото или видео.\n/cancel — отмена.")

async def send_top(target, season: str):
    rows = await get_top(15, season=season)
    label = SEASON_LABELS.get(season, season)
    if not rows:
        text = f"🏆 <b>Топ — {TRACK}</b>\n<i>{label}</i>\n\nПока пусто."
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"🏆 <b>Топ — {TRACK}</b>\n<i>{label}</i>\n"]
        for i, row in enumerate(rows, 1):
            name = f"@{row['username']}" if row["username"] else row["full_name"]
            lines.append(f"{medals.get(i, f'{i}.')} <b>{row['time_display']}</b> — {row['car']}\n👤 {name}\n")
        text = "\n".join(lines)
    other = SEASON_LEGACY if season == SEASON_CURRENT else SEASON_CURRENT
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📂 {SEASON_LABELS[other]}", callback_data=f"top_season:{other}")],
    ])
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()

@dp.message(Command("top"))
@dp.message(F.text == "🏆 Топ")
async def cmd_top(message: Message):
    await send_top(message, SEASON_CURRENT)

@dp.callback_query(F.data.startswith("top_season:"))
async def top_season_switch(callback: CallbackQuery):
    season = callback.data.split(":", 1)[1]
    if season not in SEASON_LABELS:
        await callback.answer("?")
        return
    await send_top(callback, season)

async def send_pilots(target, season: str):
    rows = await get_pilots_ranking_sorted(15, season=season)
    label = SEASON_LABELS.get(season, season)
    if not rows:
        text = f"👤 <b>Пилоты — {TRACK}</b>\n<i>{label}</i>\n\nПока пусто."
        kb_rows = []
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"👤 <b>Пилоты — {TRACK}</b>\n<i>{label} · 1 лучший</i>\n"]
        for i, row in enumerate(rows, 1):
            name = f"@{row['username']}" if row["username"] else row["full_name"]
            lines.append(f"{medals.get(i, f'{i}.')} <b>{row['time_display']}</b> — {row['car']}\n👤 {name}\n")
        text = "\n".join(lines)
        kb_rows = [[InlineKeyboardButton(text="ℹ️ Подробнее о пилоте", callback_data=f"show_pilots_list:{season}")]]
    other = SEASON_LEGACY if season == SEASON_CURRENT else SEASON_CURRENT
    kb_rows.append([InlineKeyboardButton(text=f"📂 {SEASON_LABELS[other]}", callback_data=f"pilots_season:{other}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()

@dp.message(Command("pilots"))
@dp.message(F.text == "👤 Пилоты")
async def cmd_pilots(message: Message):
    await send_pilots(message, SEASON_CURRENT)

@dp.callback_query(F.data.startswith("pilots_season:"))
async def pilots_season_switch(callback: CallbackQuery):
    season = callback.data.split(":", 1)[1]
    if season not in SEASON_LABELS:
        await callback.answer("?")
        return
    await send_pilots(callback, season)

@dp.callback_query(F.data.startswith("show_pilots_list"))
async def show_pilots_list(callback: CallbackQuery):
    parts = callback.data.split(":")
    season = parts[1] if len(parts) > 1 else SEASON_CURRENT
    rows = await get_pilots_ranking_sorted(30, season=season)
    if not rows:
        await callback.answer("Нет пилотов", show_alert=True)
        return
    buttons = []
    for row in rows:
        name = f"@{row['username']}" if row["username"] else (row["full_name"] or str(row["user_id"]))
        buttons.append([InlineKeyboardButton(text=f"ℹ️ {name} — {row['time_display']}", callback_data=f"pilot_info:{row['user_id']}")])
    buttons.append([InlineKeyboardButton(text="« Закрыть", callback_data="close_menu")])
    await callback.message.edit_text("Выбери пилота:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data == "close_menu")
async def close_menu(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data.startswith("pilot_info:"))
async def show_pilot_info(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    user, results_cur = await get_pilot_stats(user_id, season=SEASON_CURRENT)
    _, results_all = await get_pilot_stats(user_id, season=None)
    if not user:
        await callback.answer("Не найден", show_alert=True)
        return
    name = f"@{user['username']}" if user["username"] else user["full_name"]
    lines = [f"👤 <b>{name}</b>\n", f"<i>{SEASON_LABELS[SEASON_CURRENT]}</i>"]
    if results_cur:
        best = results_cur[0]
        leader_ms = await get_leader_time_ms(SEASON_CURRENT)
        if leader_ms is not None:
            delta = best["time_ms"] - leader_ms
            delta_text = "\n🏆 Лидер сезона" if delta == 0 else f"\n📉 До лидера: <b>+{delta/1000:.3f} с</b>"
        else:
            delta_text = ""
        lines += [f"⏱ Рекорд: <b>{best['time_display']}</b> — {best['car']}", delta_text,
                  f"📊 В сезоне: <b>{len(results_cur)}</b>"]
    else:
        lines.append("Нет результатов в текущем сезоне.")
    lines.append(f"\n📂 Всего за всё время: <b>{len(results_all)}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Подробнее", callback_data=f"pilot_results:{user_id}")],
    ])
    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("pilot_results:"))
async def show_pilot_results_list(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    user, results = await get_pilot_stats(user_id, season=None)
    if not user or not results:
        await callback.answer("Нет результатов", show_alert=True)
        return
    name = f"@{user['username']}" if user["username"] else user["full_name"]
    lines = [f"📋 <b>{name}</b>\n"]
    for i, r in enumerate(results, 1):
        season_mark = "🆕" if r.get("season") == SEASON_CURRENT else "📂"
        mark = " 📎" if r.get("proof_file_id") else ""
        lines.append(f"{i}. {season_mark} <b>{r['time_display']}</b> — {r['car']}{mark}")
    buttons = []
    for r in results:
        if r.get("proof_file_id"):
            buttons.append([InlineKeyboardButton(text=f"📎 {r['time_display']} — {r['car']}", callback_data=f"view_proof:{r['id']}")])
    buttons.append([InlineKeyboardButton(text="« Закрыть", callback_data="close_menu")])
    text = "\n".join(lines)
    if len(text) > 3500:
        text = "\n".join(lines[:40]) + f"\n… ещё {len(results)-40}"
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.message(Command("stats"))
@dp.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    row, popular, leader = await get_community_stats(SEASON_CURRENT)
    if not row or row["results_count"] == 0:
        await message.answer(f"Пока нет данных («{SEASON_LABELS[SEASON_CURRENT]}»).")
        return
    lines = [f"📊 <b>Статистика — {TRACK}</b>\n<i>{SEASON_LABELS[SEASON_CURRENT]}</i>\n"]
    lines.append(f"👤 Пилотов: <b>{row['pilots_count']}</b>")
    lines.append(f"🏁 Результатов: <b>{row['results_count']}</b>")
    lines.append(f"🏎 Машин: <b>{row['cars_count']}</b>")
    if row["best_ms"] is not None:
        lines.append(f"\n⏱ Лучшее: <b>{format_time(row['best_ms'])}</b>")
    if row["avg_ms"] is not None:
        lines.append(f"📈 Среднее: <b>{format_time(int(row['avg_ms']))}</b>")
    if leader:
        name = f"@{leader['username']}" if leader["username"] else leader["full_name"]
        lines.append(f"👑 Лидер: {name} — {leader['time_display']} ({leader['car']})")
    if popular:
        lines.append(f"\n🔥 Популярная: <b>{popular['name_original']}</b> ({popular['cnt']})")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("my"))
@dp.message(F.text == "📋 Мои результаты")
async def cmd_my(message: Message):
    rows = await get_user_results(message.from_user.id)
    if not rows:
        await message.answer("У тебя пока нет результатов.")
        return
    lines = ["📋 <b>Твои результаты</b>\n"]
    for i, r in enumerate(rows, 1):
        sm = "🆕" if r.get("season") == SEASON_CURRENT else "📂"
        lines.append(f"{i}. {sm} <b>{r['time_display']}</b> — {r['car']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Доказательство", callback_data="show_proof_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="show_delete_list")],
    ])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "show_delete_list")
async def show_delete_list(callback: CallbackQuery):
    if not await is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = await get_user_results(callback.from_user.id)
    if not rows:
        await callback.answer("Пусто", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {r['time_display']} — {r['car']}", callback_data=f"del_my:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_my")])
    await callback.message.edit_text("Удалить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data == "back_to_my")
async def back_to_my(callback: CallbackQuery):
    rows = await get_user_results(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("Пусто.")
        await callback.answer()
        return
    lines = ["📋 <b>Твои результаты</b>\n"] + [f"{i}. <b>{r['time_display']}</b> — {r['car']}" for i, r in enumerate(rows, 1)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Доказательство", callback_data="show_proof_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="show_delete_list")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "show_proof_list")
async def show_proof_list(callback: CallbackQuery):
    rows = await get_user_results(callback.from_user.id)
    if not rows:
        await callback.answer("Пусто", show_alert=True)
        return
    buttons = []
    for r in rows:
        has = "📎" if r["proof_file_id"] else "—"
        buttons.append([InlineKeyboardButton(text=f"{has} {r['time_display']} — {r['car']}", callback_data=f"view_proof:{r['id']}")])
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_my")])
    await callback.message.edit_text("Доказательство:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_proof:"))
async def view_proof(callback: CallbackQuery):
    row = await get_result_by_id(int(callback.data.split(":")[1]))
    if not row or not row["proof_file_id"]:
        await callback.answer("Нет доказательства", show_alert=True)
        return
    name = f"@{row['username']}" if row["username"] else row["full_name"]
    caption = f"📎 {row['time_display']} — {row['car']}\n👤 {name}"
    if row["proof_type"] == "video":
        await callback.message.answer_video(video=row["proof_file_id"], caption=caption)
    else:
        await callback.message.answer_photo(photo=row["proof_file_id"], caption=caption)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_my:"))
async def confirm_delete_my_result(callback: CallbackQuery):
    if not await is_whitelisted(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    rid = int(callback.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_del:{rid}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="show_delete_list"),
    ]])
    await callback.message.edit_text("Точно удалить?", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_del:"))
async def delete_my_result(callback: CallbackQuery):
    if not await is_whitelisted(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    deleted = await delete_result(int(callback.data.split(":")[1]), user_id=callback.from_user.id)
    if deleted:
        await callback.answer("✅ Удалено")
        rows = await get_user_results(callback.from_user.id)
        if not rows:
            await callback.message.edit_text("Все удалены.")
            return
        buttons = [[InlineKeyboardButton(text=f"🗑 {r['time_display']} — {r['car']}", callback_data=f"del_my:{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_my")])
        await callback.message.edit_text("Удалить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await callback.answer("Ошибка", show_alert=True)

@dp.message(Command("cars"))
@dp.message(F.text == "🏎 Машины")
async def cmd_cars(message: Message):
    rows = await get_all_cars(season=SEASON_CURRENT)
    if not rows:
        await message.answer("Пока нет машин в текущем сезоне.")
        return
    lines = [f"🏎 <b>Машины</b>\n<i>{SEASON_LABELS[SEASON_CURRENT]}</i>\n"]
    for row in rows:
        best = format_time(row["best_ms"]) if row["best_ms"] else "—"
        lines.append(f"• <b>{row['name_original']}</b>\n  {row['count']} рез., лучшее: {best}\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="show_cars_list")]])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "show_cars_list")
async def show_cars_list(callback: CallbackQuery):
    rows = await get_all_cars()
    if not rows:
        await callback.answer("Пусто", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"ℹ️ {r['name_original']}", callback_data=f"car_info:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_cars")])
    await callback.message.edit_text("Машина:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data == "back_to_cars")
async def back_to_cars(callback: CallbackQuery):
    rows = await get_all_cars(season=SEASON_CURRENT)
    lines = [f"🏎 <b>Машины</b>\n<i>{SEASON_LABELS[SEASON_CURRENT]}</i>\n"]
    for row in rows:
        best = format_time(row["best_ms"]) if row["best_ms"] else "—"
        lines.append(f"• <b>{row['name_original']}</b>\n  {row['count']} рез., лучшее: {best}\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="show_cars_list")]])
    await callback.message.edit_text("\n".join(lines) if rows else "Пусто.", parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("car_info:"))
async def show_car_info(callback: CallbackQuery):
    car_id = int(callback.data.split(":")[1])
    car = await get_car_details(car_id, season=SEASON_CURRENT)
    if not car:
        await callback.answer("Не найдена", show_alert=True)
        return
    best = format_time(car["best_ms"]) if car["best_ms"] else "—"
    text = (f"🏎 <b>{car['name_original']}</b>\n<i>{SEASON_LABELS[SEASON_CURRENT]}</i>\n\n"
            f"📊 Результатов: {car['results_count']}\n⏱ Лучшее: {best}\n\n")
    if car["power_hp"]:
        text += f"⚡ {car['power_hp']} л.с.\n"
    if car["torque_nm"]:
        text += f"🔧 {car['torque_nm']} Н·м\n"
    if car.get("transmission"):
        text += f"⚙️ КПП: {car['transmission']}\n"
    if car["drivetrain"]:
        text += f"🔄 Привод: {car['drivetrain']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏁 Заезды (текущий сезон)", callback_data=f"car_results:{car_id}:{SEASON_CURRENT}")],
        [InlineKeyboardButton(text="📂 Заезды (до обновления)", callback_data=f"car_results:{car_id}:{SEASON_LEGACY}")],
    ])
    if car["photo_file_id"]:
        await callback.message.answer_photo(photo=car["photo_file_id"], caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("car_results:"))
async def show_car_results(callback: CallbackQuery):
    parts = callback.data.split(":")
    car_id = int(parts[1])
    season = parts[2] if len(parts) > 2 else SEASON_CURRENT
    car = await get_car_details(car_id, season=season)
    rows = await get_car_results(car_id, season=season)
    if not car:
        await callback.answer("Не найдена", show_alert=True)
        return
    label = SEASON_LABELS.get(season, season)
    if not rows:
        await callback.answer(f"Нет заездов ({label})", show_alert=True)
        return
    lines = [f"🏁 <b>{car['name_original']}</b>\n<i>{label}</i>\n"]
    for i, r in enumerate(rows, 1):
        name = f"@{r['username']}" if r["username"] else r["full_name"]
        mark = " 📎" if r.get("proof_file_id") else ""
        lines.append(f"{i}. <b>{r['time_display']}</b> — {name}{mark}")
    text = "\n".join(lines)
    if len(text) > 3500:
        text = "\n".join(lines[:40]) + f"\n… ещё {len(rows)-40}"
    buttons = []
    for r in rows:
        if r.get("proof_file_id"):
            name = f"@{r['username']}" if r["username"] else (r["full_name"] or "")
            buttons.append([InlineKeyboardButton(text=f"📎 {r['time_display']} — {name}"[:60], callback_data=f"view_proof:{r['id']}")])
    buttons.append([InlineKeyboardButton(text="« Закрыть", callback_data="close_menu")])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.message(Command("editcar"))
async def cmd_editcar(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔")
        return
    rows = await get_all_cars()
    if not rows:
        await message.answer("Нет машин.")
        return
    buttons = [[InlineKeyboardButton(text=r["name_original"], callback_data=f"editcar_select:{r['id']}")] for r in rows]
    await state.set_state(EditCar.choosing_car)
    await message.answer("Машина:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("editcar_select:"))
async def editcar_selected(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.update_data(edit_car_id=int(callback.data.split(":")[1]))
    await state.set_state(EditCar.waiting_photo)
    await callback.message.edit_text("Фото или <code>-</code>", parse_mode="HTML")
    await callback.answer()

@dp.message(EditCar.waiting_photo, F.photo)
async def editcar_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_car_info(data["edit_car_id"], photo_file_id=message.photo[-1].file_id)
    await state.set_state(EditCar.waiting_power)
    await message.answer("Мощность л.с. или -")

@dp.message(EditCar.waiting_photo)
async def editcar_photo_skip(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "-":
        await state.set_state(EditCar.waiting_power)
        await message.answer("Мощность л.с. или -")
    else:
        await message.answer("Фото или -")

@dp.message(EditCar.waiting_power)
async def editcar_power(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    if text != "-" and text.isdigit():
        await update_car_info(data["edit_car_id"], power_hp=int(text))
    elif text != "-":
        await message.answer("Число или -")
        return
    await state.set_state(EditCar.waiting_torque)
    await message.answer("Момент Н·м или -")

@dp.message(EditCar.waiting_torque)
async def editcar_torque(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    if text != "-" and text.isdigit():
        await update_car_info(data["edit_car_id"], torque_nm=int(text))
    elif text != "-":
        await message.answer("Число или -")
        return
    await state.set_state(EditCar.waiting_transmission)
    await message.answer("КПП: MT/AT/DCT/SEQ/CVT или -")

@dp.message(EditCar.waiting_transmission)
async def editcar_transmission(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    mapping = {"MT": "MT", "AT": "AT", "DCT": "DCT", "SEQ": "SEQ", "CVT": "CVT", "ВАРИАТОР": "CVT"}
    key = raw.upper() if raw != "-" else "-"
    if key != "-" and key in mapping:
        await update_car_info(data["edit_car_id"], transmission=mapping[key])
    elif key != "-":
        await message.answer("MT/AT/DCT/SEQ/CVT или -")
        return
    await state.set_state(EditCar.waiting_drivetrain)
    await message.answer("Привод: FWD/RWD/AWD/4WD или -")

@dp.message(EditCar.waiting_drivetrain)
async def editcar_drivetrain(message: Message, state: FSMContext):
    text = (message.text or "").strip().upper()
    data = await state.get_data()
    if text != "-" and text in ("FWD", "RWD", "AWD", "4WD"):
        await update_car_info(data["edit_car_id"], drivetrain=text)
    elif text != "-":
        await message.answer("FWD/RWD/AWD/4WD или -")
        return
    await state.clear()
    car = await get_car_details(data["edit_car_id"])
    await message.answer(f"✅ <b>{car['name_original']}</b> обновлена.", parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(Command("delresult"))
async def cmd_delresult(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("/delresult ID")
        return
    rid = int(args[1].strip())
    await message.answer("✅" if await delete_result(rid) else "❌")

@dp.message(Command("delcar"))
async def cmd_delcar(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔")
        return
    rows = await get_all_cars()
    if not rows:
        await message.answer("Пусто.")
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {r['name_original']}", callback_data=f"admin_delcar:{r['id']}")] for r in rows]
    await message.answer("Удалить машину:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("admin_delcar:"))
async def admin_delete_car(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    n = await delete_car(int(callback.data.split(":")[1]))
    await callback.message.edit_text(f"✅ Удалено. Результатов: {n}")
    await callback.answer()

async def health_handler(request):
    return web.Response(text="ok")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", 8080)))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health на %s", port)

async def main():
    await init_db()
    bot = Bot(token=TOKEN)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning("Webhook: %s", e)
    await start_health_server()
    while True:
        try:
            me = await bot.get_me()
            logger.info("@%s", me.username)
            break
        except TelegramUnauthorizedError:
            await asyncio.sleep(60)
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after + 5, 60))
        except Exception as e:
            logger.error("%s", e)
            await asyncio.sleep(15)
    asyncio.create_task(daily_broadcast_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
