#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot with:
- Private menu (Content + Family) with Back buttons
- Group moderation (/ban /unban /kick /mute /unmute /purge /warn /warnings /clearwarns)
- Welcome/goodbye messages
Secure version: uses environment variables for BOT_TOKEN & OWNER_ID
"""

import logging
import json
import os
import time
from typing import Optional, Dict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    ParseMode,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set in Render
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # set in Render

# Storage for warns
WARN_FILE = "warns.json"

# Example content - replace with your real links
SUBJECTS = {
    "Math": [
        ("Algebra (PDF)", "https://example.com/math_algebra.pdf"),
        ("Calculus (PDF)", "https://example.com/math_calculus.pdf"),
    ],
    "Science": [
        ("Physics (PDF)", "https://example.com/physics.pdf"),
        ("Chemistry (PDF)", "https://example.com/chemistry.pdf"),
    ],
    "English": [
        ("Grammar (PDF)", "https://example.com/english_grammar.pdf"),
    ],
}

FAMILY_CHANNELS = [
    ("Main Channel", "https://t.me/your_real_channel"),
    ("Study Channel", "https://t.me/your_other_channel"),
]

MAX_WARNS = 3  # auto-action threshold

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- Warn persistence ----------------
def load_warns() -> Dict[str, Dict[str, int]]:
    if not os.path.exists(WARN_FILE):
        return {}
    try:
        with open(WARN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_warns(data: Dict[str, Dict[str, int]]) -> None:
    tmp = WARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WARN_FILE)

WARNS = load_warns()

# ---------------- Helpers ----------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Content", callback_data="menu:content")],
        [InlineKeyboardButton("👨‍👩‍👧 Our Family", callback_data="menu:family")],
    ])

def content_menu_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"sub:{name}")] for name in SUBJECTS.keys()]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def subject_kb(subject: str) -> InlineKeyboardMarkup:
    rows = []
    for title, url in SUBJECTS.get(subject, []):
        rows.append([InlineKeyboardButton(title, url=url)])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:content")])
    return InlineKeyboardMarkup(rows)

def family_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, url=link)] for title, link in FAMILY_CHANNELS]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def mention_html(user) -> str:
    name = user.full_name or user.first_name or "user"
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

def is_group(update: Update) -> bool:
    ct = update.effective_chat
    return ct and ct.type in ("group", "supergroup")

def is_user_admin_in_chat(context: CallbackContext, chat_id: int, user_id: int) -> bool:
    try:
        member = context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ---------------- Private: /start and callbacks ----------------
def start(update: Update, context: CallbackContext) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    else:
        update.message.reply_text("Bot is active. Use me in private for content menus or add me as admin for moderation.")

def cb_handler(update: Update, context: CallbackContext) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    q.answer()
    if q.message.chat.type != "private":
        q.answer("Open this bot in private to use content menus.", show_alert=True)
        return

    if data == "menu:home":
        q.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    elif data == "menu:content":
        q.edit_message_text("📚 Choose subject:", reply_markup=content_menu_kb())
    elif data.startswith("sub:"):
        subj = data.split(":", 1)[1]
        if subj not in SUBJECTS:
            q.edit_message_text("Subject not found.", reply_markup=content_menu_kb())
            return
        q.edit_message_text(f"📘 {subj} — materials:", reply_markup=subject_kb(subj))
    elif data == "menu:family":
        q.edit_message_text("👨‍👩‍👧 Our Family channels:", reply_markup=family_kb())

# ---------------- Group: welcome & goodbye ----------------
def welcome_handler(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.new_chat_members:
        return
    for m in update.message.new_chat_members:
        try:
            update.message.reply_text(
                f"👋 Welcome {mention_html(m)}! Read the rules and stay respectful.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

def left_handler(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.left_chat_member:
        return
    m = update.message.left_chat_member
    try:
        update.message.reply_text(f"👋 Goodbye {m.first_name or 'friend'} — Jai Hind 🇮🇳")
    except Exception:
        pass

# ---------------- Moderation Commands ----------------
def require_group_and_admin(func):
    def wrapper(update: Update, context: CallbackContext):
        if not is_group(update):
            update.effective_message.reply_text("This command must be used in groups.")
            return
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if user_id == OWNER_ID:
            return func(update, context)
        if not is_user_admin_in_chat(context, chat_id, user_id):
            update.effective_message.reply_text("Admins only.")
            return
        return func(update, context)
    return wrapper

def _get_target_from_reply_or_arg(update: Update, context: CallbackContext) -> Optional[int]:
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    if context.args:
        arg = context.args[0]
        if arg.isdigit():
            return int(arg)
    return None

# (commands /ban, /unban, /kick, /mute, /unmute, /purge, /warn, /warnings, /clearwarns stay the same — no change needed)

# ---------------- Misc ----------------
def whoami(update: Update, context: CallbackContext) -> None:
    u = update.effective_user
    update.effective_message.reply_text(f"🪪 ID: {u.id}\nName: {u.full_name}")

def help_cmd(update: Update, context: CallbackContext) -> None:
    if is_group(update):
        update.effective_message.reply_text(
            "/ban /unban /kick /mute /unmute /purge /warn /warnings /clearwarns"
        )
    else:
        update.effective_message.reply_text("Use buttons in private or add me as group admin for moderation.")

def error_handler(update: object, context: CallbackContext) -> None:
    logger.error("Exception: %s", context.error)

# ---------------- Main ----------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Please configure it in Render environment variables.")
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Private menu
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Welcome / left
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_handler))
    dp.add_handler(MessageHandler(Filters.status_update.left_chat_member, left_handler))

    # Moderation
    dp.add_handler(CommandHandler("ban", cmd_ban))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("kick", cmd_kick))
    dp.add_handler(CommandHandler("mute", cmd_mute))
    dp.add_handler(CommandHandler("unmute", cmd_unmute))
    dp.add_handler(CommandHandler("purge", cmd_purge))
    dp.add_handler(CommandHandler("warn", cmd_warn))
    dp.add_handler(CommandHandler("warnings", cmd_warnings))
    dp.add_handler(CommandHandler("clearwarns", cmd_clearwarns))

    # Misc
    dp.add_handler(CommandHandler("whoami", whoami))
    dp.add_handler(CommandHandler("help", help_cmd))

    dp.add_error_handler(error_handler)

    logger.info("Bot starting…")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()