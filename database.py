import sqlite3
import logging
import os
from datetime import datetime, timezone
import pytz
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))

raw_topic_id = os.getenv("SUPPORT_TOPIC_ID")
SUPPORT_TOPIC_ID = int(raw_topic_id) if raw_topic_id and raw_topic_id.strip().isdigit() else None

ADMINS = [int(admin_id.strip()) for admin_id in os.getenv("ADMINS", "").split(",") if admin_id.strip()]

MSK = pytz.timezone('Europe/Moscow')

DEFAULT_GREETING = (
    "Здравствуйте!\n\n"
    "Напишите Ваш вопрос, и мы ответим Вам в ближайшее время.\n\n"
    "🕘 Время работы поддержки: Пн - Вс, с 7:00 до 21:00 по МСК"
)
DEFAULT_HELP = (
    "🕘 Время работы поддержки: Пн - Вс, с 7:00 до 21:00 по МСК\n\n"
    "📝 Заполняйте тикет внимательно и кратко, но максимально подробно. "
    "Помните, что это не чат с техподдержкой в реальном времени. Все тикеты обрабатываются в порядке очереди.\n\n"
    "⌛️ Возможно придётся подождать некоторое время, прежде чем вы получите ответ на свой вопрос."
)
DEFAULT_WORK_TEXT = "💤 😴 На данный момент мы неработаем. Ответим в рабочее время, спасибо."
DEFAULT_WORK_TIME = "10:00-18:00"

# Подключение к БД
conn = sqlite3.connect("support_bot.db", check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages_mapping (
        user_chat_id       INTEGER,
        user_message_id    INTEGER,
        support_message_id INTEGER,
        ticket_id          INTEGER,
        PRIMARY KEY(user_chat_id, user_message_id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_chat_id   INTEGER NOT NULL,
        username       TEXT,
        first_name     TEXT,
        status         TEXT NOT NULL DEFAULT 'open',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        topic_id       INTEGER
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS blocked_users (
        user_chat_id INTEGER PRIMARY KEY,
        blocked_at   TEXT NOT NULL,
        admin_id     INTEGER
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        setting_key   TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL
    )
    """)
    
    # Миграция
    cursor.execute(f"PRAGMA table_info(tickets)")
    columns = [row[1] for row in cursor.fetchall()]
    if "topic_id" not in columns:
        cursor.execute(f"ALTER TABLE tickets ADD COLUMN topic_id INTEGER")
        logger.info("Добавлена колонка topic_id в таблицу tickets")
        
    conn.commit()

init_db()

# --- Базовые утилиты и Настройки ---
def get_setting(key: str, default: str = "") -> str:
    cursor.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def set_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)", (key, value))
    conn.commit()

def get_topic_mode() -> str:
    return get_setting("topic_mode", "per_user")

def set_topic_mode(mode: str):
    set_setting("topic_mode", mode)

def format_datetime(iso_string: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_msk = dt.astimezone(MSK)
        return dt_msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso_string

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def is_working_hours(time_str: str) -> bool:
    if not time_str or "-" not in time_str:
        return True
    try:
        start_str, end_str = time_str.split("-")
        start_h, start_m = map(int, start_str.strip().split(":"))
        end_h, end_m = map(int, end_str.strip().split(":"))

        now_msk = datetime.now(MSK)
        now_minutes = now_msk.hour * 60 + now_msk.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes <= end_minutes
        else:
            return now_minutes >= start_minutes or now_minutes <= end_minutes
    except Exception as e:
        logger.error(f"Ошибка парсинга времени: {e}")
        return True

# --- Работа с пользователями ---
def is_user_blocked(user_chat_id: int) -> bool:
    cursor.execute("SELECT 1 FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,))
    return cursor.fetchone() is not None

def toggle_user_block(user_chat_id: int, admin_id: int) -> bool:
    if is_user_blocked(user_chat_id):
        cursor.execute("DELETE FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,))
        conn.commit()
        return False
    else:
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO blocked_users (user_chat_id, blocked_at, admin_id) VALUES (?, ?, ?)",
            (user_chat_id, now, admin_id),
        )
        conn.commit()
        return True

# --- Работа с тикетами ---
def get_open_ticket(user_chat_id: int):
    cursor.execute(
        "SELECT id, topic_id FROM tickets WHERE user_chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (user_chat_id,),
    )
    return cursor.fetchone()

def create_ticket_in_db(user_chat_id: int, username: str, first_name: str, topic_id: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO tickets (user_chat_id, username, first_name, status, created_at, updated_at, topic_id) VALUES (?, ?, ?, 'open', ?, ?, ?)",
        (user_chat_id, username, first_name, now, now, topic_id),
    )
    conn.commit()
    return cursor.lastrowid

def update_ticket_status(ticket_id: int, status: str):
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, now, ticket_id))
    conn.commit()

def get_ticket_info(ticket_id: int):
    cursor.execute("SELECT topic_id, username, first_name, user_chat_id, status, created_at, updated_at FROM tickets WHERE id = ?", (ticket_id,))
    return cursor.fetchone()

def get_ticket_by_support_message(support_message_id: int):
    cursor.execute("SELECT ticket_id FROM messages_mapping WHERE support_message_id = ?", (support_message_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def save_mapping(user_chat_id, user_message_id, support_message_id, ticket_id):
    cursor.execute(
        "INSERT OR REPLACE INTO messages_mapping (user_chat_id, user_message_id, support_message_id, ticket_id) VALUES (?, ?, ?, ?)",
        (user_chat_id, user_message_id, support_message_id, ticket_id),
    )
    conn.commit()

def find_user_by_support_message(support_message_id):
    cursor.execute(
        "SELECT user_chat_id, user_message_id, ticket_id FROM messages_mapping WHERE support_message_id = ?",
        (support_message_id,),
    )
    return cursor.fetchone()

def get_all_open_tickets(limit: int = 50):
    cursor.execute(
        "SELECT id, user_chat_id, username, first_name, created_at, updated_at FROM tickets WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return cursor.fetchall()

def get_user_chat_id_by_ticket(ticket_id: int):
    cursor.execute("SELECT user_chat_id FROM tickets WHERE id = ?", (ticket_id,))
    row = cursor.fetchone()
    return row[0] if row else None