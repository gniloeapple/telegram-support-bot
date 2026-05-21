import logging
from datetime import datetime, timezone

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
from database import TicketStatus, TopicMode
from admin import admin_command, get_admin_conv_handler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# ── Rate limiting ─────────────────────────────────────────────────────────────
RATE_LIMIT_MESSAGES = 5   # максимум сообщений
RATE_LIMIT_WINDOW   = 60  # за N секунд

def _is_rate_limited(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """True если пользователь превысил лимит сообщений."""
    now = datetime.now(timezone.utc).timestamp()
    key = f"rl_{user_id}"
    history: list = context.bot_data.get(key, [])
    # Оставляем только события в пределах окна
    history = [t for t in history if now - t < RATE_LIMIT_WINDOW]
    if len(history) >= RATE_LIMIT_MESSAGES:
        context.bot_data[key] = history
        return True
    history.append(now)
    context.bot_data[key] = history
    return False


# ── Создание тикета ──────────────────────────────────────────────────────────

async def create_ticket(
    context: ContextTypes.DEFAULT_TYPE,
    user_chat_id: int,
    username: str = None,
    first_name: str = None,
) -> tuple:
    topic_mode = await db.get_topic_mode()
    topic_id   = None

    if topic_mode == TopicMode.PER_USER:
        display_name = username or first_name or f"User{user_chat_id}"
        topic_name   = f"🟢 {display_name}"
        try:
            forum_topic = await context.bot.create_forum_topic(
                chat_id=db.SUPPORT_CHAT_ID, name=topic_name[:128]
            )
            topic_id = forum_topic.message_thread_id
        except Exception as e:
            logger.error(f"Ошибка создания топика: {e}")

    ticket_id = await db.create_ticket_in_db(user_chat_id, username, first_name, topic_id)

    if topic_mode == TopicMode.PER_USER and topic_id:
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
                chat_id=db.SUPPORT_CHAT_ID,
                message_thread_id=topic_id,
                text=user_info,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Ошибка отправки информации: {e}")

    return ticket_id, topic_id


async def update_topic_status(
    context: ContextTypes.DEFAULT_TYPE, ticket_id: int, status: str
):
    if await db.get_topic_mode() != TopicMode.PER_USER:
        return
    ticket_info = await db.get_ticket_info(ticket_id)
    if not ticket_info or not ticket_info[0]:
        return
    topic_id, username, first_name, user_chat_id, *_ = ticket_info
    emoji       = "🔴" if status == TicketStatus.CLOSED else "🟢"
    display_name = username or first_name or f"User{user_chat_id}"
    try:
        await context.bot.edit_forum_topic(
            chat_id=db.SUPPORT_CHAT_ID,
            message_thread_id=topic_id,
            name=f"{emoji} {display_name}"[:128],
        )
    except Exception as e:
        logger.error(f"Ошибка обновления названия топика: {e}")


# ── Клавиатуры ───────────────────────────────────────────────────────────────

async def get_ticket_keyboard(user_chat_id: int, ticket_id: int) -> InlineKeyboardMarkup:
    is_blocked  = await db.is_user_blocked(user_chat_id)
    block_text  = "✅ Разблокировать" if is_blocked else "❌ Заблокировать"
    ticket_info = await db.get_ticket_info(ticket_id)
    status      = ticket_info[4] if ticket_info else TicketStatus.OPEN

    if status == TicketStatus.CLOSED:
        ticket_btn  = "🔓 Открыть тикет"
        ticket_cb   = f"reopen_ticket_{ticket_id}_{user_chat_id}"
    else:
        ticket_btn  = "🔒 Закрыть тикет"
        ticket_cb   = f"close_ticket_{ticket_id}_{user_chat_id}"

    rows = [
        [
            InlineKeyboardButton(ticket_btn, callback_data=ticket_cb),
            InlineKeyboardButton(block_text, callback_data=f"block_user_{user_chat_id}_{ticket_id}"),
        ],
        [InlineKeyboardButton("💬 Шаблоны ответов", callback_data=f"templates_{ticket_id}_{user_chat_id}")],
    ]
    return InlineKeyboardMarkup(rows)

def rating_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(str(i), callback_data=f"rate_{ticket_id}_{i}") for i in range(1, 6)]
    return InlineKeyboardMarkup([buttons])


# ── Хендлеры пользователя ────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await db.is_user_blocked(update.effective_user.id):
        return
    await update.message.reply_text(await db.get_setting("greeting", db.DEFAULT_GREETING))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await db.is_user_blocked(update.effective_user.id):
        return
    await update.message.reply_text(await db.get_setting("help", db.DEFAULT_HELP))

async def forward_to_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message      = update.message
    user         = message.from_user
    user_chat_id = message.chat_id

    if await db.is_user_blocked(user_chat_id):
        return

    # Rate limiting
    if _is_rate_limited(context, user_chat_id):
        await message.reply_text(
            f"⏳ Вы отправляете сообщения слишком часто. "
            f"Пожалуйста, подождите немного."
        )
        return

    work_hours_enabled = await db.get_setting("work_hours_enabled", "0") == "1"
    work_time          = await db.get_setting("work_hours_time", db.DEFAULT_WORK_TIME)
    ticket_data        = await db.get_open_ticket(user_chat_id)
    new_ticket         = False

    if ticket_data is None:
        ticket_id, topic_id = await create_ticket(
            context, user_chat_id, user.username, user.first_name
        )
        new_ticket   = True
        reply_text   = f"✅ Ваш тикет #{ticket_id} создан."
        if work_hours_enabled and not db.is_working_hours(work_time):
            reply_text += f"\n\n{await db.get_setting('work_hours_text', db.DEFAULT_WORK_TEXT)}"
        else:
            reply_text += " Оператор поддержки скоро ответит."
        await message.reply_text(reply_text)
    else:
        ticket_id, topic_id = ticket_data

    username   = f"@{user.username}" if user.username else "Не указан"
    topic_mode = await db.get_topic_mode()

    if topic_mode == TopicMode.SINGLE_TOPIC and new_ticket:
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
        "chat_id":      db.SUPPORT_CHAT_ID,
        "reply_markup": await get_ticket_keyboard(user_chat_id, ticket_id),
    }
    if topic_mode == TopicMode.PER_USER and topic_id:
        send_kwargs["message_thread_id"] = topic_id
    elif topic_mode == TopicMode.SINGLE_TOPIC and db.SUPPORT_TOPIC_ID:
        send_kwargs["message_thread_id"] = db.SUPPORT_TOPIC_ID

    sent_message = None
    try:
        msg = message
        if msg.photo:
            cap = msg.caption or ""
            sent_message = await context.bot.send_photo(
                photo=msg.photo[-1].file_id,
                caption=f"{header}\n\n{cap}" if cap else header,
                **send_kwargs,
            )
        elif msg.video:
            cap = msg.caption or ""
            sent_message = await context.bot.send_video(
                video=msg.video.file_id,
                caption=f"{header}\n\n{cap}" if cap else header,
                **send_kwargs,
            )
        elif msg.document:
            cap = msg.caption or ""
            sent_message = await context.bot.send_document(
                document=msg.document.file_id,
                caption=f"{header}\n\n{cap}" if cap else header,
                **send_kwargs,
            )
        elif msg.voice:
            sent_message = await context.bot.send_voice(
                voice=msg.voice.file_id, caption=header, **send_kwargs
            )
        elif msg.audio:
            cap = msg.caption or ""
            sent_message = await context.bot.send_audio(
                audio=msg.audio.file_id,
                caption=f"{header}\n\n{cap}" if cap else header,
                **send_kwargs,
            )
        elif msg.video_note:
            # video_note не поддерживает caption — отправляем отдельным сообщением
            await context.bot.send_message(text=header, **send_kwargs)
            sent_message = await context.bot.send_video_note(
                video_note=msg.video_note.file_id, **{k: v for k, v in send_kwargs.items() if k != "reply_markup"}
            )
        elif msg.sticker:
            await context.bot.send_message(text=header, **send_kwargs)
            sent_message = await context.bot.send_sticker(
                sticker=msg.sticker.file_id, **{k: v for k, v in send_kwargs.items() if k != "reply_markup"}
            )
        elif msg.location:
            await context.bot.send_message(text=header, **send_kwargs)
            sent_message = await context.bot.send_location(
                latitude=msg.location.latitude,
                longitude=msg.location.longitude,
                **{k: v for k, v in send_kwargs.items() if k != "reply_markup"},
            )
        elif msg.contact:
            await context.bot.send_message(text=header, **send_kwargs)
            sent_message = await context.bot.send_contact(
                phone_number=msg.contact.phone_number,
                first_name=msg.contact.first_name,
                last_name=msg.contact.last_name or "",
                **{k: v for k, v in send_kwargs.items() if k != "reply_markup"},
            )
        elif msg.text:
            sent_message = await context.bot.send_message(
                text=f"{header}\n\n{msg.text}", **send_kwargs
            )

        if sent_message:
            await db.save_mapping(
                user_chat_id, message.message_id, sent_message.message_id, ticket_id
            )
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения: {e}")


# ── Хендлеры поддержки ───────────────────────────────────────────────────────

async def reply_from_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != db.SUPPORT_CHAT_ID or not message.reply_to_message:
        return

    found = await db.find_user_by_support_message(message.reply_to_message.message_id)
    if not found:
        return

    user_chat_id, user_message_id, ticket_id = found

    if await db.is_user_blocked(user_chat_id):
        await message.reply_text("⛔️ Этот пользователь заблокирован. Он не получит сообщение.")
        return

    try:
        if message.photo:
            await context.bot.send_photo(
                chat_id=user_chat_id, photo=message.photo[-1].file_id, caption=message.caption or ""
            )
        elif message.video:
            await context.bot.send_video(
                chat_id=user_chat_id, video=message.video.file_id, caption=message.caption or ""
            )
        elif message.document:
            await context.bot.send_document(
                chat_id=user_chat_id, document=message.document.file_id, caption=message.caption or ""
            )
        elif message.voice:
            await context.bot.send_voice(
                chat_id=user_chat_id, voice=message.voice.file_id
            )
        elif message.audio:
            await context.bot.send_audio(
                chat_id=user_chat_id, audio=message.audio.file_id, caption=message.caption or ""
            )
        elif message.sticker:
            await context.bot.send_sticker(chat_id=user_chat_id, sticker=message.sticker.file_id)
        elif message.video_note:
            await context.bot.send_video_note(chat_id=user_chat_id, video_note=message.video_note.file_id)
        elif message.text:
            await context.bot.send_message(chat_id=user_chat_id, text=message.text)
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю: {e}")


# ── Кнопки под тикетами ──────────────────────────────────────────────────────

async def support_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    data     = query.data
    admin_id = query.from_user.id

    # ── Шаблоны ──────────────────────────────────────────────────────────────
    if data.startswith("templates_"):
        _, ticket_id_s, user_chat_id_s = data.split("_")
        ticket_id, user_chat_id = int(ticket_id_s), int(user_chat_id_s)
        templates = await db.get_templates()
        if not templates:
            await query.answer("Шаблонов пока нет. Добавьте через /admin → Шаблоны.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(t[1], callback_data=f"usetpl_{t[0]}_{ticket_id}_{user_chat_id}")]
            for t in templates
        ]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"canceltpl_{ticket_id}_{user_chat_id}")])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("usetpl_"):
        parts = data.split("_")
        tpl_id, ticket_id, user_chat_id = int(parts[1]), int(parts[2]), int(parts[3])
        templates = await db.get_templates()
        tpl = next((t for t in templates if t[0] == tpl_id), None)
        if tpl:
            try:
                await context.bot.send_message(chat_id=user_chat_id, text=tpl[2])
                await query.answer("✅ Шаблон отправлен")
            except Exception as e:
                logger.error(f"Ошибка отправки шаблона: {e}")
                await query.answer("❌ Ошибка отправки", show_alert=True)
        try:
            await query.edit_message_reply_markup(
                reply_markup=await get_ticket_keyboard(user_chat_id, ticket_id)
            )
        except Exception:
            pass
        return

    if data.startswith("canceltpl_"):
        _, ticket_id_s, user_chat_id_s = data.split("_")
        ticket_id, user_chat_id = int(ticket_id_s), int(user_chat_id_s)
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=await get_ticket_keyboard(user_chat_id, ticket_id)
            )
        except Exception:
            pass
        return

    # ── Блокировка ────────────────────────────────────────────────────────────
    if data.startswith("block_user_"):
        parts = data.split("_")
        target_user_id, ticket_id = int(parts[2]), int(parts[3])
        is_blocked_now = await db.toggle_user_block(target_user_id, admin_id)
        await query.answer(
            "❗️ Пользователь заблокирован" if is_blocked_now else "✅ Пользователь разблокирован",
            show_alert=False,
        )
        try:
            await query.edit_message_reply_markup(
                reply_markup=await get_ticket_keyboard(target_user_id, ticket_id)
            )
        except Exception:
            pass

    # ── Закрытие тикета ───────────────────────────────────────────────────────
    elif data.startswith("close_ticket_"):
        parts = data.split("_")
        ticket_id, user_chat_id = int(parts[2]), int(parts[3])
        ticket_info = await db.get_ticket_info(ticket_id)

        if ticket_info and ticket_info[4] != TicketStatus.CLOSED:
            await db.update_ticket_status(ticket_id, TicketStatus.CLOSED)
            await update_topic_status(context, ticket_id, TicketStatus.CLOSED)
            try:
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text="✅ Обращение завершено. Пожалуйста, оцените качество поддержки:",
                    reply_markup=rating_keyboard(ticket_id),
                )
            except Exception:
                pass
            await query.answer("🔒 Тикет закрыт")
        else:
            await query.answer("Этот тикет уже закрыт")

        try:
            await query.edit_message_reply_markup(
                reply_markup=await get_ticket_keyboard(user_chat_id, ticket_id)
            )
        except Exception:
            pass

    # ── Открытие тикета ───────────────────────────────────────────────────────
    elif data.startswith("reopen_ticket_"):
        parts = data.split("_")
        ticket_id, user_chat_id = int(parts[2]), int(parts[3])
        ticket_info = await db.get_ticket_info(ticket_id)

        if ticket_info and ticket_info[4] != TicketStatus.OPEN:
            await db.update_ticket_status(ticket_id, TicketStatus.OPEN)
            await update_topic_status(context, ticket_id, TicketStatus.OPEN)
            await query.answer("🔓 Тикет открыт")
        else:
            await query.answer("Этот тикет уже открыт")

        try:
            await query.edit_message_reply_markup(
                reply_markup=await get_ticket_keyboard(user_chat_id, ticket_id)
            )
        except Exception:
            pass


# ── Оценка тикета ────────────────────────────────────────────────────────────

async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    _, ticket_id_s, rating_s = query.data.split("_")
    ticket_id    = int(ticket_id_s)
    rating       = int(rating_s)
    user_chat_id = query.from_user.id

    existing = await db.get_rating(ticket_id)
    if existing:
        await query.answer("Вы уже оценили это обращение.", show_alert=True)
        return

    await db.save_rating(ticket_id, user_chat_id, rating)
    stars_str = "⭐" * rating + "☆" * (5 - rating)
    await query.answer(f"Спасибо за оценку! {stars_str}", show_alert=False)
    try:
        await query.edit_message_text(
            text=f"✅ Обращение завершено.\n\nВаша оценка: {stars_str} ({rating}/5)\nСпасибо!"
        )
    except Exception:
        pass

    # Уведомить поддержку
    try:
        ticket_info = await db.get_ticket_info(ticket_id)
        if ticket_info:
            topic_id = ticket_info[0]
            msg_kwargs = {"chat_id": db.SUPPORT_CHAT_ID, "text": f"⭐ Тикет #{ticket_id} оценён: {stars_str} ({rating}/5)"}
            if topic_id and await db.get_topic_mode() == TopicMode.PER_USER:
                msg_kwargs["message_thread_id"] = topic_id
            elif db.SUPPORT_TOPIC_ID:
                msg_kwargs["message_thread_id"] = db.SUPPORT_TOPIC_ID
            await context.bot.send_message(**msg_kwargs)
    except Exception as e:
        logger.error(f"Ошибка уведомления об оценке: {e}")


# ── Команды чата поддержки ───────────────────────────────────────────────────

async def open_tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != db.SUPPORT_CHAT_ID:
        return
    rows = await db.get_all_open_tickets()
    if not rows:
        await update.message.reply_text("Открытых тикетов нет ✅")
        return
    lines = ["📂 Открытые тикеты:\n"]
    for ticket_id, user_chat_id, username, first_name, created_at, updated_at in rows:
        lines.append(
            f"🎫 Тикет #{ticket_id}\n"
            f"👤 {first_name or 'Не указано'}\n"
            f"📱 {'@' + username if username else 'Не указан'}\n"
            f"🆔 ID: {user_chat_id}\n"
            f"📅 Создан: {db.format_datetime(created_at)}\n"
        )
    await update.message.reply_text("\n".join(lines))

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != db.SUPPORT_CHAT_ID and not db.is_admin(update.effective_user.id):
        return
    s = await db.get_stats()
    rating_str = (
        f"{s['avg_rating']} ⭐ ({s['ratings_count']} оценок)"
        if s["ratings_count"] > 0
        else "нет оценок"
    )
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"🎫 Всего тикетов: {s['total']}\n"
        f"🟢 Открытых: {s['open']}\n"
        f"📅 Сегодня: {s['today']}\n"
        f"📆 За 7 дней: {s['week']}\n"
        f"⛔️ Заблокировано: {s['blocked']}\n"
        f"⭐ Средний рейтинг: {rating_str}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


# ── main ─────────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    await db.init_db()

def main():
    application = (
        Application.builder()
        .token(db.TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start",        start))
    application.add_handler(CommandHandler("help",         help_command))
    application.add_handler(CommandHandler("admin",        admin_command))
    application.add_handler(CommandHandler("open_tickets", open_tickets_cmd))
    application.add_handler(CommandHandler("stats",        stats_cmd))

    application.add_handler(get_admin_conv_handler())

    application.add_handler(
        CallbackQueryHandler(rating_callback, pattern=r"^rate_\d+_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            support_action_callback,
            pattern=r"^(block_user_|close_ticket_|reopen_ticket_|templates_|usetpl_|canceltpl_)",
        )
    )

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
