import http.server
import os
import socketserver
import threading
import time
import json
import copy
import logging

import telebot
from telebot.apihelper import ApiTelegramException

import firebase_admin
from firebase_admin import credentials, db


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("jisan_auto_post_bot")


# =========================================================
# RENDER PORT BINDING & UPTIMEROBOT
# =========================================================

class HealthCheckHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Jisan Bot is Alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.getenv("PORT", 8080))
    try:
        with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        logger.error("Web server error: %s", e)


threading.Thread(target=run_web_server, daemon=True).start()


# =========================================================
# TELEGRAM BOT SETUP
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN Environment Variable is missing!")

bot = telebot.TeleBot(BOT_TOKEN)


# =========================================================
# FIREBASE SETUP
# =========================================================

FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").strip()

if not FIREBASE_SERVICE_ACCOUNT_JSON:
    raise ValueError(
        "Error: FIREBASE_SERVICE_ACCOUNT_JSON Environment Variable is missing!"
    )

if not FIREBASE_DATABASE_URL:
    raise ValueError(
        "Error: FIREBASE_DATABASE_URL Environment Variable is missing!"
    )


def init_firebase():
    if firebase_admin._apps:
        return

    service_account_data = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)

    cred = credentials.Certificate(service_account_data)
    firebase_admin.initialize_app(
        cred,
        {
            "databaseURL": FIREBASE_DATABASE_URL
        }
    )

    logger.info("Firebase initialized successfully.")


init_firebase()


# One branch for this bot. All persistent state stays here.
firebase_ref = db.reference("jisan_auto_post_bot")


# =========================================================
# IN-MEMORY CACHE
# Firebase is the source of truth.
# =========================================================

state_lock = threading.RLock()

DEFAULT_STATE = {
    "users": {},
    "captions": {},
    "channels": {},
    "sent_messages": {},
    "meta": {
        "next_caption_id": 1,
        "next_sent_message_id": 1,
        "migrated_sqlite": False
    }
}


def deep_default_state():
    return copy.deepcopy(DEFAULT_STATE)


def normalize_state(data):
    state = deep_default_state()

    if isinstance(data, dict):
        for key in ("users", "captions", "channels", "sent_messages"):
            value = data.get(key)
            if isinstance(value, dict):
                state[key] = value

        meta = data.get("meta")
        if isinstance(meta, dict):
            state["meta"].update(meta)

    try:
        state["meta"]["next_caption_id"] = max(
            1, int(state["meta"].get("next_caption_id", 1))
        )
    except Exception:
        state["meta"]["next_caption_id"] = 1

    try:
        state["meta"]["next_sent_message_id"] = max(
            1, int(state["meta"].get("next_sent_message_id", 1))
        )
    except Exception:
        state["meta"]["next_sent_message_id"] = 1

    return state


def load_state():
    try:
        data = firebase_ref.get()
    except Exception:
        logger.exception("Firebase read failed.")
        data = None

    state = normalize_state(data)

    with state_lock:
        global APP_STATE
        APP_STATE = state

    logger.info(
        "Firebase state loaded: %s users, %s captions, %s chats.",
        len(state["users"]),
        len(state["captions"]),
        len(state["channels"])
    )


def save_state():
    with state_lock:
        payload = copy.deepcopy(APP_STATE)

    try:
        firebase_ref.set(payload)
        return True
    except Exception:
        logger.exception("Firebase save failed.")
        return False


APP_STATE = deep_default_state()


# =========================================================
# SQLITE -> FIREBASE ONE-TIME MIGRATION
# Keeps old local data if jisan_bot.db exists.
# Future writes go to Firebase.
# =========================================================

def migrate_sqlite_to_firebase():
    db_path = "jisan_bot.db"

    with state_lock:
        if APP_STATE["meta"].get("migrated_sqlite") is True:
            return

    if not os.path.exists(db_path):
        with state_lock:
            APP_STATE["meta"]["migrated_sqlite"] = True
        save_state()
        return

    try:
        import sqlite3

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Existing users
        try:
            cursor.execute(
                """
                SELECT user_id, target_chat, interval_min,
                       is_active, current_index, last_post_time
                FROM users
                """
            )
            user_rows = cursor.fetchall()
        except Exception:
            user_rows = []

        # Existing captions
        try:
            cursor.execute(
                """
                SELECT id, user_id, caption_text
                FROM captions
                ORDER BY id ASC
                """
            )
            caption_rows = cursor.fetchall()
        except Exception:
            caption_rows = []

        # Existing channels/groups
        try:
            cursor.execute(
                """
                SELECT chat_id, title, added_by
                FROM channels
                """
            )
            channel_rows = cursor.fetchall()
        except Exception:
            channel_rows = []

        # Existing sent messages
        try:
            cursor.execute(
                """
                SELECT id, user_id, chat_id, message_id
                FROM sent_messages
                ORDER BY id ASC
                """
            )
            sent_rows = cursor.fetchall()
        except Exception:
            sent_rows = []

        conn.close()

        with state_lock:
            # Do not overwrite data already present in Firebase.
            for row in user_rows:
                user_id, target_chat, interval_min, is_active, current_index, last_post_time = row
                key = str(user_id)
                if key not in APP_STATE["users"]:
                    APP_STATE["users"][key] = {
                        "user_id": int(user_id),
                        "target_chat": target_chat,
                        "interval_min": int(interval_min or 60),
                        "is_active": int(is_active or 0),
                        "current_index": int(current_index or 0),
                        "last_post_time": float(last_post_time or 0)
                    }

            for row in caption_rows:
                cap_id, user_id, caption_text = row
                key = str(cap_id)
                if key not in APP_STATE["captions"]:
                    APP_STATE["captions"][key] = {
                        "id": int(cap_id),
                        "user_id": int(user_id),
                        "caption_text": caption_text or ""
                    }

            for row in channel_rows:
                chat_id, title, added_by = row
                key = str(chat_id)
                if key not in APP_STATE["channels"]:
                    APP_STATE["channels"][key] = {
                        "chat_id": str(chat_id),
                        "title": title or "Chat",
                        "username": "",
                        "type": "unknown",
                        "added_by": int(added_by or 0)
                    }

            for row in sent_rows:
                sent_id, user_id, chat_id, message_id = row
                key = str(sent_id)
                if key not in APP_STATE["sent_messages"]:
                    APP_STATE["sent_messages"][key] = {
                        "id": int(sent_id),
                        "user_id": int(user_id),
                        "chat_id": str(chat_id),
                        "message_id": int(message_id)
                    }

            max_cap_id = max(
                [int(k) for k in APP_STATE["captions"].keys() if str(k).isdigit()] or [0]
            )
            max_sent_id = max(
                [int(k) for k in APP_STATE["sent_messages"].keys() if str(k).isdigit()] or [0]
            )

            APP_STATE["meta"]["next_caption_id"] = max(
                int(APP_STATE["meta"].get("next_caption_id", 1)),
                max_cap_id + 1
            )
            APP_STATE["meta"]["next_sent_message_id"] = max(
                int(APP_STATE["meta"].get("next_sent_message_id", 1)),
                max_sent_id + 1
            )
            APP_STATE["meta"]["migrated_sqlite"] = True

        save_state()

        logger.info(
            "SQLite migration complete: %s users, %s captions, %s chats, %s sent messages.",
            len(user_rows),
            len(caption_rows),
            len(channel_rows),
            len(sent_rows)
        )

    except Exception:
        logger.exception("SQLite migration failed.")
        # Do not mark migration as complete if it failed.


load_state()
migrate_sqlite_to_firebase()


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def get_user(user_id):
    key = str(user_id)

    with state_lock:
        if key not in APP_STATE["users"]:
            APP_STATE["users"][key] = {
                "user_id": int(user_id),
                "target_chat": None,
                "interval_min": 60,
                "is_active": 0,
                "current_index": 0,
                "last_post_time": 0
            }
            changed = True
        else:
            changed = False

        data = copy.deepcopy(APP_STATE["users"][key])

    if changed:
        save_state()

    return data


def update_user(user_id, **kwargs):
    key = str(user_id)

    with state_lock:
        if key not in APP_STATE["users"]:
            APP_STATE["users"][key] = {
                "user_id": int(user_id),
                "target_chat": None,
                "interval_min": 60,
                "is_active": 0,
                "current_index": 0,
                "last_post_time": 0
            }

        for field, value in kwargs.items():
            if field in {
                "target_chat",
                "interval_min",
                "is_active",
                "current_index",
                "last_post_time",
                "user_id"
            }:
                APP_STATE["users"][key][field] = value

    return save_state()


def get_user_captions(user_id):
    with state_lock:
        rows = [
            copy.deepcopy(item)
            for item in APP_STATE["captions"].values()
            if int(item.get("user_id", 0)) == int(user_id)
        ]

    rows.sort(key=lambda x: int(x.get("id", 0)))
    return rows


MAX_CAPTIONS_PER_USER = 40


def add_caption_to_state(user_id, caption_text):
    with state_lock:
        user_caption_count = sum(
            1
            for item in APP_STATE["captions"].values()
            if int(item.get("user_id", 0)) == int(user_id)
        )

        if user_caption_count >= MAX_CAPTIONS_PER_USER:
            return False

        cap_id = int(APP_STATE["meta"].get("next_caption_id", 1))
        APP_STATE["meta"]["next_caption_id"] = cap_id + 1

        APP_STATE["captions"][str(cap_id)] = {
            "id": cap_id,
            "user_id": int(user_id),
            "caption_text": caption_text
        }

    if not save_state():
        # Roll back on failed save so the caption is not falsely acknowledged.
        with state_lock:
            APP_STATE["captions"].pop(str(cap_id), None)
            APP_STATE["meta"]["next_caption_id"] = cap_id
        return None

    return cap_id


def delete_caption_from_state(cap_id, user_id):
    with state_lock:
        item = APP_STATE["captions"].get(str(cap_id))

        if not item:
            return False

        if int(item.get("user_id", 0)) != int(user_id):
            return False

        del APP_STATE["captions"][str(cap_id)]

    return save_state()


def edit_caption_in_state(cap_id, user_id, new_text):
    with state_lock:
        item = APP_STATE["captions"].get(str(cap_id))

        if not item:
            return False

        if int(item.get("user_id", 0)) != int(user_id):
            return False

        item["caption_text"] = new_text

    return save_state()


def get_channels():
    with state_lock:
        rows = [
            copy.deepcopy(item)
            for item in APP_STATE["channels"].values()
        ]

    rows.sort(key=lambda x: str(x.get("title", "")).lower())
    return rows


def save_channel(chat, added_by=0):
    chat_id = str(chat.id)

    title = (
        getattr(chat, "title", None)
        or getattr(chat, "first_name", None)
        or getattr(chat, "full_name", None)
        or "Unknown Chat"
    )

    username = getattr(chat, "username", None) or ""

    chat_type = getattr(chat, "type", "unknown") or "unknown"

    with state_lock:
        APP_STATE["channels"][chat_id] = {
            "chat_id": chat_id,
            "title": title,
            "username": username,
            "type": chat_type,
            "added_by": int(added_by or 0)
        }

    save_state()


def remove_channel(chat_id):
    with state_lock:
        APP_STATE["channels"].pop(str(chat_id), None)

    save_state()


def get_sent_messages(user_id, chat_id):
    with state_lock:
        rows = [
            copy.deepcopy(item)
            for item in APP_STATE["sent_messages"].values()
            if int(item.get("user_id", 0)) == int(user_id)
            and str(item.get("chat_id")) == str(chat_id)
        ]

    rows.sort(key=lambda x: int(x.get("id", 0)))
    return rows


def add_sent_message(user_id, chat_id, message_id):
    with state_lock:
        sent_id = int(APP_STATE["meta"].get("next_sent_message_id", 1))
        APP_STATE["meta"]["next_sent_message_id"] = sent_id + 1

        APP_STATE["sent_messages"][str(sent_id)] = {
            "id": sent_id,
            "user_id": int(user_id),
            "chat_id": str(chat_id),
            "message_id": int(message_id)
        }

    if not save_state():
        with state_lock:
            APP_STATE["sent_messages"].pop(str(sent_id), None)
            APP_STATE["meta"]["next_sent_message_id"] = sent_id
        return None

    return sent_id


def delete_sent_record(sent_id):
    with state_lock:
        APP_STATE["sent_messages"].pop(str(sent_id), None)

    return save_state()


# =========================================================
# FORWARDED CHANNEL / GROUP DETECTOR
# =========================================================

@bot.message_handler(
    func=lambda msg: (
        getattr(msg, "forward_from_chat", None) is not None
        and getattr(msg.forward_from_chat, "type", None)
        in ["channel", "supergroup", "group"]
    )
)
def handle_channel_forward(message):
    user_id = message.from_user.id
    channel = message.forward_from_chat
    chat_id = str(channel.id)
    title = channel.title or "Chat"

    try:
        bot_user = bot.get_me()
        member = bot.get_chat_member(chat_id, bot_user.id)

        if member.status in ["administrator", "creator"]:
            save_channel(channel, user_id)

            update_user(
                user_id,
                target_chat=chat_id
            )

            bot.reply_to(
                message,
                f"✅ চ্যানেল বা গ্রুপ সফলভাবে সনাক্ত এবং সেট করা হয়েছে!\n\n"
                f"📌 Target Chat: {title}\n"
                f"🆔 ID: {chat_id}\n\n"
                f"এখন থেকে আপনার অটো পোস্ট এখানে যাবে।"
            )
        else:
            bot.reply_to(
                message,
                f"⚠️ গ্রুপ/চ্যানেল পাওয়া গেছে ({title}), কিন্তু বটকে সেখানে "
                f"Admin বানানো হয়নি!\n\nঅনুগ্রহ করে বটকে Admin বানিয়ে আবার "
                f"মেসেজ ফরওয়ার্ড করুন।"
            )

    except Exception:
        logger.exception("Forwarded chat verification failed.")
        bot.reply_to(
            message,
            "❌ ভেরিফাই করা যায়নি। বটকে ওই গ্রুপ বা চ্যানেলে Admin বানিয়ে "
            "আবার চেষ্টা করুন।"
        )


# =========================================================
# BOT ADDED / REMOVED TRACKING
# =========================================================

@bot.my_chat_member_handler()
def handle_chat_member_update(event):
    chat_id = str(event.chat.id)
    title = event.chat.title or "Unknown Chat"
    user_id = event.from_user.id
    new_status = event.new_chat_member.status

    try:
        if new_status in ["administrator", "member", "creator"]:
            save_channel(event.chat, user_id)

        elif new_status in ["left", "kicked"]:
            remove_channel(chat_id)

    except Exception:
        logger.exception("Failed to update chat membership state.")


# =========================================================
# /START / HELP
# =========================================================

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    user_id = message.from_user.id
    get_user(user_id)

    text = (
        "🏴‍☠️ Welcome to Jisan Bot! 🩸\n\n"
        "👑 The official Auto Post automation bot by Jisan Brand.\n\n"
        "⚡ Available Commands:\n"
        "• /addcaption <text> – নতুন ক্যাপশন যোগ করুন\n"
        "• /mycaptions – ক্যাপশন ম্যানেজ বা ডিলিট/এডিট করুন\n"
        "• /settarget – চ্যানেল বা গ্রুপ সিলেক্ট বা সেট করুন\n"
        "• /settime <minutes> – টাইম সেট করুন (মিনিটে)\n"
        "• /startpost – অটো পোস্ট চালু করুন\n"
        "• /stoppost – অটো পোস্ট বন্ধ করুন\n\n"
        "💡 টিপস: আপনার চ্যানেল বা গ্রুপ থেকে যেকোনো ১টি মেসেজ "
        "এই বটের ইনবক্সে ফরওয়ার্ড করলেও অটো সেভ হয়ে যাবে!"
    )

    if user_id == ADMIN_ID:
        text += (
            "\n\n• /stats – বট স্ট্যাটাস দেখুন (Admin)\n"
            "• /broadcast <msg> – সবাইকে মেসেজ পাঠান (Admin)"
        )

    bot.reply_to(message, text)


# =========================================================
# /ADDCAPTION
# =========================================================

@bot.message_handler(commands=["addcaption"])
def add_caption(message):
    user_id = message.from_user.id
    caption_text = message.text.replace("/addcaption", "", 1).strip()

    if not caption_text:
        bot.reply_to(
            message,
            "❌ ব্যবহার পদ্ধতি: /addcaption আপনার ক্যাপশন লিখুন"
        )
        return

    cap_id = add_caption_to_state(
        user_id,
        caption_text
    )

    if cap_id is False:
        bot.reply_to(
            message,
            "❌ সর্বোচ্চ ৪০টি ক্যাপশন সেভ করা যাবে। নতুন ক্যাপশন যোগ করতে আগে একটি ক্যাপশন ডিলিট করুন।"
        )
        return

    if cap_id is None:
        bot.reply_to(
            message,
            "❌ ক্যাপশন সেভ করা যায়নি। Firebase connection আবার চেষ্টা করুন।"
        )
        return

    bot.reply_to(
        message,
        "✅ ক্যাপশন সফলভাবে সেভ হয়েছে!\nদেখতে /mycaptions লিখুন।"
    )


# =========================================================
# PAGINATED CAPTION MANAGER
# Fixes /mycaptions failure when many/long captions exceed Telegram limit.
# =========================================================

CAPTIONS_PER_PAGE = 3


def build_caption_manager(user_id, page=0):
    rows = get_user_captions(user_id)

    total = len(rows)
    total_pages = max(
        1,
        (total + CAPTIONS_PER_PAGE - 1) // CAPTIONS_PER_PAGE
    )

    page = max(0, min(int(page), total_pages - 1))

    start = page * CAPTIONS_PER_PAGE
    page_rows = rows[start:start + CAPTIONS_PER_PAGE]

    if not rows:
        text = (
            "⚠️ আপনার কোনো সেভ করা ক্যাপশন নেই!\n"
            "নতুন ক্যাপশন যোগ করতে /addcaption ব্যবহার করুন।"
        )
        markup = telebot.types.InlineKeyboardMarkup()
        return text, markup

    msg_parts = [
        "📋 <b>আপনার সেভ করা ক্যাপশনসমূহ</b>\n",
        f"📊 মোট ক্যাপশন: <b>{total}</b>\n",
        f"📄 পেজ: <b>{page + 1}/{total_pages}</b>\n"
    ]

    markup = telebot.types.InlineKeyboardMarkup()

    for idx, row in enumerate(
        page_rows,
        start=start + 1
    ):
        cap_id = int(row["id"])
        caption = str(row.get("caption_text", ""))

        # Keep each page safely below Telegram's text limit.
        # Full caption text is preserved in Firebase; manager only shows a preview.
        preview = caption
        if len(preview) > 950:
            preview = preview[:947] + "..."

        msg_parts.append(
            f"\n--- [ Serial: {idx} ] ---\n"
            f"{preview}\n"
        )

        btn_edit = telebot.types.InlineKeyboardButton(
            f"✏️ এডিট #{idx}",
            callback_data=f"edit_cap:{cap_id}:{idx}"
        )

        btn_del = telebot.types.InlineKeyboardButton(
            f"🗑️ ডিলিট #{idx}",
            callback_data=f"del_cap:{cap_id}:{idx}"
        )

        markup.row(btn_edit, btn_del)

    navigation = []

    if page > 0:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                "‹ Previous",
                callback_data=f"caps_page:{page - 1}"
            )
        )

    if page < total_pages - 1:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                "Next ›",
                callback_data=f"caps_page:{page + 1}"
            )
        )

    if navigation:
        markup.row(*navigation)

    return "".join(msg_parts), markup


def show_caption_manager(chat_id, user_id, message_id=None, page=0):
    text, markup = build_caption_manager(
        user_id,
        page
    )

    try:
        if message_id:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
        else:
            bot.send_message(
                chat_id,
                text,
                reply_markup=markup,
                parse_mode="HTML"
            )
        return
    except Exception:
        logger.exception("Caption manager display failed.")

    # Final safe fallback.
    try:
        bot.send_message(
            chat_id,
            "📋 ক্যাপশন ম্যানেজার লোড করা সম্ভব হয়নি। আবার /mycaptions দিন।"
        )
    except Exception:
        pass


@bot.message_handler(commands=["mycaptions", "editcaption", "deletecaption"])
def handle_caption_manager(message):
    show_caption_manager(
        message.chat.id,
        message.from_user.id
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("caps_page:")
)
def callback_caption_page(call):
    try:
        page = int(call.data.split(":", 1)[1])
    except Exception:
        page = 0

    bot.answer_callback_query(call.id)

    show_caption_manager(
        call.message.chat.id,
        call.from_user.id,
        message_id=call.message.message_id,
        page=page
    )


# =========================================================
# DELETE CAPTION
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("del_cap:")
)
def callback_delete_caption(call):
    parts = call.data.split(":")

    if len(parts) < 3:
        bot.answer_callback_query(
            call.id,
            "❌ Invalid request."
        )
        return

    cap_id = parts[1]
    idx = parts[2]
    user_id = call.from_user.id

    deleted = delete_caption_from_state(
        cap_id,
        user_id
    )

    if deleted:
        bot.answer_callback_query(
            call.id,
            f"✅ সিরিয়াল #{idx} ডিলিট করা হয়েছে!"
        )
    else:
        bot.answer_callback_query(
            call.id,
            "❌ ক্যাপশন পাওয়া যায়নি।"
        )

    show_caption_manager(
        call.message.chat.id,
        user_id,
        message_id=call.message.message_id
    )


# =========================================================
# EDIT CAPTION
# =========================================================

def process_new_caption_step(message, cap_id, user_id):
    if message.text and message.text.strip().lower() == "/cancel":
        bot.reply_to(
            message,
            "❌ এডিট বাতিল করা হয়েছে।"
        )
        return

    new_text = message.text or getattr(message, "caption", None)

    if not new_text:
        bot.reply_to(
            message,
            "❌ ক্যাপশন খালি রাখা যাবে না। আবার চেষ্টা করুন।"
        )
        return

    if edit_caption_in_state(
        cap_id,
        user_id,
        new_text
    ):
        bot.reply_to(
            message,
            "✅ ক্যাপশন সফলভাবে আপডেট করা হয়েছে!"
        )
    else:
        bot.reply_to(
            message,
            "❌ ক্যাপশন আপডেট করা যায়নি।"
        )

    show_caption_manager(
        message.chat.id,
        user_id
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_cap:")
)
def callback_edit_caption(call):
    parts = call.data.split(":")

    if len(parts) < 3:
        bot.answer_callback_query(
            call.id,
            "❌ Invalid request."
        )
        return

    cap_id = parts[1]
    idx = parts[2]
    user_id = call.from_user.id

    bot.answer_callback_query(
        call.id,
        f"সিরিয়াল #{idx} এডিট হচ্ছে..."
    )

    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ সিরিয়াল #{idx} এর জন্য নতুন ক্যাপশনটি লিখে বা পেস্ট করে পাঠান:\n\n"
        "(বা বাতিল করতে /cancel লিখুন)"
    )

    bot.register_next_step_handler(
        msg,
        process_new_caption_step,
        cap_id,
        user_id
    )


# =========================================================
# TARGET SETTER
# =========================================================

@bot.message_handler(commands=["settarget", "channels"])
def set_target(message):
    user_id = message.from_user.id

    args = (
        message.text.replace("/settarget", "")
        .replace("/channels", "")
        .strip()
    )

    if args:
        target = args

        try:
            chat_info = bot.get_chat(target)
            chat_id = str(chat_info.id)
            title = chat_info.title or target

            bot_user = bot.get_me()
            member = bot.get_chat_member(
                chat_id,
                bot_user.id
            )

            if member.status in ["administrator", "creator"]:
                save_channel(
                    chat_info,
                    user_id
                )

                update_user(
                    user_id,
                    target_chat=chat_id
                )

                bot.reply_to(
                    message,
                    f"✅ গ্রুপ/চ্যানেল সফলভাবে সেট হয়েছে!\n\n"
                    f"📌 Target Chat: {title}\n"
                    f"🆔 ID: {chat_id}"
                )

            else:
                bot.reply_to(
                    message,
                    f"⚠️ বটকে {target} গ্রুপ বা চ্যানেলে Admin করা হয়নি! "
                    f"আগে Admin বানিয়ে আবার চেষ্টা করুন।"
                )

        except Exception:
            update_user(
                user_id,
                target_chat=target
            )

            bot.reply_to(
                message,
                f"🎯 টার্গেট সেট করা হয়েছে: {target}\n"
                "(মনে রাখবেন, বটকে ওই গ্রুপ/চ্যানেলে Admin থাকতে হবে)"
            )

        return

    rows = get_channels()

    markup = telebot.types.InlineKeyboardMarkup()

    if rows:
        for row in rows:
            chat_id = row.get("chat_id", "")
            title = str(row.get("title", "Unknown Chat"))

            # Telegram callback_data has a small byte limit.
            # Only the ID is placed in callback_data.
            markup.add(
                telebot.types.InlineKeyboardButton(
                    text=f"📢 {title[:45]}",
                    callback_data=f"select_chat:{chat_id}"
                )
            )

    msg_text = (
        "🎯 গ্রুপ বা চ্যানেল সেট করার সহজ উপায়সমূহ:\n\n"
        "১. মেসেজ ফরওয়ার্ড (সেরা): আপনার গ্রুপ বা চ্যানেল থেকে যেকোনো ১টি "
        "পোস্ট এই বটের ইনবক্সে Forward করুন।\n"
        "২. ইউজারনেম/আইডি দিয়ে: /settarget @YourUsername বা আইডি লিখুন।\n"
    )

    if rows:
        msg_text += "৩. অথবা নিচের বাটন থেকে সিলেক্ট করুন:"
        bot.reply_to(
            message,
            msg_text,
            reply_markup=markup
        )
    else:
        msg_text += (
            "\n👉 আপনার গ্রুপ বা চ্যানেল থেকে ১টি পোস্ট ফরওয়ার্ড করে দিন, "
            "সাথে সাথে সেট হয়ে যাবে!"
        )
        bot.reply_to(
            message,
            msg_text
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("select_chat:")
)
def callback_select_chat(call):
    chat_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    update_user(
        user_id,
        target_chat=chat_id
    )

    selected = None

    with state_lock:
        selected = copy.deepcopy(
            APP_STATE["channels"].get(str(chat_id))
        )

    title = (
        selected.get("title", "Selected Chat")
        if selected
        else "Selected Chat"
    )

    bot.answer_callback_query(
        call.id,
        f"সিলেক্ট করা হয়েছে: {title[:50]}"
    )

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            f"✅ গ্রুপ/চ্যানেল সফলভাবে সেট হয়েছে!\n\n"
            f"📌 Target Chat: {title}\n"
            f"🆔 ID: {chat_id}\n\n"
            f"এখন থেকে আপনার অটো পোস্ট এই গ্রুপ বা চ্যানেলে যাবে।"
        )
    )


# =========================================================
# /SETTIME
# =========================================================

@bot.message_handler(commands=["settime"])
def set_time(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) < 2 or not args[1].isdigit():
        bot.reply_to(
            message,
            "❌ ব্যবহার পদ্ধতি: /settime <minutes> (যেমন: /settime 60)"
        )
        return

    interval = int(args[1])

    if interval < 1:
        bot.reply_to(
            message,
            "❌ কমপক্ষে 1 মিনিট সেট করুন।"
        )
        return

    update_user(
        user_id,
        interval_min=interval
    )

    bot.reply_to(
        message,
        f"⏱️ টাইম ইন্টারভাল সেট করা হয়েছে: {interval} মিনিট পর পর।"
    )


# =========================================================
# /STARTPOST
# =========================================================

@bot.message_handler(commands=["startpost"])
def start_post(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    if not user["target_chat"]:
        bot.reply_to(
            message,
            "❌ আপনি এখনও কোনো গ্রুপ বা চ্যানেল সিলেক্ট করেননি!\n"
            "আপনার গ্রুপ/চ্যানেল থেকে ১টি পোস্ট এখানে ফরওয়ার্ড করুন "
            "অথবা /settarget লিখুন।"
        )
        return

    captions = get_user_captions(user_id)

    if not captions:
        bot.reply_to(
            message,
            "❌ আপনার কোনো সেভ করা ক্যাপশন নেই!\n"
            "/addcaption দিয়ে আগে ক্যাপশন যোগ করুন।"
        )
        return

    update_user(
        user_id,
        is_active=1,
        last_post_time=0
    )

    bot.reply_to(
        message,
        "🚀 অটো পোস্ট চালু করা হয়েছে! সময় অনুযায়ী পোস্ট হওয়া শুরু হবে।"
    )


# =========================================================
# /STOPPOST
# =========================================================

@bot.message_handler(commands=["stoppost"])
def stop_post(message):
    user_id = message.from_user.id

    update_user(
        user_id,
        is_active=0
    )

    bot.reply_to(
        message,
        "🛑 অটো পোস্ট বন্ধ করা হয়েছে।"
    )


# =========================================================
# ADMIN STATS
# =========================================================

@bot.message_handler(commands=["stats"])
def admin_stats(message):
    if message.from_user.id != ADMIN_ID:
        return

    with state_lock:
        total_users = len(APP_STATE["users"])
        active_users = sum(
            1
            for user in APP_STATE["users"].values()
            if int(user.get("is_active", 0)) == 1
        )
        total_captions = len(APP_STATE["captions"])
        total_channels = len(APP_STATE["channels"])

    bot.reply_to(
        message,
        f"📊 Admin Analytics\n\n"
        f"Total Users: {total_users}\n"
        f"Active Auto Posters: {active_users}\n"
        f"Saved Captions: {total_captions}\n"
        f"Saved Channels/Groups: {total_channels}"
    )


# =========================================================
# ADMIN BROADCAST
# =========================================================

@bot.message_handler(commands=["broadcast"])
def admin_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return

    broadcast_msg = message.text.replace(
        "/broadcast",
        "",
        1
    ).strip()

    if not broadcast_msg:
        bot.reply_to(
            message,
            "❌ ব্যবহার: /broadcast আপনার মেসেজ"
        )
        return

    with state_lock:
        users = [
            int(user.get("user_id"))
            for user in APP_STATE["users"].values()
            if user.get("user_id") is not None
        ]

    count = 0

    for user_id in users:
        try:
            bot.send_message(
                user_id,
                broadcast_msg
            )
            count += 1

            # Small delay to reduce burst pressure.
            time.sleep(0.05)

        except Exception:
            pass

    bot.reply_to(
        message,
        f"📢 মোট {count} জন ইউজারের কাছে মেসেজ পাঠানো হয়েছে!"
    )


# =========================================================
# AUTO POSTER ENGINE
# Firebase is used for the entire persistent state.
#
# IMPORTANT:
# - A caption is deleted ONLY after successful Telegram posting.
# - Failed posts keep the caption safe in Firebase.
# - The bot posts the oldest saved caption first.
# - The existing 3-message deletion rule remains.
# =========================================================

def auto_poster_loop():
    while True:
        try:
            time.sleep(15)
            current_time = time.time()

            with state_lock:
                active_posters = [
                    (
                        int(user.get("user_id")),
                        user.get("target_chat"),
                        int(user.get("interval_min", 60) or 60),
                        float(user.get("last_post_time", 0) or 0)
                    )
                    for user in APP_STATE["users"].values()
                    if int(user.get("is_active", 0)) == 1
                    and user.get("target_chat")
                ]

            for u_id, target, interval, last_time in active_posters:

                if current_time - last_time < (interval * 60):
                    continue

                captions = get_user_captions(u_id)

                if not captions:
                    # No pending captions. Keep auto-post mode but wait.
                    continue

                cap = captions[0]
                cap_id = int(cap["id"])
                post_text = str(cap.get("caption_text", ""))

                if not post_text.strip():
                    # Empty/corrupt caption should not block the queue forever.
                    delete_caption_from_state(
                        cap_id,
                        u_id
                    )
                    update_user(
                        u_id,
                        last_post_time=current_time
                    )
                    continue

                chat_target = target

                if str(target).lstrip("-").isdigit():
                    chat_target = int(target)

                try:
                    # 1. Post message.
                    sent_msg = bot.send_message(
                        chat_target,
                        post_text
                    )

                    # 2. Record sent message first.
                    sent_id = add_sent_message(
                        u_id,
                        str(target),
                        sent_msg.message_id
                    )

                    # 3. Keep only the newest 3 posts.
                    history = get_sent_messages(
                        u_id,
                        str(target)
                    )

                    if len(history) > 3:
                        old_items = history[:-3]

                        for old_item in old_items:
                            old_msg_id = int(
                                old_item["message_id"]
                            )

                            try:
                                bot.delete_message(
                                    chat_target,
                                    old_msg_id
                                )
                            except Exception:
                                pass

                            delete_sent_record(
                                old_item["id"]
                            )

                    # 4. IMPORTANT:
                    # Delete caption ONLY AFTER successful post.
                    # It remains safe in Firebase until here.
                    deleted = delete_caption_from_state(
                        cap_id,
                        u_id
                    )

                    # 5. Mark the posting time.
                    update_user(
                        u_id,
                        last_post_time=time.time()
                    )

                    if deleted:
                        logger.info(
                            "Posted caption #%s for user %s successfully; caption removed.",
                            cap_id,
                            u_id
                        )
                    else:
                        logger.warning(
                            "Posted caption #%s for user %s, but Firebase deletion failed. "
                            "The caption may remain for retry.",
                            cap_id,
                            u_id
                        )

                except ApiTelegramException as e:
                    logger.warning(
                        "Auto post Telegram error for user %s: %s",
                        u_id,
                        e
                    )

                except Exception as e:
                    logger.exception(
                        "Auto post failed for user %s: %s",
                        u_id,
                        e
                    )

        except Exception:
            logger.exception("Auto poster loop error.")
            time.sleep(5)


# Start auto poster thread.
threading.Thread(
    target=auto_poster_loop,
    daemon=True
).start()


# =========================================================
# RUN BOT
# =========================================================

if __name__ == "__main__":
    logger.info("Jisan Auto Post Bot starting...")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=False
            )
        except Exception as e:
            logger.exception(
                "Bot polling stopped unexpectedly: %s",
                e
            )
            time.sleep(5)
