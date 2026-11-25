import asyncio
import logging
import os
import re
from datetime import datetime
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
pool = None
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить результат"), KeyboardButton(text="🏆 Топ")],
            [KeyboardButton(text="👤 Пилоты"), KeyboardButton(text="🏎 Машины")],
            [KeyboardButton(text="📋 Мои результаты")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или напиши команду",
    )


class AddResult(StatesGroup):
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
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def parse_time(time_str: str):
    time_str = time_str.strip().replace(",", ".")
    match = re.match(r"^(?:(\d+):)?(\d{1,2})\.(\d{1,3})$", time_str)
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    ms_str = match.group(3).ljust(3, "0")[:3]
    return minutes * 60000 + seconds * 1000 + int(ms_str)


def format_time(ms: int) -> str:
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    millis = ms % 1000
    if minutes > 0:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cars (
                id SERIAL PRIMARY KEY,
                name_original TEXT NOT NULL,
                name_normalized TEXT NOT NULL UNIQUE,
                photo_file_id TEXT,
                power_hp INTEGER,
                torque_nm INTEGER,
                transmission TEXT,
                drivetrain TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, typ in [
            ("photo_file_id", "TEXT"), ("power_hp", "INTEGER"),
            ("torque_nm", "INTEGER"), ("transmission", "TEXT"), ("drivetrain", "TEXT"),
        ]:
            await conn.execute(f"ALTER TABLE cars ADD COLUMN IF NOT EXISTS {col} {typ}")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(telegram_id),
                car_id INTEGER NOT NULL REFERENCES cars(id),
                time_ms INTEGER NOT NULL,
                time_display TEXT NOT NULL,
                track TEXT NOT NULL,
                proof_file_id TEXT,
                proof_type TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS proof_file_id TEXT")
        await conn.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS proof_type TEXT")
    logger.info("PostgreSQL база инициализирована")


async def get_or_create_user(telegram_id, username, full_name):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (telegram_id, username, full_name) VALUES ($1, $2, $3)
               ON CONFLICT (telegram_id) DO NOTHING""",
            telegram_id, username, full_name,
        )


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
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, name_original FROM cars WHERE name_normalized = $1", normalized)
        if row:
            return row["id"], row["name_original"], False
        row = await conn.fetchrow(
            "INSERT INTO cars (name_original, name_normalized) VALUES ($1, $2) RETURNING id, name_original",
            name_original.strip(), normalized,
        )
        return row["id"], row["name_original"], True


async def add_result(user_id, car_id, time_ms, time_display, proof_file_id=None, proof_type=None):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO results (user_id, car_id, time_ms, time_display, track, proof_file_id, proof_type)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            user_id, car_id, time_ms, time_display, TRACK, proof_file_id, proof_type,
        )


async def get_top(limit=15):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT r.time_ms, r.time_display, c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id = r.car_id JOIN users u ON u.telegram_id = r.user_id
               WHERE r.track = $1 ORDER BY r.time_ms ASC LIMIT $2""", TRACK, limit,
        )


async def get_user_results(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT r.id, r.time_display, r.time_ms, c.name_original as car, r.created_at
               FROM results r JOIN cars c ON c.id = r.car_id
               WHERE r.user_id = $1 AND r.track = $2 ORDER BY r.time_ms ASC""", user_id, TRACK,
        )


async def get_pilots_ranking_sorted(limit=15):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (r.user_id)
                      r.user_id, r.time_ms, r.time_display, c.name_original as car, u.username, u.full_name
               FROM results r JOIN cars c ON c.id = r.car_id JOIN users u ON u.telegram_id = r.user_id
               WHERE r.track = $1 ORDER BY r.user_id, r.time_ms ASC""", TRACK,
        )
    return sorted(rows, key=lambda r: r["time_ms"])[:limit]


async def get_leader_time_ms():
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT MIN(time_ms) as best FROM results WHERE track = $1", TRACK)
        return row["best"] if row else None


async def get_pilot_stats(user_id):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT username, full_name FROM users WHERE telegram_id = $1", user_id)
        results = await conn.fetch(
            """SELECT r.id, r.time_ms, r.time_display, c.name_original as car, r.created_at
               FROM results r JOIN cars c ON c.id = r.car_id
               WHERE r.user_id = $1 AND r.track = $2 ORDER BY r.time_ms ASC""", user_id, TRACK,
        )
        return user, results


async def get_all_cars():
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT c.id, c.name_original, COUNT(r.id) as count, MIN(r.time_ms) as best_ms
               FROM cars c LEFT JOIN results r ON r.car_id = c.id AND r.track = $1
               GROUP BY c.id, c.name_original ORDER BY best_ms ASC NULLS LAST, c.name_original""", TRACK,
        )


async def get_car_details(car_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT c.*, COUNT(r.id) as results_count, MIN(r.time_ms) as best_ms
               FROM cars c LEFT JOIN results r ON r.car_id = c.id AND r.track = $2
               WHERE c.id = $1 GROUP BY c.id""", car_id, TRACK,
        )


async def update_car_info(car_id, photo_file_id=None, power_hp=None, torque_nm=None, transmission=None, drivetrain=None):
    async with pool.acquire() as conn:
        if photo_file_id is not None:
            await conn.execute("UPDATE cars SET photo_file_id = $1 WHERE id = $2", photo_file_id, car_id)
        if power_hp is not None:
            await conn.execute("UPDATE cars SET power_hp = $1 WHERE id = $2", power_hp, car_id)
        if torque_nm is not None:
            await conn.execute("UPDATE cars SET torque_nm = $1 WHERE id = $2", torque_nm, car_id)
        if transmission is not None:
            await conn.execute("UPDATE cars SET transmission = $1 WHERE id = $2", transmission, car_id)
        if drivetrain is not None:
            await conn.execute("UPDATE cars SET drivetrain = $1 WHERE id = $2", drivetrain, car_id)


async def delete_result(result_id, user_id=None):
    async with pool.acquire() as conn:
        if user_id is not None:
            result = await conn.execute("DELETE FROM results WHERE id = $1 AND user_id = $2", result_id, user_id)
        else:
            result = await conn.execute("DELETE FROM results WHERE id = $1", result_id)
        return result == "DELETE 1"


async def delete_car(car_id):
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM results WHERE car_id = $1", car_id)
        deleted = int(res.split()[-1]) if res.startswith("DELETE") else 0
        await conn.execute("DELETE FROM cars WHERE id = $1", car_id)
        return deleted


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    text = (
        f"Привет, {message.from_user.first_name}! 🏁\n\n"
        f"Это бот статистики для <b>{TRACK}</b> (CPM2).\n\n"
        "Используй кнопки внизу или команды:\n"
        "/add — добавить результат (нужно фото/видео)\n"
        "/top — топ всех результатов\n"
        "/pilots — рейтинг пилотов (1 лучший на человека)\n"
        "/my — мои результаты\n"
        "/cars — список машин\n\n"
        "Формат времени: <code>1:23.456</code> или <code>83.456</code>"
    )
    if is_admin(message.from_user.id):
        text += (
            "\n\n🔧 <b>Админ-команды:</b>\n"
            "/editcar — фото и характеристики машины\n"
            "/delcar — удалить машину\n"
            "/delresult — удалить результат по ID"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command("add"))
@dp.message(F.text == "➕ Добавить результат")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddResult.waiting_for_car)
    await message.answer(
        "Введи название машины:\nПример: <code>Porsche 911 GT3</code>\n\n"
        "Постарайся писать название одинаково, чтобы не создавать дубликаты.",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(AddResult.waiting_for_car)
async def process_car(message: Message, state: FSMContext):
    car_name = message.text.strip()
    if len(car_name) < 2:
        await message.answer("Слишком короткое название. Попробуй ещё раз.")
        return
    normalized = normalize_car_name(car_name)
    similar = await find_similar_cars(normalized)
    good_matches = [(car, score) for car, score in similar if score >= 85]
    if good_matches:
        await state.update_data(pending_car_name=car_name)
        buttons = [[InlineKeyboardButton(text=f"{car['name_original']} ({score}%)", callback_data=f"use_car:{car['id']}")] for car, score in good_matches[:4]]
        buttons.append([InlineKeyboardButton(text="➕ Создать новую", callback_data="create_new_car")])
        await state.set_state(AddResult.confirming_car)
        await message.answer(f"Нашёл похожие машины для «{car_name}»:\nВыбери существующую или создай новую:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        car_id, final_name, is_new = await get_or_create_car(car_name)
        await state.update_data(car_id=car_id, car_name=final_name)
        await state.set_state(AddResult.waiting_for_time)
        prefix = "Создана новая машина" if is_new else "Использую существующую"
        await message.answer(f"{prefix}: <b>{final_name}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("use_car:"))
async def use_existing_car(callback: CallbackQuery, state: FSMContext):
    car_id = int(callback.data.split(":")[1])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name_original FROM cars WHERE id = $1", car_id)
    if not row:
        await callback.answer("Машина не найдена", show_alert=True)
        return
    await state.update_data(car_id=car_id, car_name=row["name_original"])
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(f"Выбрана машина: <b>{row['name_original']}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "create_new_car")
async def create_new_car(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    car_id, final_name, _ = await get_or_create_car(data.get("pending_car_name", "Unknown"))
    await state.update_data(car_id=car_id, car_name=final_name)
    await state.set_state(AddResult.waiting_for_time)
    await callback.message.edit_text(f"Создана новая машина: <b>{final_name}</b>\n\nТеперь введи время круга:\nПример: <code>1:23.456</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(AddResult.waiting_for_time)
async def process_time(message: Message, state: FSMContext):
    time_ms = parse_time(message.text)
    if time_ms is None:
        await message.answer("Не понял формат времени.\nИспользуй: <code>1:23.456</code> или <code>83.456</code>", parse_mode="HTML")
        return
    await state.update_data(time_ms=time_ms, time_display=format_time(time_ms))
    await state.set_state(AddResult.waiting_for_proof)
    await message.answer("Теперь отправь <b>доказательство</b> — фото или видео круга.\nБез этого результат не сохранится.", parse_mode="HTML")


@dp.message(AddResult.waiting_for_proof, F.photo)
async def process_proof_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    await add_result(message.from_user.id, data["car_id"], data["time_ms"], data["time_display"], proof_file_id=message.photo[-1].file_id, proof_type="photo")
    await state.clear()
    await message.answer(f"✅ Результат сохранён!\n\n🏎 <b>{data['car_name']}</b>\n⏱ {data['time_display']}\n📍 {TRACK}\n📎 Доказательство: фото", parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(AddResult.waiting_for_proof, F.video)
async def process_proof_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name or "")
    await add_result(message.from_user.id, data["car_id"], data["time_ms"], data["time_display"], proof_file_id=message.video.file_id, proof_type="video")
    await state.clear()
    await message.answer(f"✅ Результат сохранён!\n\n🏎 <b>{data['car_name']}</b>\n⏱ {data['time_display']}\n📍 {TRACK}\n📎 Доказательство: видео", parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(AddResult.waiting_for_proof)
async def process_proof_invalid(message: Message, state: FSMContext):
    await message.answer("Нужно отправить <b>фото</b> или <b>видео</b> круга.\nТекст или другие файлы не принимаются.", parse_mode="HTML")


@dp.message(Command("top"))
@dp.message(F.text == "🏆 Топ")
async def cmd_top(message: Message):
    rows = await get_top(15)
    if not rows:
        await message.answer("Пока нет ни одного результата. Будь первым — нажми «➕ Добавить результат»")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏆 <b>Топ результатов — {TRACK}</b>\n"]
    for i, row in enumerate(rows, 1):
        name = f"@{row['username']}" if row["username"] else row["full_name"]
        lines.append(f"{medals.get(i, f'{i}.')} <b>{row['time_display']}</b> — {row['car']}\n👤 {name}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("pilots"))
@dp.message(F.text == "👤 Пилоты")
async def cmd_pilots(message: Message):
    rows = await get_pilots_ranking_sorted(15)
    if not rows:
        await message.answer("Пока нет ни одного результата.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"👤 <b>Рейтинг пилотов — {TRACK}</b>\n<i>один лучший результат на пилота</i>\n"]
    for i, row in enumerate(rows, 1):
        name = f"@{row['username']}" if row["username"] else row["full_name"]
        lines.append(f"{medals.get(i, f'{i}.')} <b>{row['time_display']}</b> — {row['car']}\n👤 {name}\n")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Подробнее о пилоте", callback_data="show_pilots_list")]])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "show_pilots_list")
async def show_pilots_list(callback: CallbackQuery):
    rows = await get_pilots_ranking_sorted(30)
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
    user, results = await get_pilot_stats(user_id)
    if not user or not results:
        await callback.answer("Пилот не найден", show_alert=True)
        return
    name = f"@{user['username']}" if user["username"] else user["full_name"]
    best = results[0]
    leader_ms = await get_leader_time_ms()
    if leader_ms is not None:
        delta = best["time_ms"] - leader_ms
        delta_text = "\n🏆 Лидер общего зачёта" if delta == 0 else f"\n📉 До лидера: <b>+{delta / 1000:.3f} с</b>"
    else:
        delta_text = ""
    lines = [f"👤 <b>{name}</b>\n", f"⏱ Личный рекорд: <b>{best['time_display']}</b> — {best['car']}", delta_text, f"\n📊 Всего результатов: {len(results)}\n", "<b>Результаты:</b>"]
    for i, r in enumerate(results[:10], 1):
        lines.append(f"{i}. {r['time_display']} — {r['car']}")
    if len(results) > 10:
        lines.append(f"… и ещё {len(results) - 10}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


@dp.message(Command("my"))
@dp.message(F.text == "📋 Мои результаты")
async def cmd_my(message: Message):
    rows = await get_user_results(message.from_user.id)
    if not rows:
        await message.answer("У тебя пока нет результатов. Добавь первый — нажми «➕ Добавить результат»")
        return
    lines = ["📋 <b>Твои результаты</b>\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. <b>{row['time_display']}</b> — {row['car']}")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить результат", callback_data="show_delete_list")]])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "show_delete_list")
async def show_delete_list(callback: CallbackQuery):
    rows = await get_user_results(callback.from_user.id)
    if not rows:
        await callback.answer("Нет результатов для удаления", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {row['time_display']} — {row['car']}", callback_data=f"del_my:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_my")])
    await callback.message.edit_text("Выбери результат, который хочешь удалить:\n(можно удалить только свои)", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data == "back_to_my")
async def back_to_my(callback: CallbackQuery):
    rows = await get_user_results(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("У тебя больше нет результатов.")
        await callback.answer()
        return
    lines = ["📋 <b>Твои результаты</b>\n"] + [f"{i}. <b>{row['time_display']}</b> — {row['car']}" for i, row in enumerate(rows, 1)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить результат", callback_data="show_delete_list")]])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_my:"))
async def confirm_delete_my_result(callback: CallbackQuery):
    result_id = int(callback.data.split(":")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_del:{result_id}"), InlineKeyboardButton(text="❌ Отмена", callback_data="show_delete_list")]])
    await callback.message.edit_text("Точно удалить этот результат?", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_del:"))
async def delete_my_result(callback: CallbackQuery):
    result_id = int(callback.data.split(":")[1])
    deleted = await delete_result(result_id, user_id=callback.from_user.id)
    if deleted:
        await callback.answer("✅ Удалено")
        rows = await get_user_results(callback.from_user.id)
        if not rows:
            await callback.message.edit_text("Все твои результаты удалены.")
            return
        buttons = [[InlineKeyboardButton(text=f"🗑 {row['time_display']} — {row['car']}", callback_data=f"del_my:{row['id']}")] for row in rows]
        buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_my")])
        await callback.message.edit_text("Выбери результат, который хочешь удалить:\n(можно удалить только свои)", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await callback.answer("Не удалось удалить (уже удалён или не твой)", show_alert=True)


@dp.message(Command("cars"))
@dp.message(F.text == "🏎 Машины")
async def cmd_cars(message: Message):
    rows = await get_all_cars()
    if not rows:
        await message.answer("Пока нет ни одной машины в базе.")
        return
    lines = ["🏎 <b>Машины в базе</b>\n"]
    for row in rows:
        best = format_time(row["best_ms"]) if row["best_ms"] else "—"
        lines.append(f"• <b>{row['name_original']}</b>\n  {row['count']} рез., лучшее: {best}\n")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Подробнее о машине", callback_data="show_cars_list")]])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "show_cars_list")
async def show_cars_list(callback: CallbackQuery):
    rows = await get_all_cars()
    if not rows:
        await callback.answer("Нет машин в базе", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"ℹ️ {row['name_original']}", callback_data=f"car_info:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_cars")])
    await callback.message.edit_text("Выбери машину, чтобы посмотреть подробности:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data == "back_to_cars")
async def back_to_cars(callback: CallbackQuery):
    rows = await get_all_cars()
    if not rows:
        await callback.message.edit_text("Пока нет ни одной машины в базе.")
        await callback.answer()
        return
    lines = ["🏎 <b>Машины в базе</b>\n"]
    for row in rows:
        best = format_time(row["best_ms"]) if row["best_ms"] else "—"
        lines.append(f"• <b>{row['name_original']}</b>\n  {row['count']} рез., лучшее: {best}\n")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Подробнее о машине", callback_data="show_cars_list")]])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("car_info:"))
async def show_car_info(callback: CallbackQuery):
    car = await get_car_details(int(callback.data.split(":")[1]))
    if not car:
        await callback.answer("Машина не найдена", show_alert=True)
        return
    best = format_time(car["best_ms"]) if car["best_ms"] else "—"
    text = f"🏎 <b>{car['name_original']}</b>\n\n📊 Результатов: {car['results_count']}\n⏱ Лучшее время: {best}\n\n"
    if car["power_hp"]:
        text += f"⚡ Мощность: {car['power_hp']} л.с.\n"
    if car["torque_nm"]:
        text += f"🔧 Момент: {car['torque_nm']} Н·м\n"
    if car.get("transmission"):
        text += f"⚙️ КПП: {car['transmission']}\n"
    if car["drivetrain"]:
        text += f"🔄 Привод: {car['drivetrain']}\n"
    if car["photo_file_id"]:
        await callback.message.answer_photo(photo=car["photo_file_id"], caption=text, parse_mode="HTML")
    else:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.message(Command("editcar"))
async def cmd_editcar(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return
    rows = await get_all_cars()
    if not rows:
        await message.answer("В базе нет машин.")
        return
    buttons = [[InlineKeyboardButton(text=row["name_original"], callback_data=f"editcar_select:{row['id']}")] for row in rows]
    await state.set_state(EditCar.choosing_car)
    await message.answer("Выбери машину для редактирования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("editcar_select:"))
async def editcar_selected(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.update_data(edit_car_id=int(callback.data.split(":")[1]))
    await state.set_state(EditCar.waiting_photo)
    await callback.message.edit_text("Отправь <b>фото</b> машины.\nИли напиши <code>-</code>, чтобы пропустить.", parse_mode="HTML")
    await callback.answer()


@dp.message(EditCar.waiting_photo, F.photo)
async def editcar_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_car_info(data["edit_car_id"], photo_file_id=message.photo[-1].file_id)
    await state.set_state(EditCar.waiting_power)
    await message.answer("✅ Фото сохранено.\n\nТеперь введи <b>мощность</b> в л.с. (только число).\nИли <code>-</code> чтобы пропустить.", parse_mode="HTML")


@dp.message(EditCar.waiting_photo)
async def editcar_photo_skip(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "-":
        await state.set_state(EditCar.waiting_power)
        await message.answer("Фото пропущено.\n\nВведи <b>мощность</b> в л.с. (только число).\nИли <code>-</code> чтобы пропустить.", parse_mode="HTML")
    else:
        await message.answer("Отправь фото или напиши <code>-</code>", parse_mode="HTML")


@dp.message(EditCar.waiting_power)
async def editcar_power(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    data = await state.get_data()
    if text == "-":
        pass
    elif text.isdigit():
        await update_car_info(data["edit_car_id"], power_hp=int(text))
    else:
        await message.answer("Введи число или <code>-</code>", parse_mode="HTML")
        return
    await state.set_state(EditCar.waiting_torque)
    await message.answer("Теперь введи <b>крутящий момент</b> в Н·м (только число).\nИли <code>-</code> чтобы пропустить.", parse_mode="HTML")


@dp.message(EditCar.waiting_torque)
async def editcar_torque(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    data = await state.get_data()
    if text == "-":
        pass
    elif text.isdigit():
        await update_car_info(data["edit_car_id"], torque_nm=int(text))
    else:
        await message.answer("Введи число или <code>-</code>", parse_mode="HTML")
        return
    await state.set_state(EditCar.waiting_transmission)
    await message.answer("Теперь укажи <b>трансмиссию</b>:\n• MT — механика\n• AT — автомат\n• DCT — робот с двумя сцеплениями\n• SEQ — секвенталка\n• CVT — вариатор\n\nИли <code>-</code> чтобы пропустить.", parse_mode="HTML")


@dp.message(EditCar.waiting_transmission)
async def editcar_transmission(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    mapping = {"MT": "MT", "AT": "AT", "DCT": "DCT", "SEQ": "SEQ", "CVT": "CVT", "ВАРИАТОР": "CVT"}
    key = raw.upper() if raw != "-" else "-"
    if key == "-":
        pass
    elif key in mapping:
        await update_car_info(data["edit_car_id"], transmission=mapping[key])
    else:
        await message.answer("Напиши MT, AT, DCT, SEQ, CVT (или «вариатор») либо <code>-</code>", parse_mode="HTML")
        return
    await state.set_state(EditCar.waiting_drivetrain)
    await message.answer("Теперь укажи <b>привод</b>:\n• FWD — передний\n• RWD — задний\n• AWD — полный\n• 4WD — полный (подключаемый)\n\nИли <code>-</code> чтобы пропустить.", parse_mode="HTML")


@dp.message(EditCar.waiting_drivetrain)
async def editcar_drivetrain(message: Message, state: FSMContext):
    text = (message.text or "").strip().upper()
    data = await state.get_data()
    if text == "-":
        pass
    elif text in ("FWD", "RWD", "AWD", "4WD"):
        await update_car_info(data["edit_car_id"], drivetrain=text)
    else:
        await message.answer("Напиши FWD, RWD, AWD, 4WD или <code>-</code>", parse_mode="HTML")
        return
    await state.clear()
    car = await get_car_details(data["edit_car_id"])
    await message.answer(f"✅ Данные машины <b>{car['name_original']}</b> обновлены!\nПосмотреть можно через /cars → «Подробнее».", parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command("delresult"))
async def cmd_delresult(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Использование:\n<code>/delresult ID</code>", parse_mode="HTML")
        return
    result_id = int(args[1].strip())
    if await delete_result(result_id):
        await message.answer(f"✅ Результат #{result_id} удалён.")
    else:
        await message.answer(f"❌ Результат #{result_id} не найден.")


@dp.message(Command("delcar"))
async def cmd_delcar(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return
    rows = await get_all_cars()
    if not rows:
        await message.answer("В базе нет машин.")
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {row['name_original']} ({row['count']} рез.)", callback_data=f"admin_delcar:{row['id']}")] for row in rows]
    await message.answer("Выбери машину для удаления:\n(удалятся все результаты этой машины)", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin_delcar:"))
async def admin_delete_car(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    deleted_count = await delete_car(int(callback.data.split(":")[1]))
    await callback.message.edit_text(f"✅ Машина удалена.\nУдалено результатов: {deleted_count}")
    await callback.answer("Удалено")


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
