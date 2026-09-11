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
        "• /mycaptions – সেভ করা ক্যাপশন দেখুন\n"
        "• /editcaption <id> <text> – ক্যাপশন এডিট করুন\n"
        "• /deletecaption <id> – ক্যাপশন ডিলিট করুন\n"
        "• /settarget @channel – চ্যানেল/গ্রুপ সেট করুন\n"
        "• /settime <minutes> – টাইম সেট করুন (মিনিটে)\n"
        "• /startpost – অটো পোস্ট চালু করুন\n"
        "• /stoppost – অটো পোস্ট বন্ধ করুন\n"
    )

    if user_id == ADMIN_ID:
        text += (
            "• /stats – বট স্ট্যাটাস দেখুন (Admin)\n"
            "• /broadcast <msg> – সবাইকে মেসেজ পাঠান (Admin)\n"
        )

    text += "\n🎯 Send /addcaption first to set your auto post sequence!"
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

    bot.reply_to(message, "✅ ক্যাপশন সফলভাবে সেভ হয়েছে!")


@bot.message_handler(commands=["mycaptions"])
def list_captions(message):
    user_id = message.from_user.id
    with db_lock:
        cursor.execute(
            "SELECT id, caption_text FROM captions WHERE user_id = ?",
            (user_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        bot.reply_to(message, "⚠️ আপনার কোনো সেভ করা ক্যাপশন নেই।")
        return

    msg = "📋 আপনার সেভ করা সম্পূর্ণ ক্যাপশনসমূহ:\n\n"
    for idx, row in enumerate(rows, start=1):
        msg += f"--- [ Serial: {idx} | ID: {row[0]} ] ---\n{row[1]}\n\n"

    msg += (
        "------------------------------------\n"
        "এডিট করতে: /editcaption <ID> <New Text>\n"
        "ডিলিট করতে: /deletecaption <ID>"
    )
    bot.reply_to(message, msg)


@bot.message_handler(commands=["editcaption"])
def edit_caption(message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=2)

    if len(args) < 3:
        bot.reply_to(message, "❌ ব্যবহার পদ্ধতি: /editcaption <ID> <নতুন ক্যাপশন>")
        return

    cap_id, new_text = args[1], args[2]

    with db_lock:
        cursor.execute(
            "UPDATE captions SET caption_text = ? WHERE id = ? AND user_id = ?",
            (new_text, cap_id, user_id),
        )
        conn.commit()
        updated = cursor.rowcount

    if updated > 0:
        bot.reply_to(message, f"✅ ID {cap_id} সফলভাবে আপডেট করা হয়েছে!")
    else:
        bot.reply_to(message, "❌ ক্যাপশন খুঁজে পাওয়া যায়নি বা এটি আপনার নয়।")


@bot.message_handler(commands=["deletecaption"])
def delete_caption(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) < 2:
        bot.reply_to(message, "❌ ব্যবহার পদ্ধতি: /deletecaption <ID>")
        return

    cap_id = args[1]
    with db_lock:
        cursor.execute(
            "DELETE FROM captions WHERE id = ? AND user_id = ?",
            (cap_id, user_id),
        )
        conn.commit()
        deleted = cursor.rowcount

    if deleted > 0:
        bot.reply_to(message, f"🗑️ ID {cap_id} সফলভাবে ডিলিট করা হয়েছে!")
    else:
        bot.reply_to(message, "❌ ক্যাপশন খুঁজে পাওয়া যায়নি।")


@bot.message_handler(commands=["settarget"])
def set_target(message):
    user_id = message.from_user.id
    target = message.text.replace("/settarget", "", 1).strip()

    if not target:
        bot.reply_to(
            message, "❌ ব্যবহার পদ্ধতি: /settarget @channelusername অথবা ID"
        )
        return

    update_user(user_id, target_chat=target)
    bot.reply_to(
        message,
        f"🎯 টার্গেট সেট করা হয়েছে: {target}\n\n*(মনে রাখবেন, বটকে ওই চ্যানেল/গ্রুপে Admin বানাতে হবে)*",
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
            message, "❌ আগে চ্যানেল সেট করুন। যেমন: /settarget @yourchannel"
        )
        return

    update_user(user_id, is_active=1, last_post_time=0)
    bot.reply_to(
        message,
        "🚀 অটো পোস্ট চালু করা হয়েছে! সময় অনুযায়ী পোস্ট হওয়া শুরু হবে।",
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
        f"📊 Admin Analytics\n\nTotal Users: {total_users}\nActive Auto Posters: {active_users}",
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


# --- BACKGROUND AUTO POSTER ENGINE ---


def auto_poster_loop():
    while True:
        try:
            time.sleep(15)
            current_time = time.time()

            with db_lock:
                cursor.execute(
                    "SELECT user_id, target_chat, interval_min, current_index,"
                    " last_post_time FROM users WHERE is_active = 1"
                )
                active_posters = cursor.fetchall()

            for user in active_posters:
                u_id, target, interval, curr_idx, last_time = user

                if current_time - last_time >= (interval * 60):
                    with db_lock:
                        cursor.execute(
                            "SELECT caption_text FROM captions WHERE user_id ="
                            " ? ORDER BY id ASC",
                            (u_id,),
                        )
                        caps = cursor.fetchall()

                    if caps:
                        next_idx = curr_idx % len(caps)
                        post_text = caps[next_idx][0]

                        try:
                            bot.send_message(target, post_text)
                            update_user(
                                u_id,
                                current_index=(next_idx + 1),
                                last_post_time=current_time,
                            )
                        except Exception as e:
                            print(f"Post failed for user {u_id}: {e}")

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
        bot.infinity_polling(skip_pending=True, timeout=20)
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
