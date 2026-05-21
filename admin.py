import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)

import database as db
from database import TopicMode

logger = logging.getLogger(__name__)

(
    WAITING_GREETING,
    WAITING_HELP,
    WAITING_WORK_TEXT,
    WAITING_WORK_TIME,
    WAITING_TPL_TITLE,
    WAITING_TPL_CONTENT,
) = range(6)


# ── Главное меню ─────────────────────────────────────────────────────────────

async def get_admin_main_keyboard():
    topic_mode = await db.get_topic_mode()
    mode_text  = "📁 Отдельный топик для каждого" if topic_mode == TopicMode.PER_USER else "📂 Общий топик"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить приветствие",  callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию",   callback_data="admin_edit_help")],
        [InlineKeyboardButton("🕒 Режим работы",          callback_data="admin_work_menu")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}",   callback_data="admin_toggle_mode")],
        [InlineKeyboardButton("💬 Шаблоны ответов",       callback_data="admin_templates_menu")],
        [InlineKeyboardButton("❌ Закрыть",               callback_data="admin_close_menu")],
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return
    msg = await update.message.reply_text(
        "⚙️ Управление ботом",
        reply_markup=await get_admin_main_keyboard(),
    )
    context.user_data["admin_menu_message_id"] = msg.message_id
    context.user_data["admin_menu_chat_id"]    = msg.chat_id

async def _edit_menu(context, chat_id, message_id, text, markup, parse_mode=None):
    try:
        kwargs = dict(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        await context.bot.edit_message_text(**kwargs)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования меню: {e}")

async def _delete_back_button(context, chat_id, back_msg_id):
    if back_msg_id and chat_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=back_msg_id)
        except Exception:
            pass

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if not db.is_admin(query.from_user.id):
            return ConversationHandler.END

    menu_msg_id = context.user_data.get("admin_menu_message_id")
    menu_chat_id = context.user_data.get("admin_menu_chat_id")

    if menu_msg_id and menu_chat_id:
        await _edit_menu(context, menu_chat_id, menu_msg_id, "⚙️ Управление ботом", await get_admin_main_keyboard())

    await _delete_back_button(context, menu_chat_id, context.user_data.get("back_button_message_id"))
    return ConversationHandler.END


# ── Меню режима работы ───────────────────────────────────────────────────────

async def show_work_hours_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    enabled   = await db.get_setting("work_hours_enabled", "0") == "1"
    work_time = await db.get_setting("work_hours_time", db.DEFAULT_WORK_TIME)
    work_text = await db.get_setting("work_hours_text", db.DEFAULT_WORK_TEXT)

    text = (
        "🔶 <b>Режим работы</b>\n\n"
        "Вы можете установить рабочие часы. В нерабочее время бот предупредит пользователя.\n\n"
        f"Текущее время: {work_time} MSK (+3)\n"
        f"Текст:\n\n{work_text}"
    )
    toggle_text = "✅ Выключить автоответ" if enabled else "❌ Включить автоответ"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text,                      callback_data="admin_toggle_work")],
        [InlineKeyboardButton("Добавить текст автоответа",      callback_data="admin_edit_work_text")],
        [InlineKeyboardButton("Добавить время режима работы",   callback_data="admin_edit_work_time")],
        [InlineKeyboardButton("🔙 Назад",                       callback_data="admin_back_to_menu")],
    ])

    menu_msg_id  = context.user_data.get("admin_menu_message_id")
    menu_chat_id = context.user_data.get("admin_menu_chat_id")
    if menu_msg_id and menu_chat_id:
        await _edit_menu(context, menu_chat_id, menu_msg_id, text, keyboard, parse_mode="HTML")

    await _delete_back_button(context, menu_chat_id, context.user_data.get("back_button_message_id"))
    return ConversationHandler.END


# ── Меню шаблонов ────────────────────────────────────────────────────────────

async def show_templates_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    templates = await db.get_templates()
    menu_msg_id  = context.user_data.get("admin_menu_message_id")
    menu_chat_id = context.user_data.get("admin_menu_chat_id")

    buttons = []
    for tpl in templates:
        buttons.append([
            InlineKeyboardButton(f"🗑 {tpl[1]}", callback_data=f"admin_del_tpl_{tpl[0]}")
        ])
    buttons.append([InlineKeyboardButton("➕ Добавить шаблон", callback_data="admin_add_tpl")])
    buttons.append([InlineKeyboardButton("🔙 Назад",           callback_data="admin_back_to_menu")])

    text = (
        "💬 <b>Шаблоны быстрых ответов</b>\n\n"
        + (
            "\n".join(f"• <b>{t[1]}</b>: {t[2][:60]}{'…' if len(t[2]) > 60 else ''}" for t in templates)
            if templates else "Шаблонов пока нет."
        )
        + "\n\n<i>Нажмите на шаблон чтобы удалить, или добавьте новый.</i>"
    )

    if menu_msg_id and menu_chat_id:
        await _edit_menu(context, menu_chat_id, menu_msg_id, text, InlineKeyboardMarkup(buttons), parse_mode="HTML")

    await _delete_back_button(context, menu_chat_id, context.user_data.get("back_button_message_id"))
    return ConversationHandler.END


# ── Основной callback-хендлер ────────────────────────────────────────────────

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not db.is_admin(query.from_user.id):
        return

    context.user_data["admin_menu_message_id"] = query.message.message_id
    context.user_data["admin_menu_chat_id"]    = query.message.chat_id

    back_markup        = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню",  callback_data="admin_back_to_menu")]])
    cancel_work_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_work_menu")]])
    cancel_tpl_markup  = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_templates_menu")]])

    data = query.data

    if data == "admin_edit_greeting":
        current = await db.get_setting("greeting", db.DEFAULT_GREETING)
        msg = await query.message.reply_text(
            f"👉 Введите новое приветствие:\n\n<b>Текущее:</b>\n\n{current}",
            parse_mode="HTML", reply_markup=back_markup,
        )
        context.user_data["back_button_message_id"] = msg.message_id
        return WAITING_GREETING

    elif data == "admin_edit_help":
        current = await db.get_setting("help", db.DEFAULT_HELP)
        msg = await query.message.reply_text(
            f"👉 Введите новое сообщение помощи:\n\n<b>Текущее:</b>\n\n{current}",
            parse_mode="HTML", reply_markup=back_markup,
        )
        context.user_data["back_button_message_id"] = msg.message_id
        return WAITING_HELP

    elif data == "admin_toggle_mode":
        current  = await db.get_topic_mode()
        new_mode = TopicMode.SINGLE_TOPIC if current == TopicMode.PER_USER else TopicMode.PER_USER
        await db.set_topic_mode(new_mode)
        try:
            await query.edit_message_reply_markup(reply_markup=await get_admin_main_keyboard())
        except Exception:
            pass

    elif data == "admin_work_menu":
        return await show_work_hours_menu(update, context)

    elif data == "admin_toggle_work":
        current = await db.get_setting("work_hours_enabled", "0")
        await db.set_setting("work_hours_enabled", "0" if current == "1" else "1")
        return await show_work_hours_menu(update, context)

    elif data == "admin_edit_work_text":
        msg = await query.message.reply_text(
            "Введите текст автоответа:", reply_markup=cancel_work_markup
        )
        context.user_data["back_button_message_id"] = msg.message_id
        return WAITING_WORK_TEXT

    elif data == "admin_edit_work_time":
        msg = await query.message.reply_text(
            "Введите время работы в формате <b>HH:MM-HH:MM</b> (например 10:00-18:00):",
            parse_mode="HTML", reply_markup=cancel_work_markup,
        )
        context.user_data["back_button_message_id"] = msg.message_id
        return WAITING_WORK_TIME

    elif data == "admin_templates_menu":
        return await show_templates_menu(update, context)

    elif data == "admin_add_tpl":
        msg = await query.message.reply_text(
            "Введите <b>название</b> шаблона (короткое, например «Принято»):",
            parse_mode="HTML", reply_markup=cancel_tpl_markup,
        )
        context.user_data["back_button_message_id"] = msg.message_id
        return WAITING_TPL_TITLE

    elif data.startswith("admin_del_tpl_"):
        tpl_id = int(data.split("_")[-1])
        await db.delete_template(tpl_id)
        return await show_templates_menu(update, context)

    elif data == "admin_back_to_menu":
        return await show_admin_menu(update, context)

    elif data == "admin_close_menu":
        try:
            await query.message.delete()
        except Exception as e:
            logger.error(f"Ошибка удаления меню: {e}")
        return ConversationHandler.END


# ── Сохранение значений ──────────────────────────────────────────────────────

async def save_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    await db.set_setting("greeting", update.message.text)
    await update.message.reply_text("✅ Приветствие обновлено!")
    return await show_admin_menu(update, context)

async def save_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    await db.set_setting("help", update.message.text)
    await update.message.reply_text("✅ Текст помощи обновлён!")
    return await show_admin_menu(update, context)

async def save_work_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    await db.set_setting("work_hours_text", update.message.text)
    await update.message.reply_text("✅ Текст автоответа сохранён!")
    return await show_work_hours_menu(update, context)

async def save_work_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    value = update.message.text.strip()
    if not db.validate_time_range(value):
        await update.message.reply_text(
            "❌ Неверный формат. Введите время в формате <b>HH:MM-HH:MM</b> (например 09:00-21:00):",
            parse_mode="HTML",
        )
        return WAITING_WORK_TIME
    await db.set_setting("work_hours_time", value)
    await update.message.reply_text("✅ Время работы сохранено!")
    return await show_work_hours_menu(update, context)

async def save_tpl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["new_tpl_title"] = update.message.text.strip()
    chat_id = update.message.chat_id

    # Удаляем промпт «Введите название» и сообщение пользователя с названием
    back_msg_id = context.user_data.get("back_button_message_id")
    if back_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=back_msg_id)
        except Exception:
            pass
    try:
        await update.message.delete()
    except Exception:
        pass

    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_templates_menu")]])
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Теперь введите <b>текст</b> шаблона «{context.user_data['new_tpl_title']}»:",
        parse_mode="HTML", reply_markup=cancel_markup,
    )
    context.user_data["back_button_message_id"] = msg.message_id
    return WAITING_TPL_CONTENT

async def save_tpl_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        return ConversationHandler.END
    title   = context.user_data.pop("new_tpl_title", "Без названия")
    content = update.message.text.strip()
    chat_id = update.message.chat_id

    # Удаляем промпт «Введите текст» и сообщение пользователя с текстом
    back_msg_id = context.user_data.get("back_button_message_id")
    if back_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=back_msg_id)
        except Exception:
            pass
    try:
        await update.message.delete()
    except Exception:
        pass

    await db.add_template(title, content)
    await context.bot.send_message(chat_id=chat_id, text=f"✅ Шаблон «{title}» добавлен!")
    return await show_templates_menu(update, context)

async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END


# ── ConversationHandler ───────────────────────────────────────────────────────

def get_admin_conv_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback_handler, pattern="^admin_")],
        states={
            WAITING_GREETING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_greeting),
                CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$"),
            ],
            WAITING_HELP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_help),
                CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$"),
            ],
            WAITING_WORK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_work_text),
                CallbackQueryHandler(show_work_hours_menu, pattern="^admin_work_menu$"),
            ],
            WAITING_WORK_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_work_time),
                CallbackQueryHandler(show_work_hours_menu, pattern="^admin_work_menu$"),
            ],
            WAITING_TPL_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_tpl_title),
                CallbackQueryHandler(show_templates_menu, pattern="^admin_templates_menu$"),
            ],
            WAITING_TPL_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_tpl_content),
                CallbackQueryHandler(show_templates_menu, pattern="^admin_templates_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_admin),
            CallbackQueryHandler(show_admin_menu,       pattern="^admin_back_to_menu$"),
            CallbackQueryHandler(show_work_hours_menu,  pattern="^admin_work_menu$"),
            CallbackQueryHandler(show_templates_menu,   pattern="^admin_templates_menu$"),
        ],
    )
