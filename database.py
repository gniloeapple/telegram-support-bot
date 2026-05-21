import logging
import os
from datetime import datetime, timezone
from enum import Enum

import aiosqlite
import pytz
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))

raw_topic_id = os.getenv("SUPPORT_TOPIC_ID")
SUPPORT_TOPIC_ID = int(raw_topic_id) if raw_topic_id and raw_topic_id.strip().isdigit() else None

ADMINS = [int(admin_id.strip()) for admin_id in os.getenv("ADMINS", "").split(",") if admin_id.strip()]

MSK = pytz.timezone("Europe/Moscow")
DB_PATH = "support_bot.db"


# ── Константы вместо «магических строк» ──────────────────────────────────────

class TopicMode(str, Enum):
    PER_USER     = "per_user"
    SINGLE_TOPIC = "single_topic"

class TicketStatus(str, Enum):
    OPEN   = "open"
    CLOSED = "closed"


# ── Тексты по умолчанию ──────────────────────────────────────────────────────

DEFAULT_GREETING = (
    "Здравствуйте!\n\n"
    "Напишите Ваш вопрос, и мы ответим Вам в ближайшее время.\n\n"
    "🕘 Время работы поддержки: Пн - Вс, с 7:00 до 21:00 по МСК"
)
DEFAULT_HELP = (
    "🕘 Время работы поддержки: Пн - Вс, с 7:00 до 21:00 по МСК\n\n"
    "📝 Заполняйте тикет внимательно и кратко, но максимально подробно. "
    "Помните, что это не чат с техподдержкой в реальном времени. "
    "Все тикеты обрабатываются в порядке очереди.\n\n"
    "⌛️ Возможно придётся подождать некоторое время, "
    "прежде чем вы получите ответ на свой вопрос."
)
DEFAULT_WORK_TEXT = "💤 На данный момент мы не работаем. Ответим в рабочее время, спасибо."
DEFAULT_WORK_TIME = "10:00-18:00"


# ── Инициализация БД ─────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS messages_mapping (
            user_chat_id       INTEGER,
            user_message_id    INTEGER,
            support_message_id INTEGER,
            ticket_id          INTEGER,
            PRIMARY KEY(user_chat_id, user_message_id)
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_chat_id   INTEGER NOT NULL,
            username       TEXT,
            first_name     TEXT,
            status         TEXT NOT NULL DEFAULT 'open',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            topic_id       INTEGER
        );

        CREATE TABLE IF NOT EXISTS blocked_users (
            user_chat_id INTEGER PRIMARY KEY,
            blocked_at   TEXT NOT NULL,
            admin_id     INTEGER,
            reason       TEXT
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            setting_key   TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ticket_ratings (
            ticket_id    INTEGER PRIMARY KEY,
            user_chat_id INTEGER NOT NULL,
            rating       INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            rated_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS templates (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            title   TEXT NOT NULL,
            content TEXT NOT NULL
        );
        """)

        # Миграция: добавляем topic_id если нет
        async with db.execute("PRAGMA table_info(tickets)") as cur:
            columns = [row[1] async for row in cur]
        if "topic_id" not in columns:
            await db.execute("ALTER TABLE tickets ADD COLUMN topic_id INTEGER")
            logger.info("Добавлена колонка topic_id в таблицу tickets")

        # Миграция: добавляем reason в blocked_users если нет
        async with db.execute("PRAGMA table_info(blocked_users)") as cur:
            columns = [row[1] async for row in cur]
        if "reason" not in columns:
            await db.execute("ALTER TABLE blocked_users ADD COLUMN reason TEXT")
            logger.info("Добавлена колонка reason в таблицу blocked_users")

        await db.commit()


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def format_datetime(iso_string: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
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
        end_h, end_m     = map(int, end_str.strip().split(":"))
        now_msk          = datetime.now(MSK)
        now_min          = now_msk.hour * 60 + now_msk.minute
        start_min        = start_h * 60 + start_m
        end_min          = end_h   * 60 + end_m
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min
    except Exception as e:
        logger.error(f"Ошибка парсинга времени: {e}")
        return True

def validate_time_range(time_str: str) -> bool:
    """Проверяет, что строка соответствует формату HH:MM-HH:MM."""
    if not time_str or "-" not in time_str:
        return False
    try:
        start_str, end_str = time_str.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        return (0 <= sh < 24 and 0 <= sm < 60 and
                0 <= eh < 24 and 0 <= em < 60)
    except Exception:
        return False


# ── Настройки ────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()

async def get_topic_mode() -> str:
    return await get_setting("topic_mode", TopicMode.PER_USER)

async def set_topic_mode(mode: str):
    await set_setting("topic_mode", mode)


# ── Пользователи / блокировки ────────────────────────────────────────────────

async def is_user_blocked(user_chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,)
        ) as cur:
            return await cur.fetchone() is not None

async def toggle_user_block(user_chat_id: int, admin_id: int, reason: str = "") -> bool:
    """Возвращает True если пользователь теперь заблокирован."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,)
        ) as cur:
            blocked = await cur.fetchone() is not None

        if blocked:
            await db.execute("DELETE FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,))
            await db.commit()
            return False
        else:
            await db.execute(
                "INSERT INTO blocked_users (user_chat_id, blocked_at, admin_id, reason) VALUES (?, ?, ?, ?)",
                (user_chat_id, _now_iso(), admin_id, reason),
            )
            await db.commit()
            return True


# ── Тикеты ───────────────────────────────────────────────────────────────────

async def get_open_ticket(user_chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, topic_id FROM tickets WHERE user_chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (user_chat_id,),
        ) as cur:
            return await cur.fetchone()

async def create_ticket_in_db(user_chat_id: int, username: str, first_name: str, topic_id: int) -> int:
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tickets (user_chat_id, username, first_name, status, created_at, updated_at, topic_id) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (user_chat_id, username, first_name, now, now, topic_id),
        )
        await db.commit()
        return cur.lastrowid

async def update_ticket_status(ticket_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), ticket_id),
        )
        await db.commit()

async def get_ticket_info(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT topic_id, username, first_name, user_chat_id, status, created_at, updated_at "
            "FROM tickets WHERE id = ?",
            (ticket_id,),
        ) as cur:
            return await cur.fetchone()

async def get_all_open_tickets(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_chat_id, username, first_name, created_at, updated_at "
            "FROM tickets WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()

async def get_user_chat_id_by_ticket(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_chat_id FROM tickets WHERE id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── Маппинг сообщений ────────────────────────────────────────────────────────

async def save_mapping(user_chat_id, user_message_id, support_message_id, ticket_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO messages_mapping "
            "(user_chat_id, user_message_id, support_message_id, ticket_id) VALUES (?, ?, ?, ?)",
            (user_chat_id, user_message_id, support_message_id, ticket_id),
        )
        await db.commit()

async def find_user_by_support_message(support_message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_chat_id, user_message_id, ticket_id FROM messages_mapping "
            "WHERE support_message_id = ?",
            (support_message_id,),
        ) as cur:
            return await cur.fetchone()


# ── Рейтинги ─────────────────────────────────────────────────────────────────

async def save_rating(ticket_id: int, user_chat_id: int, rating: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ticket_ratings (ticket_id, user_chat_id, rating, rated_at) "
            "VALUES (?, ?, ?, ?)",
            (ticket_id, user_chat_id, rating, _now_iso()),
        )
        await db.commit()

async def get_rating(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rating FROM ticket_ratings WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── Шаблоны ответов ──────────────────────────────────────────────────────────

async def get_templates():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title, content FROM templates ORDER BY id") as cur:
            return await cur.fetchall()

async def add_template(title: str, content: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO templates (title, content) VALUES (?, ?)", (title, content)
        )
        await db.commit()
        return cur.lastrowid

async def delete_template(template_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        await db.commit()


# ── Статистика ───────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM tickets") as cur:
            total = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'") as cur:
            open_count = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM tickets WHERE date(created_at) = date('now')"
        ) as cur:
            today = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM tickets WHERE created_at >= datetime('now', '-7 days')"
        ) as cur:
            week = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM blocked_users") as cur:
            blocked = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT AVG(rating), COUNT(*) FROM ticket_ratings"
        ) as cur:
            row = await cur.fetchone()
            avg_rating, ratings_count = (row[0] or 0), row[1]

    return {
        "total":        total,
        "open":         open_count,
        "today":        today,
        "week":         week,
        "blocked":      blocked,
        "avg_rating":   round(avg_rating, 2),
        "ratings_count": ratings_count,
    }
