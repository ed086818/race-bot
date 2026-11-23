import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime
import os

# Получаем токен из секретов
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Ошибка: Не найден BOT_TOKEN в Secrets. Добавь его в настройках!")

DB_NAME = "races.db"

# Машина состояний для сбора данных
class RaceForm(StatesGroup):
    waiting_for_car = State()
    waiting_for_time = State()

# Инициализация БД
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                car_model TEXT NOT NULL,
                lap_time REAL NOT NULL,
                timestamp DATETIME NOT NULL
            )
        """)
        await db.commit()

# Сохранение заезда
async def add_lap(user_id: int, car_model: str, lap_time: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO laps (user_id, car_model, lap_time, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, car_model, lap_time, datetime.now().isoformat())
        )
        await db.commit()

# Получение статистики
async def get_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total, AVG(lap_time) AS avg_time,
                   MIN(lap_time) AS best_time, MAX(lap_time) AS worst_time
            FROM laps WHERE user_id = ?
            """,
            (user_id,)
        )
        return await cursor.fetchone()

# Получение последних заездов
async def get_last_laps(user_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT car_model, lap_time, timestamp
            FROM laps
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit)
        )
        return await cursor.fetchall()

# Настройка бота
bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для учёта гоночных заездов. \n"
        "Команды:\n"
        "/add — добавить заезд\n"
        "/stats — статистика\n"
        "/list — последние 5 заездов"
    )

@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(RaceForm.waiting_for_car)
    await message.answer("Напиши модель автомобиля (например, Honda Civic):")

@dp.message(RaceForm.waiting_for_car)
async def process_car(message: types.Message, state: FSMContext):
    car = message.text.strip()
    if not car:
        await message.answer("Пожалуйста, напиши название авто.")
        return
    await state.update_data(car=car)
    await state.set_state(RaceForm.waiting_for_time)
    await message.answer("Теперь укажи время круга в секундах (например, 95.5):")

@dp.message(RaceForm.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    text = message.text.strip().replace(",", ".")
    try:
        lap_time = float(text)
        if lap_time <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректное время. Напиши число больше 0 (например, 102.3).")
        return

    data = await state.get_data()
    car = data.get("car")
    await add_lap(message.from_user.id, car, lap_time)
    await state.clear()
    await message.answer(f"✅ Заезд сохранён: {car}, время {lap_time:.2f} сек.")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    stats = await get_stats(message.from_user.id)
    total = stats["total"]
    if total == 0:
        await message.answer("У тебя пока нет сохранённых заездов.")
        return
    
    response = (
        f"📊 Твоя статистика:\n"
        f"Всего заездов: {total}\n"
        f"Среднее время: {stats['avg_time']:.2f} сек\n"
        f"Лучший круг: {stats['best_time']:.2f} сек\n"
        f"Худший круг: {stats['worst_time']:.2f} сек"
    )
    await message.answer(response)

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    rows = await get_last_laps(message.from_user.id, limit=5)
    if not rows:
        await message.answer("Нет сохранённых заездов.")
        return
    
    lines = [f"{r['car_model']} — {r['lap_time']:.2f} сек ({r['timestamp'][:10]})" for r in rows]
    await message.answer("🏁 Последние заезды:\n" + "\n".join(lines))

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
