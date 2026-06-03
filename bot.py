import os
from dotenv import load_dotenv
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramForbiddenError

# Конфигурация
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = 'support_tickets.db'

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Асинхронная работа с базой данных ---

async def init_db():
    """Инициализация базы данных и создание таблицы тикетов."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                operator_message_id INTEGER NOT NULL
            )
        ''')
        await db.commit()
        logger.info("База данных инициализирована.")

async def save_ticket(user_id: int, operator_message_id: int):
    """Сохранение связи между пользователем и сообщением в группе операторов."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO tickets (user_id, operator_message_id) VALUES (?, ?)',
            (user_id, operator_message_id)
        )
        await db.commit()

async def get_user_id_by_message(operator_message_id: int):
    """Поиск ID пользователя по ID сообщения в группе."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT user_id FROM tickets WHERE operator_message_id = ?',
            (operator_message_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

# --- Инициализация бота и диспетчера ---

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- Обработка сообщений ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработка команды /start."""
    await message.answer("Добро пожаловать в службу поддержки! Напишите ваш вопрос, и мы ответим вам в ближайшее время.")

@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    """Пересылка сообщения от пользователя в группу поддержки."""
    # Пересылаем сообщение операторам
    forwarded_msg = await message.forward(chat_id=SUPPORT_GROUP_ID)
    
    # Сохраняем ID сообщения для возможности ответа
    await save_ticket(user_id=message.from_user.id, operator_message_id=forwarded_msg.message_id)
    logger.info(f"Сообщение от {message.from_user.id} переслано операторам.")

@dp.message(F.chat.id == SUPPORT_GROUP_ID, F.reply_to_message)
async def handle_operator_reply(message: types.Message):
    """Обработка ответа оператора (через Reply) и отправка пользователю."""
    # Пытаемся найти user_id, на сообщение которого ответил оператор
    user_id = await get_user_id_by_message(message.reply_to_message.message_id)
    
    if not user_id:
        return  # Сообщение не является ответом на тикет

    try:
        # Копируем сообщение оператора пользователю
        await message.copy_to(chat_id=user_id)
        await message.answer(f"✅ Ответ доставлен пользователю {user_id}")
    except TelegramForbiddenError:
        # Если пользователь заблокировал бота
        await message.reply("❌ Не удалось доставить ответ: пользователь заблокировал бота.")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {user_id}: {e}")
        await message.reply(f"❌ Произошла ошибка: {e}")

# --- Запуск бота ---

async def main():
    await init_db()
    logger.info("Запуск polling...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")