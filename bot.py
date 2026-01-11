import sqlite3
import logging
import os
from datetime import datetime, timezone
import pytz

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID"))

# Общий топик (используется в режиме single_topic)
raw_topic_id = os.getenv("SUPPORT_TOPIC_ID")
SUPPORT_TOPIC_ID = int(raw_topic_id) if raw_topic_id and raw_topic_id.strip().isdigit() else None

# Список админов из .env (через запятую)
ADMINS = [int(admin_id.strip()) for admin_id in os.getenv("ADMINS", "").split(",") if admin_id.strip()]

# Временная зона МСК
MSK = pytz.timezone('Europe/Moscow')

# Состояния для ConversationHandler
WAITING_GREETING, WAITING_HELP = range(2)

conn = sqlite3.connect("support_bot.db", check_same_thread=False)
cursor = conn.cursor()

# ---- таблица маппинга сообщений ----
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS messages_mapping (
    user_chat_id       INTEGER,
    user_message_id    INTEGER,
    support_message_id INTEGER,
    ticket_id          INTEGER,
    PRIMARY KEY(user_chat_id, user_message_id)
)
"""
)

# ---- таблица тикетов ----
cursor.execute(
    """
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
"""
)

# ---- таблица заблокированных пользователей ----
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS blocked_users (
    user_chat_id INTEGER PRIMARY KEY,
    blocked_at   TEXT NOT NULL,
    admin_id     INTEGER
)
"""
)

# ---- таблица настроек бота ----
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS bot_settings (
    setting_key   TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
)
"""
)

# ---- МИГРАЦИЯ: Добавляем topic_id, если его нет ----
def add_column_if_not_exists(table_name: str, column_name: str, column_type: str):
    """Добавляет колонку в таблицу, если её ещё нет"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        logger.info(f"Добавлена колонка {column_name} в таблицу {table_name}")
    else:
        logger.info(f"Колонка {column_name} уже существует в таблице {table_name}")

add_column_if_not_exists("tickets", "topic_id", "INTEGER")

conn.commit()


# ----------------- Утилиты настроек -----------------
def get_setting(key: str, default: str = "") -> str:
    """Получает значение настройки из БД"""
    cursor.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    """Сохраняет значение настройки в БД"""
    cursor.execute(
        "INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def get_topic_mode() -> str:
    """Возвращает режим топиков: 'per_user' или 'single_topic'"""
    return get_setting("topic_mode", "per_user")


def set_topic_mode(mode: str):
    """Устанавливает режим топиков"""
    set_setting("topic_mode", mode)


# Дефолтные тексты
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


# ----------------- Утилиты -----------------
def format_datetime(iso_string: str) -> str:
    """Конвертирует ISO datetime в читаемый формат МСК"""
    try:
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_msk = dt.astimezone(MSK)
        return dt_msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso_string


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь админом"""
    return user_id in ADMINS


# ----------------- Работа с БД / Блокировка -----------------

def is_user_blocked(user_chat_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь"""
    cursor.execute("SELECT 1 FROM blocked_users WHERE user_chat_id = ?", (user_chat_id,))
    return cursor.fetchone() is not None


def toggle_user_block(user_chat_id: int, admin_id: int) -> bool:
    """
    Блокирует или разблокирует пользователя.
    """
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


# ----------------- Работа с БД / тикетами -----------------
def get_open_ticket(user_chat_id: int):
    """Возвращает ID и topic_id открытого тикета пользователя"""
    cursor.execute(
        """
        SELECT id, topic_id FROM tickets
        WHERE user_chat_id = ? AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_chat_id,),
    )
    row = cursor.fetchone()
    return row if row else None


async def create_ticket(context: ContextTypes.DEFAULT_TYPE, user_chat_id: int, username: str = None, first_name: str = None) -> tuple:
    """Создает тикет и топик в форуме (если режим per_user)"""
    now = datetime.now(timezone.utc).isoformat()
    topic_mode = get_topic_mode()
    
    topic_id = None
    
    # Создаем отдельный топик только в режиме per_user
    if topic_mode == "per_user":
        display_name = username if username else (first_name if first_name else f"User{user_chat_id}")
        topic_name = f"🟢 {display_name}"
        
        try:
            forum_topic = await context.bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                name=topic_name[:128]
            )
            topic_id = forum_topic.message_thread_id
            logger.info(f"Создан топик {topic_id} для пользователя {user_chat_id}")
        except Exception as e:
            logger.error(f"Ошибка создания топика: {e}")
    
    # Сохраняем тикет с topic_id
    cursor.execute(
        """
        INSERT INTO tickets (user_chat_id, username, first_name, status, created_at, updated_at, topic_id)
        VALUES (?, ?, ?, 'open', ?, ?, ?)
        """,
        (user_chat_id, username, first_name, now, now, topic_id),
    )
    conn.commit()
    ticket_id = cursor.lastrowid
    
    # Отправляем информацию о пользователе в топик (только для per_user режима)
    if topic_mode == "per_user" and topic_id:
        username_display = f"@{username}" if username else "Не указан"
        user_info = (
            f"👤 <b>Информация о пользователе</b>\n\n"
            f"🆔 ID: <code>{user_chat_id}</code>\n"
            f"👤 Имя: {first_name or 'Не указано'}\n"
            f"📱 Username: {username_display}\n"
            f"🎫 Тикет: #{ticket_id}"
        )
        try:
            await context.bot.send_message(
                chat_id=SUPPORT_CHAT_ID,
                message_thread_id=topic_id,
                text=user_info,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки информации о пользователе: {e}")
    
    return ticket_id, topic_id


def update_ticket_status(ticket_id: int, status: str):
    """Обновляет статус тикета"""
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """
        UPDATE tickets
        SET status = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, now, ticket_id),
    )
    conn.commit()


async def update_topic_status(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, status: str):
    """Обновляет название топика при изменении статуса (только для per_user режима)"""
    topic_mode = get_topic_mode()
    if topic_mode != "per_user":
        return
    
    cursor.execute(
        """
        SELECT topic_id, username, first_name, user_chat_id FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return
    
    topic_id, username, first_name, user_chat_id = row
    
    status_emoji = "🔴" if status == "closed" else "🟢"
    display_name = username if username else (first_name if first_name else f"User{user_chat_id}")
    topic_name = f"{status_emoji} {display_name}"
    
    try:
        await context.bot.edit_forum_topic(
            chat_id=SUPPORT_CHAT_ID,
            message_thread_id=topic_id,
            name=topic_name[:128]
        )
        logger.info(f"Обновлено название топика {topic_id} на '{topic_name}'")
    except Exception as e:
        logger.error(f"Ошибка обновления названия топика: {e}")


def get_ticket_by_support_message(support_message_id: int):
    cursor.execute(
        """
        SELECT ticket_id FROM messages_mapping
        WHERE support_message_id = ?
        """,
        (support_message_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def save_mapping(user_chat_id, user_message_id, support_message_id, ticket_id):
    cursor.execute(
        """
        INSERT OR REPLACE INTO messages_mapping (
            user_chat_id, user_message_id, support_message_id, ticket_id
        )
        VALUES (?, ?, ?, ?)
        """,
        (user_chat_id, user_message_id, support_message_id, ticket_id),
    )
    conn.commit()


def find_user_by_support_message(support_message_id):
    cursor.execute(
        """
        SELECT user_chat_id, user_message_id, ticket_id
        FROM messages_mapping
        WHERE support_message_id = ?
        """,
        (support_message_id,),
    )
    return cursor.fetchone()


def get_all_open_tickets(limit: int = 50):
    cursor.execute(
        """
        SELECT id, user_chat_id, username, first_name, created_at, updated_at
        FROM tickets
        WHERE status = 'open'
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cursor.fetchall()


def get_user_chat_id_by_ticket(ticket_id: int):
    cursor.execute(
        """
        SELECT user_chat_id FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_topic_id_by_ticket(ticket_id: int):
    """Возвращает topic_id по ID тикета"""
    cursor.execute(
        """
        SELECT topic_id FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


# ----------------- Хендлеры пользователя -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(update.effective_user.id):
        return
    
    greeting_text = get_setting("greeting", DEFAULT_GREETING)
    await update.message.reply_text(greeting_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(update.effective_user.id):
        return

    help_text = get_setting("help", DEFAULT_HELP)
    await update.message.reply_text(help_text)


async def forward_to_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = message.from_user
    user_chat_id = message.chat_id
    user_message_id = message.message_id

    if is_user_blocked(user_chat_id):
        return

    ticket_data = get_open_ticket(user_chat_id)
    new_ticket = False
    
    if ticket_data is None:
        ticket_id, topic_id = await create_ticket(context, user_chat_id, user.username, user.first_name)
        new_ticket = True
        await message.reply_text(
            f"✅ Ваш тикет #{ticket_id} создан. Оператор поддержки скоро ответит."
        )
    else:
        ticket_id, topic_id = ticket_data

    username = f"@{user.username}" if user.username else "Не указан"
    
    # В режиме single_topic показываем полную информацию о тикете
    topic_mode = get_topic_mode()
    if topic_mode == "single_topic" and new_ticket:
        header = (
            f"🎫 НОВЫЙ ТИКЕТ\n\n"
            f"🆔 Тикет: {ticket_id}\n"
            f"👤 Пользователь: {user.first_name or 'Не указано'}\n"
            f"🆔 Telegram ID: {user.id}\n"
            f"📱 Username: {username}"
        )
    else:
        header = f"💬 {user.first_name or 'Не указано'} ({username}):"

    send_kwargs = {
        "chat_id": SUPPORT_CHAT_ID,
    }
    
    # Определяем куда отправлять сообщение
    if topic_mode == "per_user" and topic_id:
        # Режим отдельных топиков - используем topic_id из тикета
        send_kwargs["message_thread_id"] = topic_id
    elif topic_mode == "single_topic" and SUPPORT_TOPIC_ID:
        # Режим общего топика - используем SUPPORT_TOPIC_ID
        send_kwargs["message_thread_id"] = SUPPORT_TOPIC_ID

    keyboard = [
        [InlineKeyboardButton("❌ Заблокировать/Разблокировать", callback_data=f"block_{user_chat_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    send_kwargs["reply_markup"] = reply_markup

    sent_message = None

    try:
        if message.photo:
            cap = message.caption or ""
            file_id = message.photo[-1].file_id
            caption_text = f"{header}\n\n{cap}" if cap else header
            sent_message = await context.bot.send_photo(
                photo=file_id,
                caption=caption_text,
                **send_kwargs,
            )
        elif message.video:
            cap = message.caption or ""
            caption_text = f"{header}\n\n{cap}" if cap else header
            sent_message = await context.bot.send_video(
                video=message.video.file_id,
                caption=caption_text,
                **send_kwargs,
            )
        elif message.document:
            cap = message.caption or ""
            caption_text = f"{header}\n\n{cap}" if cap else header
            sent_message = await context.bot.send_document(
                document=message.document.file_id,
                caption=caption_text,
                **send_kwargs,
            )
        elif message.voice:
            sent_message = await context.bot.send_voice(
                voice=message.voice.file_id,
                caption=header,
                **send_kwargs,
            )
        elif message.audio:
            cap = message.caption or ""
            caption_text = f"{header}\n\n{cap}" if cap else header
            sent_message = await context.bot.send_audio(
                audio=message.audio.file_id,
                caption=caption_text,
                **send_kwargs,
            )
        elif message.text:
            sent_message = await context.bot.send_message(
                text=f"{header}\n\n{message.text}",
                **send_kwargs,
            )
        else:
            return

        if sent_message:
            save_mapping(
                user_chat_id,
                user_message_id,
                sent_message.message_id,
                ticket_id,
            )
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения: {e}")


# ----------------- Хендлеры поддержки -----------------
async def reply_from_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.chat_id != SUPPORT_CHAT_ID:
        return
    if not message.reply_to_message:
        return

    replied_msg = message.reply_to_message
    found = find_user_by_support_message(replied_msg.message_id)
    if not found:
        return

    user_chat_id, user_message_id, ticket_id = found
    
    if is_user_blocked(user_chat_id):
        await message.reply_text("⛔️ Этот пользователь заблокирован. Он не получит сообщение.")
        return

    try:
        if message.photo:
            cap = message.caption or ""
            await context.bot.send_photo(
                chat_id=user_chat_id,
                photo=message.photo[-1].file_id,
                caption=cap,
            )
        elif message.video:
            cap = message.caption or ""
            await context.bot.send_video(
                chat_id=user_chat_id,
                video=message.video.file_id,
                caption=cap,
            )
        elif message.document:
            cap = message.caption or ""
            await context.bot.send_document(
                chat_id=user_chat_id,
                document=message.document.file_id,
                caption=cap,
            )
        elif message.voice:
            await context.bot.send_voice(
                chat_id=user_chat_id,
                voice=message.voice.file_id,
                caption=message.caption or "",
            )
        elif message.audio:
            cap = message.caption or ""
            await context.bot.send_audio(
                chat_id=user_chat_id,
                audio=message.audio.file_id,
                caption=cap,
            )
        elif message.text:
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=message.text,
            )

    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю: {e}")


# ----------------- Админ-панель -----------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin - показывает админ-панель"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return
    
    topic_mode = get_topic_mode()
    mode_text = "📁 Отдельный топик для каждого" if topic_mode == "per_user" else "📂 Общий топик"
    
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = await update.message.reply_text(
        "⚙️ Управление ботом",
        reply_markup=reply_markup
    )
    context.user_data['admin_menu_message_id'] = msg.message_id
    context.user_data['admin_menu_chat_id'] = msg.chat_id


async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает админ-панель и завершает conversation"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    topic_mode = get_topic_mode()
    mode_text = "📁 Отдельный топик для каждого" if topic_mode == "per_user" else "📂 Общий топик"
    
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    menu_msg_id = context.user_data.get('admin_menu_message_id')
    menu_chat_id = context.user_data.get('admin_menu_chat_id')
    
    if menu_msg_id and menu_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="⚙️ Управление ботом",
                reply_markup=reply_markup
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Ошибка редактирования меню: {e}")
    
    back_button_msg_id = context.user_data.get('back_button_message_id')
    if back_button_msg_id and menu_chat_id:
        try:
            await context.bot.delete_message(
                chat_id=menu_chat_id,
                message_id=back_button_msg_id
            )
        except Exception as e:
            logger.error(f"Ошибка удаления кнопки: {e}")
    
    return ConversationHandler.END


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки в админ-панели"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not is_admin(user_id):
        return
    
    context.user_data['admin_menu_message_id'] = query.message.message_id
    context.user_data['admin_menu_chat_id'] = query.message.chat_id
    
    back_keyboard = [
        [InlineKeyboardButton("◀️ В меню", callback_data="admin_back_to_menu")]
    ]
    back_markup = InlineKeyboardMarkup(back_keyboard)
    
    if query.data == "admin_edit_greeting":
        current_text = get_setting("greeting", DEFAULT_GREETING)
        msg = await query.message.reply_text(
            f"👉 Введите новое сообщение приветствия:\n\n"
            f"<b>Текущее приветствие:</b>\n{current_text}",
            parse_mode="HTML",
            reply_markup=back_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_GREETING
    
    elif query.data == "admin_edit_help":
        current_text = get_setting("help", DEFAULT_HELP)
        msg = await query.message.reply_text(
            f"👉 Введите новое сообщение помощи:\n\n"
            f"<b>Текущая информация:</b>\n{current_text}",
            parse_mode="HTML",
            reply_markup=back_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_HELP
    
    elif query.data == "admin_toggle_mode":
        # Переключаем режим топиков
        current_mode = get_topic_mode()
        new_mode = "single_topic" if current_mode == "per_user" else "per_user"
        set_topic_mode(new_mode)
        
        mode_text = "📁 Отдельный топик для каждого пользователя" if new_mode == "per_user" else "📂 Общий топик для всех"
        
        keyboard = [
            [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
            [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
            [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка обновления кнопок: {e}")


async def save_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет новое приветственное сообщение"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    new_text = update.message.text
    set_setting("greeting", new_text)
    
    await update.message.reply_text("✅ Приветственное сообщение успешно обновлено!")
    
    topic_mode = get_topic_mode()
    mode_text = "📁 Отдельный топик для каждого" if topic_mode == "per_user" else "📂 Общий топик"
    
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    menu_msg_id = context.user_data.get('admin_menu_message_id')
    menu_chat_id = context.user_data.get('admin_menu_chat_id')
    
    if menu_msg_id and menu_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="⚙️ Управление ботом",
                reply_markup=reply_markup
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Ошибка редактирования меню: {e}")
    
    back_button_msg_id = context.user_data.get('back_button_message_id')
    if back_button_msg_id and menu_chat_id:
        try:
            await context.bot.delete_message(
                chat_id=menu_chat_id,
                message_id=back_button_msg_id
            )
        except Exception as e:
            logger.error(f"Ошибка удаления кнопки: {e}")
    
    return ConversationHandler.END


async def save_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет новое сообщение помощи"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    new_text = update.message.text
    set_setting("help", new_text)
    
    await update.message.reply_text("✅ Сообщение помощи успешно обновлено!")
    
    topic_mode = get_topic_mode()
    mode_text = "📁 Отдельный топик для каждого" if topic_mode == "per_user" else "📂 Общий топик"
    
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    menu_msg_id = context.user_data.get('admin_menu_message_id')
    menu_chat_id = context.user_data.get('admin_menu_chat_id')
    
    if menu_msg_id and menu_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="⚙️ Управление ботом",
                reply_markup=reply_markup
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Ошибка редактирования меню: {e}")
    
    back_button_msg_id = context.user_data.get('back_button_message_id')
    if back_button_msg_id and menu_chat_id:
        try:
            await context.bot.delete_message(
                chat_id=menu_chat_id,
                message_id=back_button_msg_id
            )
        except Exception as e:
            logger.error(f"Ошибка удаления кнопки: {e}")
    
    return ConversationHandler.END


async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции редактирования"""
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END


# ----------------- Обработка кнопок -----------------
async def block_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("block_"):
        return
    
    try:
        target_user_id = int(data.split("_")[1])
    except (IndexError, ValueError):
        return
    
    admin_id = query.from_user.id
    
    is_blocked_now = toggle_user_block(target_user_id, admin_id)
    
    cursor.execute("SELECT username, first_name FROM tickets WHERE user_chat_id = ? ORDER BY id DESC LIMIT 1", (target_user_id,))
    res = cursor.fetchone()
    if res:
        username, first_name = res
        username_str = f"@{username}" if username else "без юзернейма"
        user_info = f"{first_name or 'Пользователь'} ({username_str})"
    else:
        user_info = f"Пользователь {target_user_id}"

    if is_blocked_now:
        text = f"👨 {user_info}\n❗️ Пользователь заблокирован"
    else:
        text = f"👨 {user_info}\n❗️ Пользователь разблокирован"

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        message_thread_id=query.message.message_thread_id,
        text=text
    )


# --------- Команды для операторов в чате поддержки ---------
async def open_tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.chat_id != SUPPORT_CHAT_ID:
        return

    rows = get_all_open_tickets()

    if not rows:
        await message.reply_text("Открытых тикетов нет ✅")
        return

    lines = ["📂 Открытые тикеты:\n"]
    for ticket_id, user_chat_id, username, first_name, created_at, updated_at in rows:
        created_fmt = format_datetime(created_at)
        username_display = f"@{username}" if username else "Не указан"
        first_name_display = first_name or "Не указано"
        
        lines.append(
            f"🎫 Тикет #{ticket_id}\n"
            f"👤 {first_name_display}\n"
            f"📱 {username_display}\n"
            f"🆔 ID: {user_chat_id}\n"
            f"📅 Создан: {created_fmt}\n"
        )

    text = "\n".join(lines)
    await message.reply_text(text)


async def close_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != SUPPORT_CHAT_ID:
        return
    if not message.reply_to_message:
        await message.reply_text("Команду /close нужно вызывать ответом на сообщение тикета.")
        return

    ticket_id = get_ticket_by_support_message(message.reply_to_message.message_id)
    if not ticket_id:
        await message.reply_text("Не удалось определить тикет для этого сообщения.")
        return

    user_chat_id = get_user_chat_id_by_ticket(ticket_id)
    
    update_ticket_status(ticket_id, "closed")
    await update_topic_status(context, ticket_id, "closed")
    await message.reply_text(f"✅ Тикет #{ticket_id} закрыт.")
    
    if user_chat_id:
        try:
            await context.bot.send_message(
                chat_id=user_chat_id,
                text="✅ Обращение завершено"
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления пользователю {user_chat_id}: {e}")


async def reopen_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != SUPPORT_CHAT_ID:
        return
    if not message.reply_to_message:
        await message.reply_text("Команду /reopen нужно вызывать ответом на сообщение тикета.")
        return

    ticket_id = get_ticket_by_support_message(message.reply_to_message.message_id)
    if not ticket_id:
        await message.reply_text("Не удалось определить тикет для этого сообщения.")
        return

    update_ticket_status(ticket_id, "open")
    await update_topic_status(context, ticket_id, "open")
    await message.reply_text(f"♻️ Тикет #{ticket_id} снова открыт.")


async def ticket_info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != SUPPORT_CHAT_ID:
        return
    if not message.reply_to_message:
        await message.reply_text("Команду /ticket нужно вызывать ответом на сообщение тикета.")
        return

    ticket_id = get_ticket_by_support_message(message.reply_to_message.message_id)
    if not ticket_id:
        await message.reply_text("Не удалось определить тикет для этого сообщения.")
        return

    cursor.execute(
        """
        SELECT user_chat_id, status, created_at, updated_at
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    )
    row = cursor.fetchone()
    if not row:
        await message.reply_text("Тикет не найден в базе.")
        return

    user_chat_id, status, created_at, updated_at = row
    created_fmt = format_datetime(created_at)
    updated_fmt = format_datetime(updated_at)
    
    is_blocked = is_user_blocked(user_chat_id)
    block_status = "ДА ⛔️" if is_blocked else "НЕТ ✅"

    text = (
        f"📄 Тикет #{ticket_id}\n"
        f"Пользователь: {user_chat_id}\n"
        f"Статус тикета: {status}\n"
        f"Заблокирован: {block_status}\n"
        f"Создан: {created_fmt}\n"
        f"Обновлён: {updated_fmt}"
    )
    await message.reply_text(text)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


def main():
    application = Application.builder().token(TOKEN).build()

    # ConversationHandler для админ-панели
    admin_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback_handler, pattern="^admin_edit_")
        ],
        states={
            WAITING_GREETING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_greeting),
                CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$")
            ],
            WAITING_HELP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_help),
                CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_admin),
            CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$")
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))

    # команды для операторов
    application.add_handler(CommandHandler("close", close_ticket_cmd))
    application.add_handler(CommandHandler("reopen", reopen_ticket_cmd))
    application.add_handler(CommandHandler("ticket", ticket_info_cmd))
    application.add_handler(CommandHandler("open_tickets", open_tickets_cmd))

    # Conversation handler для админки
    application.add_handler(admin_conv_handler)

    # Обработчик кнопки переключения режима топиков
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_toggle_mode$"))
    
    # Обработчик нажатия на кнопку Block/Unblock
    application.add_handler(CallbackQueryHandler(block_user_callback, pattern="^block_"))

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.ALL ^ filters.COMMAND),
            forward_to_support,
        )
    )
    application.add_handler(MessageHandler(filters.REPLY, reply_from_support))

    application.add_error_handler(error_handler)

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()