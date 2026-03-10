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

logger = logging.getLogger(__name__)

WAITING_GREETING, WAITING_HELP, WAITING_WORK_TEXT, WAITING_WORK_TIME = range(4)

def get_admin_main_keyboard():
    topic_mode = db.get_topic_mode()
    mode_text = "📁 Отдельный топик для каждого" if topic_mode == "per_user" else "📂 Общий топик"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="admin_edit_greeting")],
        [InlineKeyboardButton("📝 Изменить информацию", callback_data="admin_edit_help")],
        [InlineKeyboardButton("🕒 Режим работы", callback_data="admin_work_menu")],
        [InlineKeyboardButton(f"🔄 Режим: {mode_text}", callback_data="admin_toggle_mode")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="admin_close_menu")],
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        return
    
    msg = await update.message.reply_text(
        "⚙️ Управление ботом",
        reply_markup=get_admin_main_keyboard()
    )
    context.user_data['admin_menu_message_id'] = msg.message_id
    context.user_data['admin_menu_chat_id'] = msg.chat_id

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
        if not db.is_admin(user_id):
            return ConversationHandler.END

    menu_msg_id = context.user_data.get('admin_menu_message_id')
    menu_chat_id = context.user_data.get('admin_menu_chat_id')
    
    if menu_msg_id and menu_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="⚙️ Управление ботом",
                reply_markup=get_admin_main_keyboard()
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Ошибка редактирования меню: {e}")
    
    back_button_msg_id = context.user_data.get('back_button_message_id')
    if back_button_msg_id and menu_chat_id:
        try:
            await context.bot.delete_message(chat_id=menu_chat_id, message_id=back_button_msg_id)
        except Exception:
            pass
    return ConversationHandler.END

async def show_work_hours_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    enabled = db.get_setting("work_hours_enabled", "0") == "1"
    work_time = db.get_setting("work_hours_time", db.DEFAULT_WORK_TIME)
    work_text = db.get_setting("work_hours_text", db.DEFAULT_WORK_TEXT)

    text = (
        "🔶 <b>Режим работы</b>\n\n"
        "Вы можете установить режим работы, когда вы можете отвечать на сообщения. "
        "В иное время бот будет предупреждать, что ответ из-за не рабочего времени будет чуть позже\n\n"
        f"Текущее время режима работы: {work_time} MSK (+3)\n"
        f"Текст:\n\n{work_text}"
    )

    toggle_btn_text = "✅ Выключить автоответ" if enabled else "❌ Включить автоответ"

    keyboard = [
        [InlineKeyboardButton(toggle_btn_text, callback_data="admin_toggle_work")],
        [InlineKeyboardButton("Добавить текст автоответа", callback_data="admin_edit_work_text")],
        [InlineKeyboardButton("Добавить время режима работы", callback_data="admin_edit_work_time")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back_to_menu")]
    ]

    menu_msg_id = context.user_data.get('admin_menu_message_id')
    menu_chat_id = context.user_data.get('admin_menu_chat_id')

    if menu_msg_id and menu_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Ошибка редактирования меню работы: {e}")

    back_button_msg_id = context.user_data.get('back_button_message_id')
    if back_button_msg_id and menu_chat_id:
        try:
            await context.bot.delete_message(chat_id=menu_chat_id, message_id=back_button_msg_id)
        except Exception:
            pass

    return ConversationHandler.END

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not db.is_admin(user_id):
        return
    
    context.user_data['admin_menu_message_id'] = query.message.message_id
    context.user_data['admin_menu_chat_id'] = query.message.chat_id
    
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data="admin_back_to_menu")]])
    cancel_work_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_work_menu")]])
    
    if query.data == "admin_edit_greeting":
        current_text = db.get_setting("greeting", db.DEFAULT_GREETING)
        msg = await query.message.reply_text(
            f"👉 Введите новое сообщение приветствия:\n\n<b>Текущее приветствие:</b>\n\n{current_text}",
            parse_mode="HTML", reply_markup=back_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_GREETING
    
    elif query.data == "admin_edit_help":
        current_text = db.get_setting("help", db.DEFAULT_HELP)
        msg = await query.message.reply_text(
            f"👉 Введите новое сообщение помощи:\n\n<b>Текущая информация:</b>\n\n{current_text}",
            parse_mode="HTML", reply_markup=back_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_HELP
    
    elif query.data == "admin_toggle_mode":
        current_mode = db.get_topic_mode()
        new_mode = "single_topic" if current_mode == "per_user" else "per_user"
        db.set_topic_mode(new_mode)
        try:
            await query.edit_message_reply_markup(reply_markup=get_admin_main_keyboard())
        except Exception:
            pass
            
    elif query.data == "admin_work_menu":
        return await show_work_hours_menu(update, context)
        
    elif query.data == "admin_toggle_work":
        current = db.get_setting("work_hours_enabled", "0")
        db.set_setting("work_hours_enabled", "0" if current == "1" else "1")
        return await show_work_hours_menu(update, context)
        
    elif query.data == "admin_edit_work_text":
        msg = await query.message.reply_text(
            "Давайте добавим текст режима работы", 
            reply_markup=cancel_work_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_WORK_TEXT
        
    elif query.data == "admin_edit_work_time":
        msg = await query.message.reply_text(
            "Давайте добавим время режима работы в формате 10:00-18:00", 
            reply_markup=cancel_work_markup
        )
        context.user_data['back_button_message_id'] = msg.message_id
        return WAITING_WORK_TIME
        
    elif query.data == "admin_back_to_menu":
        return await show_admin_menu(update, context)
        
    elif query.data == "admin_close_menu":
        try:
            await query.message.delete()
        except Exception as e:
            logger.error(f"Ошибка удаления меню: {e}")
        return ConversationHandler.END

async def save_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return ConversationHandler.END
    db.set_setting("greeting", update.message.text)
    await update.message.reply_text("✅ Приветственное сообщение успешно обновлено!")
    return await show_admin_menu(update, context)

async def save_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return ConversationHandler.END
    db.set_setting("help", update.message.text)
    await update.message.reply_text("✅ Сообщение помощи успешно обновлено!")
    return await show_admin_menu(update, context)

async def save_work_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return ConversationHandler.END
    db.set_setting("work_hours_text", update.message.text)
    await update.message.reply_text("✅ Текст режима работы сохранен!")
    return await show_work_hours_menu(update, context)

async def save_work_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return ConversationHandler.END
    db.set_setting("work_hours_time", update.message.text)
    await update.message.reply_text("✅ Время режима работы сохранено!")
    return await show_work_hours_menu(update, context)

async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

# Экспорт ConversationHandler для подключения в основном файле
def get_admin_conv_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback_handler, pattern="^admin_")
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
            WAITING_WORK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_work_text),
                CallbackQueryHandler(show_work_hours_menu, pattern="^admin_work_menu$")
            ],
            WAITING_WORK_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_work_time),
                CallbackQueryHandler(show_work_hours_menu, pattern="^admin_work_menu$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_admin),
            CallbackQueryHandler(show_admin_menu, pattern="^admin_back_to_menu$"),
            CallbackQueryHandler(show_work_hours_menu, pattern="^admin_work_menu$")
        ],
    )