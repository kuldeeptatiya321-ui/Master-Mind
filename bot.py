#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rose-like core moderation bot + private menu
Compatible with python-telegram-bot==13.15 (Updater style)

Features:
- Private menu (Content + Family) with back buttons
- Group welcome/goodbye messages (toggleable, custom text)
- Moderation: /ban /unban /kick /mute /unmute /purge
- Warn system: /warn /warnings /clearwarns /setmaxwarns
- Rules: /setrules /rules
- Pins: /pin (reply) /unpin
- Notes: /save <name> <text>, /get <name>, /notes, /delnote <name>
- Locks: /lock <links|media|stickers>, /unlock <...> (auto-delete locked content)
- Anti-flood: configurable threshold & action (mute) — simple in-memory
- Utils: /whoami /help

SECURITY: reads BOT_TOKEN and OWNER_ID from environment variables.
"""

import os
import json
import time
import logging
from typing import Dict, Any, Optional
from collections import defaultdict, deque

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    ParseMode,
    MessageEntity,
)
from telegram.error import BadRequest
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# ============ ENV & CONSTANTS ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Storage files (created on first save)
WARN_FILE = "warns.json"
SETTINGS_FILE = "settings.json"
NOTES_FILE = "notes.json"
LOCKS_FILE = "locks.json"

# In-memory anti-flood buffers: chat_id -> user_id -> deque of timestamps
FLOOD_WINDOW_SEC = 10
DEFAULT_FLOOD_LIMIT = 8           # messages in window
DEFAULT_FLOOD_MUTE_MIN = 5        # minutes to mute on flood

MAX_WARNS_DEFAULT = 3

# Example menu content — replace with your real links
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

# ============ LOGGING ============
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("rose_like_bot")

# ============ JSON UTILS ============
def _load(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _atomic_save(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# Persistent dicts
WARNS: Dict[str, Dict[str, int]] = _load(WARN_FILE, {})
SETTINGS: Dict[str, Dict[str, Any]] = _load(SETTINGS_FILE, {})
NOTES: Dict[str, Dict[str, str]] = _load(NOTES_FILE, {})
LOCKS: Dict[str, Dict[str, bool]] = _load(LOCKS_FILE, {})

# Anti-flood buffer
FLOOD_BUCKETS: Dict[int, Dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=64)))

def save_all():
    _atomic_save(WARN_FILE, WARNS)
    _atomic_save(SETTINGS_FILE, SETTINGS)
    _atomic_save(NOTES_FILE, NOTES)
    _atomic_save(LOCKS_FILE, LOCKS)

# ============ HELPERS ============
def mention_html(user) -> str:
    name = user.full_name or user.first_name or "user"
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

def is_group(update: Update) -> bool:
    ct = update.effective_chat
    return ct and ct.type in ("group", "supergroup")

def is_user_admin_in_chat(context: CallbackContext, chat_id: int, user_id: int) -> bool:
    try:
        m = context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

def require_group_and_admin(func):
    def wrapper(update: Update, context: CallbackContext):
        if not is_group(update):
            update.effective_message.reply_text("This command must be used in groups.")
            return
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if user_id == OWNER_ID or is_user_admin_in_chat(context, chat_id, user_id):
            return func(update, context)
        update.effective_message.reply_text("Admins only.")
    return wrapper

def chat_settings(chat_id: int) -> Dict[str, Any]:
    key = str(chat_id)
    SETTINGS.setdefault(key, {
        "welcome_on": True,
        "welcome_text": "👋 Welcome {mention}! Please read the rules.",
        "rules_text": "📜 No spam. Be respectful. Follow admin instructions.",
        "max_warns": MAX_WARNS_DEFAULT,
        "flood_limit": DEFAULT_FLOOD_LIMIT,
        "flood_mute_min": DEFAULT_FLOOD_MUTE_MIN,
    })
    return SETTINGS[key]

def chat_locks(chat_id: int) -> Dict[str, bool]:
    key = str(chat_id)
    LOCKS.setdefault(key, {
        "links": False,
        "media": False,
        "stickers": False,
    })
    return LOCKS[key]

# ============ PRIVATE MENU ============
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
    rows = [[InlineKeyboardButton(title, url=url)] for title, url in SUBJECTS.get(subject, [])]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:content")])
    return InlineKeyboardMarkup(rows)

def family_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, url=link)] for title, link in FAMILY_CHANNELS]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def start(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type == "private":
        update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    else:
        update.message.reply_text("Hi! Add me as admin for moderation. Use /help for commands.")

def cb_handler(update: Update, context: CallbackContext):
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

# ============ WELCOME / GOODBYE ============
def welcome_handler(update: Update, context: CallbackContext):
    if not update.message or not update.message.new_chat_members:
        return
    chat_id = update.effective_chat.id
    st = chat_settings(chat_id)
    if not st.get("welcome_on", True):
        return
    for m in update.message.new_chat_members:
        try:
            text = st.get("welcome_text", "").replace("{mention}", mention_html(m))
            update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

def left_handler(update: Update, context: CallbackContext):
    if not update.message or not update.message.left_chat_member:
        return
    m = update.message.left_chat_member
    try:
        update.message.reply_text(f"👋 Goodbye {m.first_name or 'friend'} — Jai Hind 🇮🇳")
    except Exception:
        pass

@require_group_and_admin
def cmd_setwelcome(update: Update, context: CallbackContext):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()
    if not text:
        msg.reply_text("Usage: /setwelcome Welcome {mention}! Read the rules: /rules")
        return
    chat_settings(chat_id)["welcome_text"] = text
    save_all()
    msg.reply_text("✅ Welcome message updated.")

@require_group_and_admin
def cmd_welcomeon(update: Update, context: CallbackContext):
    chat_settings(update.effective_chat.id)["welcome_on"] = True
    save_all()
    update.effective_message.reply_text("✅ Welcome messages enabled.")

@require_group_and_admin
def cmd_welcomeoff(update: Update, context: CallbackContext):
    chat_settings(update.effective_chat.id)["welcome_on"] = False
    save_all()
    update.effective_message.reply_text("✅ Welcome messages disabled.")

# ============ MODERATION ============
def _get_target_from_reply_or_arg(update: Update, context: CallbackContext) -> Optional[int]:
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    if context.args and context.args[0].isdigit():
        return int(context.args[0])
    return None

@require_group_and_admin
def cmd_ban(update: Update, context: CallbackContext):
    target = _get_target_from_reply_or_arg(update, context)
    if not target:
        update.effective_message.reply_text("Reply to a user or pass user_id: /ban <id>")
        return
    try:
        context.bot.kick_chat_member(update.effective_chat.id, target)
        update.effective_message.reply_text("✅ User banned.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to ban: {e.message}")

@require_group_and_admin
def cmd_unban(update: Update, context: CallbackContext):
    if not context.args or not context.args[0].isdigit():
        update.effective_message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    try:
        context.bot.unban_chat_member(update.effective_chat.id, uid)
        update.effective_message.reply_text("✅ User unbanned.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to unban: {e.message}")

@require_group_and_admin
def cmd_kick(update: Update, context: CallbackContext):
    target = _get_target_from_reply_or_arg(update, context)
    if not target:
        update.effective_message.reply_text("Reply to a user to /kick.")
        return
    try:
        chat_id = update.effective_chat.id
        context.bot.kick_chat_member(chat_id, target)
        context.bot.unban_chat_member(chat_id, target)  # classic "kick"
        update.effective_message.reply_text("✅ User kicked.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to kick: {e.message}")

@require_group_and_admin
def cmd_mute(update: Update, context: CallbackContext):
    target = _get_target_from_reply_or_arg(update, context)
    if not target:
        update.effective_message.reply_text("Reply to a user to /mute or /mute <minutes> while replying.")
        return
    minutes = 10
    if context.args and context.args[0].isdigit():
        minutes = max(1, int(context.args[0]))
    until = int(time.time() + minutes * 60)
    perms = ChatPermissions(can_send_messages=False)
    try:
        context.bot.restrict_chat_member(update.effective_chat.id, target, perms, until)
        update.effective_message.reply_text(f"🔇 Muted for {minutes} minutes.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to mute: {e.message}")

@require_group_and_admin
def cmd_unmute(update: Update, context: CallbackContext):
    target = _get_target_from_reply_or_arg(update, context)
    if not target:
        update.effective_message.reply_text("Reply to a user to /unmute.")
        return
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
    )
    try:
        context.bot.restrict_chat_member(update.effective_chat.id, target, perms)
        update.effective_message.reply_text("🔈 User unmuted.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to unmute: {e.message}")

@require_group_and_admin
def cmd_purge(update: Update, context: CallbackContext):
    msg = update.effective_message
    chat_id = update.effective_chat.id

    if context.args and context.args[0].isdigit():
        n = min(300, int(context.args[0]))
        deleted = 0
        for mid in range(msg.message_id, msg.message_id - n - 1, -1):
            try:
                context.bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception:
                pass
        msg.reply_text(f"🧹 Purged {deleted} messages.")
        return

    if not msg.reply_to_message:
        msg.reply_text("Reply to a message with /purge OR use /purge <count>")
        return

    start = msg.reply_to_message.message_id
    end = msg.message_id
    deleted = 0
    for mid in range(start, end + 1):
        try:
            context.bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    msg.reply_text(f"🧹 Purged {deleted} messages (best effort).")

# ============ WARNS ============
@require_group_and_admin
def cmd_warn(update: Update, context: CallbackContext):
    target = _get_target_from_reply_or_arg(update, context)
    if not target:
        update.effective_message.reply_text("Reply to a user to /warn.")
        return
    chat_id = str(update.effective_chat.id)
    WARNS.setdefault(chat_id, {})
    WARNS[chat_id][str(target)] = WARNS[chat_id].get(str(target), 0) + 1
    save_all()

    maxw = chat_settings(int(chat_id))["max_warns"]
    cnt = WARNS[chat_id][str(target)]
    update.effective_message.reply_text(f"⚠️ Warned. {cnt}/{maxw}")

    if cnt >= maxw:
        try:
            context.bot.kick_chat_member(int(chat_id), target)
            WARNS[chat_id][str(target)] = 0
            save_all()
            update.effective_message.reply_text("🚨 Max warns reached — user banned.")
        except BadRequest as e:
            update.effective_message.reply_text(f"❌ Auto-ban failed: {e.message}")

def cmd_warnings(update: Update, context: CallbackContext):
    # can be used by anyone
    chat_id = str(update.effective_chat.id)
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target = int(context.args[0])
    else:
        update.effective_message.reply_text("Reply to user or pass user ID to see warnings.")
        return
    cnt = WARNS.get(chat_id, {}).get(str(target), 0)
    maxw = chat_settings(int(chat_id))["max_warns"]
    update.effective_message.reply_text(f"📒 Warnings: {cnt}/{maxw}")

@require_group_and_admin
def cmd_clearwarns(update: Update, context: CallbackContext):
    chat_id = str(update.effective_chat.id)
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target = int(context.args[0])
    else:
        update.effective_message.reply_text("Reply to user or pass user ID to clear warnings.")
        return
    WARNS.setdefault(chat_id, {})
    WARNS[chat_id][str(target)] = 0
    save_all()
    update.effective_message.reply_text("✅ Warnings cleared.")

@require_group_and_admin
def cmd_setmaxwarns(update: Update, context: CallbackContext):
    if not context.args or not context.args[0].isdigit():
        update.effective_message.reply_text("Usage: /setmaxwarns <number>")
        return
    n = max(1, min(10, int(context.args[0])))
    chat_settings(update.effective_chat.id)["max_warns"] = n
    save_all()
    update.effective_message.reply_text(f"✅ Max warns set to {n}.")

# ============ RULES ============
@require_group_and_admin
def cmd_setrules(update: Update, context: CallbackContext):
    text = " ".join(context.args).strip()
    if not text:
        update.effective_message.reply_text("Usage: /setrules <text>")
        return
    chat_settings(update.effective_chat.id)["rules_text"] = text
    save_all()
    update.effective_message.reply_text("✅ Rules updated.")

def cmd_rules(update: Update, context: CallbackContext):
    text = chat_settings(update.effective_chat.id)["rules_text"]
    update.effective_message.reply_text(text)

# ============ PINS ============
@require_group_and_admin
def cmd_pin(update: Update, context: CallbackContext):
    if not update.effective_message.reply_to_message:
        update.effective_message.reply_text("Reply to a message to /pin.")
        return
    try:
        update.effective_chat.pin_message(update.effective_message.reply_to_message.message_id, disable_notification=True)
        update.effective_message.reply_text("📌 Pinned.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to pin: {e.message}")

@require_group_and_admin
def cmd_unpin(update: Update, context: CallbackContext):
    try:
        update.effective_chat.unpin_message()
        update.effective_message.reply_text("📍 Unpinned.")
    except BadRequest as e:
        update.effective_message.reply_text(f"❌ Failed to unpin: {e.message}")

# ============ NOTES ============
@require_group_and_admin
def cmd_save(update: Update, context: CallbackContext):
    if len(context.args) < 2:
        update.effective_message.reply_text("Usage: /save <name> <text>")
        return
    name = context.args[0].lower()
    text = " ".join(context.args[1:])
    cid = str(update.effective_chat.id)
    NOTES.setdefault(cid, {})
    NOTES[cid][name] = text
    save_all()
    update.effective_message.reply_text(f"✅ Note '{name}' saved.")

def cmd_get(update: Update, context: CallbackContext):
    if not context.args:
        update.effective_message.reply_text("Usage: /get <name>")
        return
    name = context.args[0].lower()
    cid = str(update.effective_chat.id)
    txt = NOTES.get(cid, {}).get(name)
    if not txt:
        update.effective_message.reply_text("❌ Note not found.")
    else:
        update.effective_message.reply_text(txt)

def cmd_notes(update: Update, context: CallbackContext):
    cid = str(update.effective_chat.id)
    keys = sorted((NOTES.get(cid, {}) or {}).keys())
    if not keys:
        update.effective_message.reply_text("No notes saved.")
        return
    update.effective_message.reply_text("🗒 Notes:\n" + ", ".join(keys))

@require_group_and_admin
def cmd_delnote(update: Update, context: CallbackContext):
    if not context.args:
        update.effective_message.reply_text("Usage: /delnote <name>")
        return
    name = context.args[0].lower()
    cid = str(update.effective_chat.id)
    if name in (NOTES.get(cid, {}) or {}):
        del NOTES[cid][name]
        save_all()
        update.effective_message.reply_text(f"🗑 Deleted note '{name}'.")
    else:
        update.effective_message.reply_text("❌ Note not found.")

# ============ LOCKS ============
LOCKABLES = {"links", "media", "stickers"}

@require_group_and_admin
def cmd_lock(update: Update, context: CallbackContext):
    if not context.args or context.args[0].lower() not in LOCKABLES:
        update.effective_message.reply_text("Usage: /lock <links|media|stickers>")
        return
    t = context.args[0].lower()
    chat_locks(update.effective_chat.id)[t] = True
    save_all()
    update.effective_message.reply_text(f"🔒 Locked {t}.")

@require_group_and_admin
def cmd_unlock(update: Update, context: CallbackContext):
    if not context.args or context.args[0].lower() not in LOCKABLES:
        update.effective_message.reply_text("Usage: /unlock <links|media|stickers>")
        return
    t = context.args[0].lower()
    chat_locks(update.effective_chat.id)[t] = False
    save_all()
    update.effective_message.reply_text(f"🔓 Unlocked {t}.")

def enforcement_handler(update: Update, context: CallbackContext):
    """Deletes messages violating locks; applies anti-flood."""
    if not is_group(update) or not update.effective_message:
        return
    msg = update.effective_message
    chat_id = update.effective_chat.id
    locks = chat_locks(chat_id)

    # Lock checks
    try:
        # links
        if locks.get("links"):
            has_link = False
            if msg.entities:
                for ent in msg.entities:
                    if ent.type in (MessageEntity.URL, MessageEntity.TEXT_LINK):
                        has_link = True
                        break
            if has_link:
                context.bot.delete_message(chat_id, msg.message_id)
                return

        # media
        if locks.get("media"):
            if msg.photo or msg.video or msg.document or msg.audio or msg.voice or msg.animation:
                context.bot.delete_message(chat_id, msg.message_id)
                return

        # stickers
        if locks.get("stickers") and msg.sticker:
            context.bot.delete_message(chat_id, msg.message_id)
            return
    except Exception:
        pass

    # Anti-flood (simple)
    st = chat_settings(chat_id)
    limit = int(st.get("flood_limit", DEFAULT_FLOOD_LIMIT))
    mute_min = int(st.get("flood_mute_min", DEFAULT_FLOOD_MUTE_MIN))
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    dq = FLOOD_BUCKETS[chat_id][user_id]
    now = time.time()
    dq.append(now)
    # remove timestamps older than window
    while dq and now - dq[0] > FLOOD_WINDOW_SEC:
        dq.popleft()

    if len(dq) > limit:
        # mute user
        try:
            perms = ChatPermissions(can_send_messages=False)
            until = int(now + mute_min * 60)
            context.bot.restrict_chat_member(chat_id, user_id, perms, until)
            update.effective_chat.send_message(
                f"🚫 Flood detected: muted {mention_html(update.effective_user)} for {mute_min} min.",
                parse_mode=ParseMode.HTML,
            )
            dq.clear()
        except Exception:
            pass

# ============ UTILS ============
def whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    update.effective_message.reply_text(f"🪪 ID: {u.id}\nName: {u.full_name}")

def help_cmd(update: Update, context: CallbackContext):
    text = (
        "👮 Moderation:\n"
        "/ban /unban <id> /kick\n"
        "/mute [minutes] (reply) /unmute (reply)\n"
        "/purge [count or reply]\n"
        "/warn (reply) /warnings [id] /clearwarns [id] /setmaxwarns <n>\n\n"
        "📜 Rules:\n"
        "/setrules <text> /rules\n\n"
        "📌 Pins:\n"
        "/pin (reply) /unpin\n\n"
        "🗒 Notes:\n"
        "/save <name> <text> /get <name> /notes /delnote <name>\n\n"
        "🔒 Locks:\n"
        "/lock <links|media|stickers> /unlock <...>\n\n"
        "👋 Welcome:\n"
        "/setwelcome <text with {mention}> /welcomeon /welcomeoff\n\n"
        "🔧 Other:\n"
        "/whoami\n"
    )
    update.effective_message.reply_text(text)

def error_handler(update: object, context: CallbackContext):
    logger.error("Exception: %s", context.error)

# ============ MAIN ============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Configure in Render environment variables.")
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Private menu
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Welcome/left
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_handler))
    dp.add_handler(MessageHandler(Filters.status_update.left_chat_member, left_handler))

    # Moderation
    dp.add_handler(CommandHandler("ban", cmd_ban))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("kick", cmd_kick))
    dp.add_handler(CommandHandler("mute", cmd_mute))
    dp.add_handler(CommandHandler("unmute", cmd_unmute))
    dp.add_handler(CommandHandler("purge", cmd_purge))

    # Warns
    dp.add_handler(CommandHandler("warn", cmd_warn))
    dp.add_handler(CommandHandler("warnings", cmd_warnings))
    dp.add_handler(CommandHandler("clearwarns", cmd_clearwarns))
    dp.add_handler(CommandHandler("setmaxwarns", cmd_setmaxwarns))

    # Rules
    dp.add_handler(CommandHandler("setrules", cmd_setrules))
    dp.add_handler(CommandHandler("rules", cmd_rules))

    # Pins
    dp.add_handler(CommandHandler("pin", cmd_pin))
    dp.add_handler(CommandHandler("unpin", cmd_unpin))

    # Notes
    dp.add_handler(CommandHandler("save", cmd_save))
    dp.add_handler(CommandHandler("get", cmd_get))
    dp.add_handler(CommandHandler("notes", cmd_notes))
    dp.add_handler(CommandHandler("delnote", cmd_delnote))

    # Locks + Anti-flood enforcement on every normal message
    dp.add_handler(MessageHandler(Filters.all & ~Filters.status_update, enforcement_handler))

    # Utils
    dp.add_handler(CommandHandler("whoami", whoami))
    dp.add_handler(CommandHandler("help", help_cmd))

    dp.add_error_handler(error_handler)

    logger.info("Bot starting…")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()