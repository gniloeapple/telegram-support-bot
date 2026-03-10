import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

import database as db
from admin import admin_command, get_admin_conv_handler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


async def create_ticket(context: ContextTypes.DEFAULT_TYPE, user_chat_id: int, username: str = None, first_name: str = None) -> tuple:
    topic_mode = db.get_topic_mode()
    topic_id = None
    
    if topic_mode == "per_user":
        display_name = username if username else (first_name if first_name else f"User{user_chat_id}")
        topic_name = f"🟢 {display_name}"
        try:
            forum_topic = await context.bot.create_forum_topic(chat_id=db.SUPPORT_CHAT_ID, name=topic_name[:128])
            topic_id = forum_topic.message_thread_id
        except Exception as e:
            logger.error(f"Ошибка создания топика: {e}")
    
    ticket_id = db.create_ticket_in_db(user_chat_id, username, first_name, topic_id)
    
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
            await context.bot.send_message(chat_id=db.SUPPORT_CHAT_ID, message_thread_id=topic_id, text=user_info, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки информации: {e}")
    
    return ticket_id, topic_id

async def update_topic_status(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, status: str):
    if db.get_topic_mode() != "per_user":
        return
        
    ticket_info = db.get_ticket_info(ticket_id)
    if not ticket_info or not ticket_info[0]:
        return
        
    topic_id, username, first_name, user_chat_id, _, _, _ = ticket_info
    status_emoji = "🔴" if status == "closed" else "🟢"
    display_name = username if username else (first_name if first_name else f"User{user_chat_id}")
    topic_name = f"{status_emoji} {display_name}"
    
    try:
        await context.bot.edit_forum_topic(chat_id=db.SUPPORT_CHAT_ID, message_thread_id=topic_id, name=topic_name[:128])
    except Exception as e:
        logger.error(f"Ошибка обновления названия топика: {e}")

def get_ticket_keyboard(user_chat_id: int, ticket_id: int) -> InlineKeyboardMarkup:
    is_blocked = db.is_user_blocked(user_chat_id)
    block_text = "✅ Разблокировать" if is_blocked else "❌ Заблокировать"

    ticket_info = db.get_ticket_info(ticket_id)
    status = ticket_info[4] if ticket_info else "open"

    ticket_btn_text = "🔓 Открыть тикет" if status == "closed" else "🔒 Закрыть тикет"
    ticket_callback = f"reopen_ticket_{ticket_id}_{user_chat_id}" if status == "closed" else f"close_ticket_{ticket_id}_{user_chat_id}"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(ticket_btn_text, callback_data=ticket_callback),
            InlineKeyboardButton(block_text, callback_data=f"block_user_{user_chat_id}_{ticket_id}")
        ]
    ])

# ----------------- Хендлеры пользователя -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.is_user_blocked(update.effective_user.id): return
    await update.message.reply_text(db.get_setting("greeting", db.DEFAULT_GREETING))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.is_user_blocked(update.effective_user.id): return
    await update.message.reply_text(db.get_setting("help", db.DEFAULT_HELP))

async def forward_to_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = message.from_user
    user_chat_id = message.chat_id

    if db.is_user_blocked(user_chat_id): return

    work_hours_enabled = db.get_setting("work_hours_enabled", "0") == "1"
    work_time = db.get_setting("work_hours_time", db.DEFAULT_WORK_TIME)
    
    ticket_data = db.get_open_ticket(user_chat_id)
    new_ticket = False

    if ticket_data is None:
        ticket_id, topic_id = await create_ticket(context, user_chat_id, user.username, user.first_name)
        new_ticket = True
        
        reply_text = f"✅ Ваш тикет #{ticket_id} создан."
        if work_hours_enabled and not db.is_working_hours(work_time):
            reply_text += f"\n\n{db.get_setting('work_hours_text', db.DEFAULT_WORK_TEXT)}"
        else:
            reply_text += " Оператор поддержки скоро ответит."
            
        await message.reply_text(reply_text)
    else:
        ticket_id, topic_id = ticket_data

    username = f"@{user.username}" if user.username else "Не указан"
    topic_mode = db.get_topic_mode()
    
    if topic_mode == "single_topic" and new_ticket:
        header = f"🎫 НОВЫЙ ТИКЕТ\n\n🆔 Тикет: {ticket_id}\n👤 Пользователь: {user.first_name or 'Не указано'}\n🆔 Telegram ID: {user.id}\n📱 Username: {username}"
    else:
        header = f"💬 {user.first_name or 'Не указано'} ({username}):"

    send_kwargs = {"chat_id": db.SUPPORT_CHAT_ID, "reply_markup": get_ticket_keyboard(user_chat_id, ticket_id)}
    
    if topic_mode == "per_user" and topic_id:
        send_kwargs["message_thread_id"] = topic_id
    elif topic_mode == "single_topic" and db.SUPPORT_TOPIC_ID:
        send_kwargs["message_thread_id"] = db.SUPPORT_TOPIC_ID

    sent_message = None
    try:
        if message.photo:
            cap = message.caption or ""
            sent_message = await context.bot.send_photo(photo=message.photo[-1].file_id, caption=f"{header}\n\n{cap}" if cap else header, **send_kwargs)
        elif message.video:
            cap = message.caption or ""
            sent_message = await context.bot.send_video(video=message.video.file_id, caption=f"{header}\n\n{cap}" if cap else header, **send_kwargs)
        elif message.document:
            cap = message.caption or ""
            sent_message = await context.bot.send_document(document=message.document.file_id, caption=f"{header}\n\n{cap}" if cap else header, **send_kwargs)
        elif message.voice:
            sent_message = await context.bot.send_voice(voice=message.voice.file_id, caption=header, **send_kwargs)
        elif message.audio:
            cap = message.caption or ""
            sent_message = await context.bot.send_audio(audio=message.audio.file_id, caption=f"{header}\n\n{cap}" if cap else header, **send_kwargs)
        elif message.text:
            sent_message = await context.bot.send_message(text=f"{header}\n\n{message.text}", **send_kwargs)

        if sent_message:
            db.save_mapping(user_chat_id, message.message_id, sent_message.message_id, ticket_id)
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения: {e}")

# ----------------- Хендлеры поддержки -----------------
async def reply_from_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != db.SUPPORT_CHAT_ID or not message.reply_to_message: return

    found = db.find_user_by_support_message(message.reply_to_message.message_id)
    if not found: return

    user_chat_id, user_message_id, ticket_id = found
    if db.is_user_blocked(user_chat_id):
        await message.reply_text("⛔️ Этот пользователь заблокирован. Он не получит сообщение.")
        return

    try:
        if message.photo: await context.bot.send_photo(chat_id=user_chat_id, photo=message.photo[-1].file_id, caption=message.caption or "")
        elif message.video: await context.bot.send_video(chat_id=user_chat_id, video=message.video.file_id, caption=message.caption or "")
        elif message.document: await context.bot.send_document(chat_id=user_chat_id, document=message.document.file_id, caption=message.caption or "")
        elif message.voice: await context.bot.send_voice(chat_id=user_chat_id, voice=message.voice.file_id, caption=message.caption or "")
        elif message.audio: await context.bot.send_audio(chat_id=user_chat_id, audio=message.audio.file_id, caption=message.caption or "")
        elif message.text: await context.bot.send_message(chat_id=user_chat_id, text=message.text)
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю: {e}")

# ----------------- Обработка кнопок под тикетами -----------------
async def support_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    admin_id = query.from_user.id
    
    if data.startswith("block_user_"):
        parts = data.split("_")
        target_user_id, ticket_id = int(parts[2]), int(parts[3])
        is_blocked_now = db.toggle_user_block(target_user_id, admin_id)
        
        await query.answer("❗️ Пользователь заблокирован" if is_blocked_now else "✅ Пользователь разблокирован", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=get_ticket_keyboard(target_user_id, ticket_id))
        except Exception: pass

    elif data.startswith("close_ticket_"):
        parts = data.split("_")
        ticket_id, user_chat_id = int(parts[2]), int(parts[3])
        ticket_info = db.get_ticket_info(ticket_id)
        
        if ticket_info and ticket_info[4] != "closed":
            db.update_ticket_status(ticket_id, "closed")
            await update_topic_status(context, ticket_id, "closed")
            try: await context.bot.send_message(chat_id=user_chat_id, text="✅ Обращение завершено")
            except Exception: pass
            await query.answer("🔒 Тикет закрыт")
        else:
            await query.answer("Этот тикет уже закрыт")

        try: await query.edit_message_reply_markup(reply_markup=get_ticket_keyboard(user_chat_id, ticket_id))
        except Exception: pass

    elif data.startswith("reopen_ticket_"):
        parts = data.split("_")
        ticket_id, user_chat_id = int(parts[2]), int(parts[3])
        ticket_info = db.get_ticket_info(ticket_id)
        
        if ticket_info and ticket_info[4] != "open":
            db.update_ticket_status(ticket_id, "open")
            await update_topic_status(context, ticket_id, "open")
            await query.answer("🔓 Тикет открыт")
        else:
            await query.answer("Этот тикет уже открыт")
            
        try: await query.edit_message_reply_markup(reply_markup=get_ticket_keyboard(user_chat_id, ticket_id))
        except Exception: pass


async def open_tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != db.SUPPORT_CHAT_ID: return
    rows = db.get_all_open_tickets()
    if not rows:
        await update.message.reply_text("Открытых тикетов нет ✅")
        return
    lines = ["📂 Открытые тикеты:\n"]
    for ticket_id, user_chat_id, username, first_name, created_at, updated_at in rows:
        lines.append(f"🎫 Тикет #{ticket_id}\n👤 {first_name or 'Не указано'}\n📱 {'@' + username if username else 'Не указан'}\n🆔 ID: {user_chat_id}\n📅 Создан: {db.format_datetime(created_at)}\n")
    await update.message.reply_text("\n".join(lines))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    application = Application.builder().token(db.TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("open_tickets", open_tickets_cmd))

    # Подключаем меню администратора из admin.py
    application.add_handler(get_admin_conv_handler())
    
    # Обработчик для кнопок блокировки и управления тикетами
    application.add_handler(CallbackQueryHandler(support_action_callback, pattern="^(block_user_|close_ticket_|reopen_ticket_)"))
    
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.ALL ^ filters.COMMAND), forward_to_support))
    application.add_handler(MessageHandler(filters.REPLY, reply_from_support))
    application.add_error_handler(error_handler)

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()