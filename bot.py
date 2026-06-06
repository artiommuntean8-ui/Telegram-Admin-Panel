import os
from dotenv import load_dotenv
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramForbiddenError

# Конфигурация
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin_secret")  # Секретное слово для регистрации
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
        # Таблица операторов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS operators (
                user_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'online' -- online, break, busy
            )
        ''')
        # Таблица тикетов (диалогов)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                operator_id INTEGER,
                status TEXT DEFAULT 'waiting' -- waiting, active, closed
            )
        ''')
        await db.commit()
        logger.info("База данных инициализирована.")

# --- Вспомогательные функции БД ---

async def get_active_ticket(user_id: int, is_operator=False):
    async with aiosqlite.connect(DB_PATH) as db:
        col = "operator_id" if is_operator else "client_id"
        async with db.execute(f'SELECT id, client_id, operator_id FROM tickets WHERE {col} = ? AND status = "active"', (user_id,)) as cursor:
            return await cursor.fetchone()

async def assign_ticket_to_operator(operator_id: int):
    """Берет самого старого клиента из очереди и назначает оператору."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, client_id FROM tickets WHERE status = "waiting" ORDER BY id ASC LIMIT 1') as cursor:
            ticket = await cursor.fetchone()
            if ticket:
                ticket_id, client_id = ticket
                await db.execute('UPDATE tickets SET operator_id = ?, status = "active" WHERE id = ?', (operator_id, ticket_id))
                await db.execute('UPDATE operators SET status = "busy" WHERE user_id = ?', (operator_id,))
                await db.commit()
                return client_id
    return None

# --- Инициализация бота и диспетчера ---

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- Обработка сообщений ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    """Регистрация оператора или приветствие клиента."""
    if command.args == ADMIN_SECRET:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('INSERT OR REPLACE INTO operators (user_id, status) VALUES (?, "online")', (message.from_user.id,))
            await db.commit()
        await message.answer("Вы зарегистрированы как оператор. Статус: В сети.")
        return

    await message.answer("Добро пожаловать в службу поддержки! Напишите ваш вопрос, и мы ответим вам в ближайшее время.")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Переключение статуса оператора."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT status FROM operators WHERE user_id = ?', (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return
            
            new_status = "break" if row[0] == "online" else "online"
            await db.execute('UPDATE operators SET status = ? WHERE user_id = ?', (new_status, message.from_user.id))
            await db.commit()
            
            if new_status == "online":
                client_id = await assign_ticket_to_operator(message.from_user.id)
                if client_id:
                    await message.answer(f"Вы вышли с перерыва. Новый клиент: {client_id}")
                    await bot.send_message(client_id, "Оператор подключился к чату.")
                    return
            
            await message.answer(f"Ваш статус изменен на: {'На перерыве' if new_status == 'break' else 'В сети'}")

@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    """Закрытие текущего тикета оператором."""
    ticket = await get_active_ticket(message.from_user.id, is_operator=True)
    if not ticket:
        await message.answer("У вас нет активных диалогов.")
        return

    ticket_id, client_id, _ = ticket
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE tickets SET status = "closed" WHERE id = ?', (ticket_id,))
        await db.execute('UPDATE operators SET status = "online" WHERE user_id = ?', (message.from_user.id,))
        await db.commit()

    await message.answer(f"Диалог с пользователем {client_id} завершен.")
    try:
        await bot.send_message(client_id, "Оператор завершил диалог. Спасибо за обращение!")
    except: pass

    # Проверяем очередь после закрытия
    next_client = await assign_ticket_to_operator(message.from_user.id)
    if next_client:
        await message.answer(f"Следующий клиент из очереди: {next_client}")
        await bot.send_message(next_client, "Оператор подключился к чату.")

@dp.message(F.chat.type == "private")
async def handle_messages(message: types.Message):
    """Маршрутизация сообщений между клиентом и оператором."""
    user_id = message.from_user.id

    # Проверяем, не оператор ли это
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT status FROM operators WHERE user_id = ?', (user_id,)) as cursor:
            is_op = await cursor.fetchone()

    if is_op:
        # Логика оператора
        ticket = await get_active_ticket(user_id, is_operator=True)
        if ticket:
            try:
                await message.copy_to(chat_id=ticket[1])
            except Exception:
                await message.answer("Не удалось отправить сообщение клиенту.")
        return

    # Логика клиента
    ticket = await get_active_ticket(user_id, is_operator=False)
    if ticket:
        if ticket[2]: # Если оператор назначен
            await message.copy_to(chat_id=ticket[2])
        return

    # Если активного тикета нет, проверяем, нет ли уже в очереди
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id FROM tickets WHERE client_id = ? AND status = "waiting"', (user_id,)) as cursor:
            if await cursor.fetchone():
                await message.answer("Ваш запрос уже в очереди. Пожалуйста, ожидайте свободного оператора.")
                return

        # Создаем новый тикет
        await db.execute('INSERT INTO tickets (client_id, status) VALUES (?, "waiting")', (user_id,))
        await db.commit()

    # Пытаемся сразу найти свободного оператора
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id FROM operators WHERE status = "online" LIMIT 1') as cursor:
            op_row = await cursor.fetchone()
            
    if op_row:
        op_id = op_row[0]
        await assign_ticket_to_operator(op_id)
        await message.answer("Оператор подключился. Можете описывать вашу проблему.")
        await bot.send_message(op_id, f"Новый клиент! ID: {user_id}. Напишите сообщение для ответа.")
    else:
        await message.answer("Все операторы заняты. Мы ответим вам в порядке очереди.")

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