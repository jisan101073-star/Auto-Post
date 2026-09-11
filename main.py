import http.server
import os
import socketserver
import sqlite3
import threading
import time
import telebot
from telebot.apihelper import ApiTelegramException


# --- RENDER PORT BINDING & UPTIMEROBOT FIX ---
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
        print(f"Web server error: {e}")


threading.Thread(target=run_web_server, daemon=True).start()


# --- TELEGRAM BOT & DATABASE SETUP ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN Environment Variable is missing!")

bot = telebot.TeleBot(BOT_TOKEN)

# SQLite Database Setup
db_lock = threading.Lock()
conn = sqlite3.connect("jisan_bot.db", check_same_thread=False)
cursor = conn.cursor()

# Create Tables
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    target_chat TEXT,
    interval_min INTEGER DEFAULT 60,
    is_active INTEGER DEFAULT 0,
    current_index INTEGER DEFAULT 0,
    last_post_time REAL DEFAULT 0
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS captions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    caption_text TEXT
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS channels (
    chat_id TEXT PRIMARY KEY,
    title TEXT,
    added_by INTEGER
)
"""
)
conn.commit()


# Helper Functions
def get_user(user_id):
    with db_lock:
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        if not res:
            cursor.execute(
                "INSERT INTO users (user_id) VALUES (?)", (user_id,)
            )
            conn.commit()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            res = cursor.fetchone()
        return {
            "user_id": res[0],
            "target_chat": res[1],
            "interval_min": res[2],
            "is_active": res[3],
            "current_index": res[4],
            "last_post_time": res[5],
        }


def update_user(user_id, **kwargs):
    with db_lock:
        for key, val in kwargs.items():
            cursor.execute(
                f"UPDATE users SET {key} = ? WHERE user_id = ?", (val, user_id)
            )
        conn.commit()


# --- AUTOMATIC CHANNEL DETECTOR VIA FORWARDED MESSAGES ---
@bot.message_handler(
    func=lambda msg: msg.forward_from_chat is not None
    and msg.forward_from_chat.type == "channel"
)
def handle_channel_forward(message):
    user_id = message.from_user.id
    channel = message.forward_from_chat
    chat_id = str(channel.id)
    title = channel.title or "Channel"

    try:
        bot_user = bot.get_me()
        member = bot.get_chat_member(chat_id, bot_user.id)

        if member.status in ["administrator", "creator"]:
            with db_lock:
                cursor.execute(
                    "INSERT OR REPLACE INTO channels (chat_id, title, added_by)"
                    " VALUES (?, ?, ?)",
                    (chat_id, title, user_id),
                )
                conn.commit()

            update_user(user_id, target_chat=chat_id)
            bot.reply_to(
                message,
                f"✅ চ্যানেল সফলভাবে সনাক্ত এবং সেট করা হয়েছে!\n\n📌 Target"
                f" Channel: {title}\n🆔 ID: {chat_id}\n\nএখন থেকে আপনার অটো পোস্ট"
                " এই চ্যানেলে যাবে।",
            )
        else:
            bot.reply_to(
                message,
                f"⚠️ চ্যানেল পাওয়া গেছে ({title}), কিন্তু বটকে ওই চ্যানেলে Admin"
                " বানানো হয়নি!\n\nঅনুগ্রহ করে বটকে Admin বানিয়ে আবার মেসেজ"
                " ফরওয়ার্ড করুন।",
            )
    except Exception as e:
        bot.reply_to(
            message,
            "❌ চ্যানেল ভেরিফাই করা যায়নি। বটকে ওই চ্যানেলে Admin বানিয়ে আবার চেষ্টা"
            " করুন।",
        )


@bot.my_chat_member_handler()
def handle_chat_member_update(event):
    chat_id = str(event.chat.id)
    title = event.chat.title or "Unknown Channel"
    user_id = event.from_user.id
    new_status = event.new_chat_member.status

    with db_lock:
        if new_status in ["administrator", "member"]:
            cursor.execute(
                "INSERT OR REPLACE INTO channels (chat_id, title, added_by)"
                " VALUES (?, ?, ?)",
                (chat_id, title, user_id),
            )
        elif new_status in ["left", "kicked"]:
            cursor.execute(
                "DELETE FROM channels WHERE chat_id = ?", (chat_id,)
            )
        conn.commit()


# --- COMMAND HANDLERS ---


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
        "• /settarget – চ্যানেল সিলেক্ট বা সেট করুন\n"
        "• /settime <minutes> – টাইম সেট করুন (মিনিটে)\n"
        "• /startpost – অটো পোস্ট চালু করুন\n"
        "• /stoppost – অটো পোস্ট বন্ধ করুন\n\n"
        "💡 টিপস: আপনার চ্যানেল থেকে যেকোনো ১টি মেসেজ এই বটের ইনবক্সে ফরওয়ার্ড করলেও চ্যানেল অটো সেভ হয়ে যাবে!"
    )

    if user_id == ADMIN_ID:
        text += (
            "\n\n• /stats – বট স্ট্যাটাস দেখুন (Admin)\n• /broadcast <msg> –"
            " সবাইকে মেসেজ পাঠান (Admin)"
        )

    bot.reply_to(message, text)


@bot.message_handler(commands=["addcaption"])
def add_caption(message):
    user_id = message.from_user.id
    caption_text = message.text.replace("/addcaption", "", 1).strip()

    if not caption_text:
        bot.reply_to(message, "❌ ব্যবহার পদ্ধতি: /addcaption আপনার ক্যাপশন লিখুন")
        return

    with db_lock:
        cursor.execute(
            "INSERT INTO captions (user_id, caption_text) VALUES (?, ?)",
            (user_id, caption_text),
        )
        conn.commit()

    bot.reply_to(
        message, "✅ ক্যাপশন সফলভাবে সেভ হয়েছে!\nদেখতে /mycaptions লিখুন।"
    )


# --- INTERACTIVE CAPTION MANAGER ---
def show_caption_manager(chat_id, user_id, message_id=None):
    with db_lock:
        cursor.execute(
            "SELECT id, caption_text FROM captions WHERE user_id = ?", (user_id,)
        )
        rows = cursor.fetchall()

    if not rows:
        text = (
            "⚠️ আপনার কোনো সেভ করা ক্যাপশন নেই!\nনতুন ক্যাপশন যোগ করতে"
            " /addcaption ব্যবহার করুন।"
        )
        if message_id:
            try:
                bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id
                )
            except Exception:
                bot.send_message(chat_id, text)
        else:
            bot.send_message(chat_id, text)
        return

    msg = "📋 আপনার সেভ করা সম্পূর্ণ ক্যাপশনসমূহ:\n\n"
    markup = telebot.types.InlineKeyboardMarkup()

    for idx, row in enumerate(rows, start=1):
        cap_id = row[0]
        msg += f"--- [ Serial: {idx} ] ---\n{row[1]}\n\n"

        btn_edit = telebot.types.InlineKeyboardButton(
            f"✏️ এডিট #{idx}", callback_data=f"edit_cap:{cap_id}:{idx}"
        )
        btn_del = telebot.types.InlineKeyboardButton(
            f"🗑️ ডিলিট #{idx}", callback_data=f"del_cap:{cap_id}:{idx}"
        )
        markup.row(btn_edit, btn_del)

    msg += (
        "------------------------------------\n👇 নিচের বাটনে চাপ দিয়ে এডিট বা"
        " ডিলিট করুন:"
    )

    if message_id:
        try:
            bot.edit_message_text(
                msg, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except Exception:
            bot.send_message(chat_id, msg, reply_markup=markup)
    else:
        bot.send_message(chat_id, msg, reply_markup=markup)


@bot.message_handler(commands=["mycaptions", "editcaption", "deletecaption"])
def handle_caption_manager(message):
    show_caption_manager(message.chat.id, message.from_user.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("del_cap:")
)
def callback_delete_caption(call):
    parts = call.data.split(":")
    cap_id = parts[1]
    idx = parts[2]
    user_id = call.from_user.id

    with db_lock:
        cursor.execute(
            "DELETE FROM captions WHERE id = ? AND user_id = ?",
            (cap_id, user_id),
        )
        conn.commit()

    bot.answer_callback_query(call.id, f"✅ সিরিয়াল #{idx} ডিলিট করা হয়েছে!")
    show_caption_manager(
        call.message.chat.id, user_id, message_id=call.message.message_id
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_cap:")
)
def callback_edit_caption(call):
    parts = call.data.split(":")
    cap_id = parts[1]
    idx = parts[2]
    user_id = call.from_user.id

    bot.answer_callback_query(call.id, f"সিরিয়াল #{idx} এডিট হচ্ছে...")
    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ সিরিয়াল #{idx} এর জন্য নতুন ক্যাপশনটি লিখে বা পেস্ট করে"
        " পাঠান:\n\n(বা বাতিল করতে /cancel লিখুন)",
    )
    bot.register_next_step_handler(
        msg, process_new_caption_step, cap_id, user_id
    )


def process_new_caption_step(message, cap_id, user_id):
    if message.text and message.text.strip().lower() == "/cancel":
        bot.reply_to(message, "❌ এডিট বাতিল করা হয়েছে।")
        return

    new_text = message.text or message.caption
    if not new_text:
        bot.reply_to(
            message, "❌ ক্যাপশন খালি রাখা যাবে না। আবার চেষ্টা করুন।"
        )
        return

    with db_lock:
        cursor.execute(
            "UPDATE captions SET caption_text = ? WHERE id = ? AND user_id = ?",
            (new_text, cap_id, user_id),
        )
        conn.commit()

    bot.reply_to(message, "✅ ক্যাপশন সফলভাবে আপডেট করা হয়েছে!")
    show_caption_manager(message.chat.id, user_id)


# --- TARGET SETTER & CHANNEL SELECTOR ---
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
            member = bot.get_chat_member(chat_id, bot_user.id)

            if member.status in ["administrator", "creator"]:
                with db_lock:
                    cursor.execute(
                        "INSERT OR REPLACE INTO channels (chat_id, title,"
                        " added_by) VALUES (?, ?, ?)",
                        (chat_id, title, user_id),
                    )
                    conn.commit()
                update_user(user_id, target_chat=chat_id)
                bot.reply_to(
                    message,
                    f"✅ চ্যানেল সফলভাবে সেট হয়েছে!\n\n📌 Target Channel:"
                    f" {title}\n🆔 ID: {chat_id}",
                )
            else:
                bot.reply_to(
                    message,
                    f"⚠️ বটকে {target} চ্যানেলে Admin করা হয়নি! আগে Admin বানিয়ে"
                    " আবার চেষ্টা করুন।",
                )
        except Exception:
            update_user(user_id, target_chat=target)
            bot.reply_to(
                message,
                f"🎯 টার্গেট সেট করা হয়েছে: {target}\n(মনে রাখবেন, বটকে ওই"
                " চ্যানেলে Admin থাকতে হবে)",
            )
        return

    with db_lock:
        cursor.execute("SELECT chat_id, title FROM channels")
        rows = cursor.fetchall()

    markup = telebot.types.InlineKeyboardMarkup()
    if rows:
        for chat_id, title in rows:
            markup.add(
                telebot.types.InlineKeyboardButton(
                    text=f"📢 {title}",
                    callback_data=f"select_chat:{chat_id}:{title[:15]}",
                )
            )

    msg_text = (
        "🎯 চ্যানেল সেট করার ৩টি সহজ উপায়:\n\n"
        "১. মেসেজ ফরওয়ার্ড (সেরা): আপনার চ্যানেল থেকে যেকোনো ১টি পোস্ট এই বটের ইনবক্সে Forward করুন।\n"
        "২. ইউজারনেম দিয়ে: /settarget @YourChannelUsername লিখুন।\n"
    )

    if rows:
        msg_text += "৩. অথবা নিচের বাটন থেকে সিলেক্ট করুন:"
        bot.reply_to(message, msg_text, reply_markup=markup)
    else:
        msg_text += "\n👉 আপনার চ্যানেল থেকে ১টি পোস্ট ফরওয়ার্ড করে দিন, সাথে সাথে সেট হয়ে যাবে!"
        bot.reply_to(message, msg_text)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("select_chat:")
)
def callback_select_chat(call):
    data_parts = call.data.split(":", 2)
    chat_id = data_parts[1]
    title = data_parts[2]
    user_id = call.from_user.id

    update_user(user_id, target_chat=chat_id)

    bot.answer_callback_query(call.id, f"সিলেক্ট করা হয়েছে: {title}")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            f"✅ চ্যানেল সফলভাবে সেট হয়েছে!\n\n"
            f"📌 Target Channel: {title}\n"
            f"🆔 ID: {chat_id}\n\n"
            f"এখন থেকে আপনার অটো পোস্ট এই চ্যানেলে যাবে।"
        ),
    )


@bot.message_handler(commands=["settime"])
def set_time(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) < 2 or not args[1].isdigit():
        bot.reply_to(
            message,
            "❌ ব্যবহার পদ্ধতি: /settime <minutes> (যেমন: /settime 60)",
        )
        return

    interval = int(args[1])
    update_user(user_id, interval_min=interval)
    bot.reply_to(
        message, f"⏱️ টাইম ইন্টারভাল সেট করা হয়েছে: {interval} মিনিট পর পর।"
    )


@bot.message_handler(commands=["startpost"])
def start_post(message):
    user_id = message.from_user.id
    u = get_user(user_id)

    if not u["target_chat"]:
        bot.reply_to(
            message,
            "❌ আপনি এখনও কোনো চ্যানেল সিলেক্ট করেননি!\n"
            "আপনার চ্যানেল থেকে ১টি পোস্ট এখানে ফরওয়ার্ড করুন অথবা /settarget"
            " @channel লিখুন।",
        )
        return

    update_user(user_id, is_active=1, last_post_time=0)
    bot.reply_to(
        message,
        "🚀 অটো পোস্ট চালু করা হয়েছে! সময় অনুযায়ী পোস্ট হওয়া শুরু হবে।",
    )


@bot.message_handler(commands=["stoppost"])
def stop_post(message):
    user_id = message.from_user.id
    update_user(user_id, is_active=0)
    bot.reply_to(message, "🛑 অটো পোস্ট বন্ধ করা হয়েছে।")


# --- ADMIN ONLY COMMANDS ---


@bot.message_handler(commands=["stats"])
def admin_stats(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
        active_users = cursor.fetchone()[0]

    bot.reply_to(
        message,
        f"📊 Admin Analytics\n\nTotal Users: {total_users}\nActive Auto Posters:"
        f" {active_users}",
    )


@bot.message_handler(commands=["broadcast"])
def admin_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return

    broadcast_msg = message.text.replace("/broadcast", "", 1).strip()
    if not broadcast_msg:
        bot.reply_to(message, "❌ ব্যবহার: /broadcast আপনার মেসেজ")
        return

    with db_lock:
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()

    count = 0
    for u in users:
        try:
            bot.send_message(u[0], broadcast_msg)
            count += 1
        except Exception:
            pass

    bot.reply_to(message, f"📢 মোট {count} জন ইউজারের কাছে মেসেজ পাঠানো হয়েছে!")


# --- BACKGROUND AUTO POSTER ENGINE (WITH AUTO-DELETE AFTER POSTING) ---


def auto_poster_loop():
    while True:
        try:
            time.sleep(15)
            current_time = time.time()

            with db_lock:
                cursor.execute(
                    "SELECT user_id, target_chat, interval_min, last_post_time"
                    " FROM users WHERE is_active = 1"
                )
                active_posters = cursor.fetchall()

            for user in active_posters:
                u_id, target, interval, last_time = user

                if current_time - last_time >= (interval * 60):
                    with db_lock:
                        cursor.execute(
                            "SELECT id, caption_text FROM captions WHERE"
                            " user_id = ? ORDER BY id ASC LIMIT 1",
                            (u_id,),
                        )
                        cap = cursor.fetchone()

                    if cap:
                        cap_id, post_text = cap

                        try:
                            # 1. Post caption to channel
                            bot.send_message(target, post_text)

                            # 2. Auto delete caption from Database after successfully posting
                            with db_lock:
                                cursor.execute(
                                    "DELETE FROM captions WHERE id = ?",
                                    (cap_id,),
                                )
                                conn.commit()

                            update_user(u_id, last_post_time=current_time)

                        except Exception as e:
                            print(f"Post failed for user {u_id}: {e}")
                    else:
                        # No captions left -> Automatically stop posting and notify user
                        update_user(u_id, is_active=0)
                        try:
                            bot.send_message(
                                u_id,
                                "⚠️ আপনার সেভ করা সকল ক্যাপশন পোস্ট করা শেষ হয়ে"
                                " গেছে! অটো-পোস্ট বন্ধ করা হলো।",
                            )
                        except Exception:
                            pass

        except Exception as err:
            print(f"Loop error: {err}")


threading.Thread(target=auto_poster_loop, daemon=True).start()

# --- BOT STARTUP ---
try:
    bot.remove_webhook()
except Exception as e:
    print(f"Webhook note: {e}")

print("Jisan Bot is Starting...")

while True:
    try:
        bot.infinity_polling(
            skip_pending=True,
            timeout=20,
            allowed_updates=telebot.util.update_types,
        )
    except ApiTelegramException as e:
        if e.error_code == 409:
            print("⚠️ Conflict 409! Waiting 10 seconds for old instance to close...")
            time.sleep(10)
        else:
            print(f"Telegram API Exception: {e}")
            time.sleep(5)
    except Exception as e:
        print(f"Polling Exception: {e}")
        time.sleep(5)
        
