import http.server
import os
import socketserver
import sqlite3
import threading
import time
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, RPCError


# --- RENDER PORT BINDING & UPTIMEROBOT FIX ---
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Jisan Userbot is Alive!")

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


# --- PYROGRAM USERBOT & DATABASE SETUP ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise ValueError("Error: API_ID, API_HASH, or SESSION_STRING Environment Variables are missing!")

app = Client(
    "jisan_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

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
    last_post_time REAL DEFAULT 0
)
"""
)

# Table to store full posts (Media + Caption/Text)
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    from_chat_id INTEGER,
    message_id INTEGER,
    preview_text TEXT
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

# Table for Tracking Sent Messages (To keep only 3 posts max)
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_id TEXT,
    message_id INTEGER
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
            "last_post_time": res[4],
        }


def update_user(user_id, **kwargs):
    with db_lock:
        for key, val in kwargs.items():
            cursor.execute(
                f"UPDATE users SET {key} = ? WHERE user_id = ?", (val, user_id)
            )
        conn.commit()


# --- COMMAND HANDLERS (CONTROLLED BY YOUR OWN ACCOUNT) ---


@app.on_message(filters.command(["start", "help"]) & filters.me)
async def send_welcome(client, message):
    user_id = message.from_user.id
    get_user(user_id)

    text = (
        "🏴‍☠️ Welcome to Jisan Userbot! 🩸\n\n"
        "👑 Session String এর মাধ্যমে আপনার নিজের আইডি থেকে মিডিয়া ও টেক্সটসহ অটো-পোস্ট সিস্টেম।\n\n"
        "⚡ Available Commands:\n"
        "• /addpost (ছবি, ভিডিও বা লেখার সাথে রিপ্লাই বা লিখে পাঠান) – কিউতে পোস্ট যোগ করুন\n"
        "• /myposts – সেভ করা পোস্টগুলো দেখুন ও ম্যানেজ করুন\n"
        "• /settarget – গ্রুপ বা চ্যানেল সেট করুন\n"
        "• /settime <minutes> – টাইম সেট করুন (মিনিটে)\n"
        "• /startpost – অটো পোস্ট চালু করুন\n"
        "• /stoppost – অটো পোস্ট বন্ধ করুন"
    )
    await message.reply_text(text)


@app.on_message(filters.command(["addpost"]) & filters.me)
async def add_post(client, message):
    user_id = message.from_user.id
    target_msg = message.reply_to_message or message

    preview = "Media Post"
    if target_msg.text:
        preview = target_msg.text[:30] + "..."
    elif target_msg.caption:
        preview = target_msg.caption[:30] + "..."
    elif target_msg.photo:
        preview = "📷 Photo Post"
    elif target_msg.video:
        preview = "📹 Video Post"
    elif target_msg.document:
        preview = "📁 Document Post"

    with db_lock:
        cursor.execute(
            "INSERT INTO posts (user_id, from_chat_id, message_id, preview_text) VALUES (?, ?, ?, ?)",
            (user_id, target_msg.chat.id, target_msg.id, preview),
        )
        conn.commit()

    await message.reply_text("✅ পোস্টটি সফলভাবে কিউতে সেভ করা হয়েছে!\nলিস্ট দেখতে /myposts লিখুন।")


# --- POST MANAGER ---
async def show_post_manager(client, chat_id, user_id, message_id=None):
    with db_lock:
        cursor.execute(
            "SELECT id, preview_text FROM posts WHERE user_id = ?", (user_id,)
        )
        rows = cursor.fetchall()

    if not rows:
        text = "⚠️ আপনার কোনো সেভ করা পোস্ট নেই!\nপোস্ট যোগ করতে কোনো মেসেজ বা ছবি/ভিডিওর সাথে /addpost লিখে পাঠান।"
        if message_id:
            try:
                await client.edit_message_text(chat_id, message_id, text)
            except Exception:
                await client.send_message(chat_id, text)
        else:
            await client.send_message(chat_id, text)
        return

    msg = "📋 আপনার সেভ করা সম্পূর্ণ পোস্টসমূহ:\n\n"
    markup_buttons = []

    for idx, row in enumerate(rows, start=1):
        post_id = row[0]
        msg += f"--- [ Serial: {idx} ] ---\n{row[1]}\n\n"
        markup_buttons.append([
            InlineKeyboardButton(f"🗑️ ডিলিট #{idx}", callback_data=f"del_post:{post_id}:{idx}")
        ])

    msg += "------------------------------------\n👇 নিচের বাটনে চাপ দিয়ে ডিলিট করতে পারেন:"
    markup = InlineKeyboardMarkup(markup_buttons)

    if message_id:
        try:
            await client.edit_message_text(chat_id, message_id, msg, reply_markup=markup)
        except Exception:
            await client.send_message(chat_id, msg, reply_markup=markup)
    else:
        await client.send_message(chat_id, msg, reply_markup=markup)


@app.on_message(filters.command(["myposts", "posts"]) & filters.me)
async def handle_post_manager(client, message):
    await show_post_manager(client, message.chat.id, message.from_user.id)


@app.on_callback_query(filters.regex(r"^del_post:"))
async def callback_delete_post(client, callback_query):
    parts = callback_query.data.split(":")
    post_id = parts[1]
    idx = parts[2]
    user_id = callback_query.from_user.id

    with db_lock:
        cursor.execute(
            "DELETE FROM posts WHERE id = ? AND user_id = ?",
            (post_id, user_id),
        )
        conn.commit()

    await callback_query.answer(f"✅ সিরিয়াল #{idx} ডিলিট করা হয়েছে!")
    await show_post_manager(
        client, callback_query.message.chat.id, user_id, message_id=callback_query.message.id
    )


# --- TARGET SETTER & CHANNEL SELECTOR ---
@app.on_message(filters.command(["settarget", "channels"]) & filters.me)
async def set_target(client, message):
    user_id = message.from_user.id
    args = (
        message.text.replace("/settarget", "")
        .replace("/channels", "")
        .strip()
    )

    if args:
        target = args
        try:
            chat_info = await client.get_chat(target)
            chat_id = str(chat_info.id)
            title = chat_info.title or target
            update_user(user_id, target_chat=chat_id)
            await message.reply_text(
                f"✅ গ্রুপ/চ্যানেল সফলভাবে সেট হয়েছে!\n\n📌 Target: {title}\n🆔 ID: {chat_id}"
            )
        except Exception:
            update_user(user_id, target_chat=target)
            await message.reply_text(f"🎯 টার্গেট সেট করা হয়েছে: {target}")
        return

    with db_lock:
        cursor.execute("SELECT chat_id, title FROM channels")
        rows = cursor.fetchall()

    markup_buttons = []
    if rows:
        for chat_id, title in rows:
            markup_buttons.append([
                InlineKeyboardButton(text=f"📢 {title}", callback_data=f"select_chat:{chat_id}:{title[:15]}")
            ])

    msg_text = "🎯 গ্রুপ বা চ্যানেল সেট করার নিয়ম:\n\nইউজারনেম বা আইডি দিয়ে: /settarget @GroupOrChannelUsername লিখুন।"
    markup = InlineKeyboardMarkup(markup_buttons) if markup_buttons else None
    await message.reply_text(msg_text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^select_chat:"))
async def callback_select_chat(client, callback_query):
    data_parts = callback_query.data.split(":", 2)
    chat_id = data_parts[1]
    title = data_parts[2]
    user_id = callback_query.from_user.id

    update_user(user_id, target_chat=chat_id)

    await callback_query.answer(f"সিলেক্ট করা হয়েছে: {title}")
    await callback_query.message.edit_text(
        f"✅ গ্রুপ/চ্যানেল সফলভাবে সেট হয়েছে!\n\n📌 Target: {title}\n🆔 ID: {chat_id}\n\nএখন থেকে আপনার আইডি থেকে অটো পোস্ট এখানে যাবে।"
    )


@app.on_message(filters.command(["settime"]) & filters.me)
async def set_time(client, message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) < 2 or not args[1].isdigit():
        await message.reply_text("❌ ব্যবহার পদ্ধতি: /settime <minutes> (যেমন: /settime 60)")
        return

    interval = int(args[1])
    update_user(user_id, interval_min=interval)
    await message.reply_text(f"⏱️ টাইম ইন্টারভাল সেট করা হয়েছে: {interval} মিনিট পর পর।")


@app.on_message(filters.command(["startpost"]) & filters.me)
async def start_post(client, message):
    user_id = message.from_user.id
    u = get_user(user_id)

    if not u["target_chat"]:
        await message.reply_text("❌ আপনি এখনও কোনো গ্রুপ বা চ্যানেল সেট করেননি!\n/settarget ব্যবহার করুন।")
        return

    update_user(user_id, is_active=1, last_post_time=0)
    await message.reply_text("🚀 আপনার আইডি থেকে অটো পোস্ট চালু করা হয়েছে! অফলাইনেও সময় অনুযায়ী পোস্ট হবে।")


@app.on_message(filters.command(["stoppost"]) & filters.me)
async def stop_post(client, message):
    user_id = message.from_user.id
    update_user(user_id, is_active=0)
    await message.reply_text("🛑 অটো পোস্ট বন্ধ করা হয়েছে।")


# --- BACKGROUND AUTO POSTER ENGINE (POSTING FROM YOUR ID WITH 3-MESSAGE LIMIT) ---


async def auto_poster_loop():
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.sleep(15)
            current_time = time.time()

            with db_lock:
                cursor.execute(
                    "SELECT user_id, target_chat, interval_min, last_post_time FROM users WHERE is_active = 1"
                )
                active_posters = cursor.fetchall()

            for user in active_posters:
                u_id, target, interval, last_time = user

                if current_time - last_time >= (interval * 60):
                    with db_lock:
                        cursor.execute(
                            "SELECT id, from_chat_id, message_id FROM posts WHERE user_id = ? ORDER BY id ASC LIMIT 1",
                            (u_id,),
                        )
                        post_row = cursor.fetchone()

                    if post_row:
                        post_id, from_chat_id, source_msg_id = post_row

                        try:
                            chat_target = target
                            if str(target).lstrip('-').isdigit():
                                chat_target = int(target)

                            # 1. Copy post directly from YOUR account using Session String
                            sent_msg = await client.copy_message(
                                chat_id=chat_target,
                                from_chat_id=from_chat_id,
                                message_id=source_msg_id
                            )

                            # 2. Keep only 3 messages rule (4th message deletes 1st)
                            with db_lock:
                                cursor.execute(
                                    "INSERT INTO sent_messages (user_id, chat_id, message_id) VALUES (?, ?, ?)",
                                    (u_id, str(target), sent_msg.id),
                                )
                                conn.commit()

                                cursor.execute(
                                    "SELECT id, message_id FROM sent_messages WHERE user_id = ? AND chat_id = ? ORDER BY id ASC",
                                    (u_id, str(target)),
                                )
                                history = cursor.fetchall()

                            if len(history) > 3:
                                for old_msg in history[:-3]:
                                    db_id, old_msg_id = old_msg
                                    try:
                                        await client.delete_messages(chat_target, old_msg_id)
                                    except Exception:
                                        pass
                                    with db_lock:
                                        cursor.execute(
                                            "DELETE FROM sent_messages WHERE id = ?",
                                            (db_id,),
                                        )
                                        conn.commit()

                            # 3. Delete posted item from queue so it won't repeat
                            with db_lock:
                                cursor.execute(
                                    "DELETE FROM posts WHERE id = ?",
                                    (post_id,),
                                )
                                conn.commit()

                            update_user(u_id, last_post_time=current_time)

                        except FloodWait as e:
                            print(f"⚠️ FloodWait: Sleeping for {e.value} seconds...")
                            await asyncio.sleep(e.value)
                        except RPCError as e:
                            print(f"RPC Error for user {u_id}: {e}")
                            if "CHAT_WRITE_FORBIDDEN" in str(e) or "USER_NOT_PARTICIPANT" in str(e) or "PEER_ID_INVALID" in str(e):
                                update_user(u_id, is_active=0)
                                try:
                                    await client.send_message(
                                        u_id,
                                        f"⚠️ গ্রুপ/চ্যানেল ({target}) এ মেসেজ পাঠাতে বাধা দেওয়া হয়েছে ({e})!\n\nওই গ্রুপের জন্য অটো-পোস্ট বন্ধ করা হলো।"
                                    )
                                except Exception:
                                    pass
                            else:
                                update_user(u_id, last_post_time=current_time)
                        except Exception as e:
                            print(f"Post failed for user {u_id}: {e}")
                            update_user(u_id, last_post_time=current_time)
                    else:
                        update_user(u_id, is_active=0)
                        try:
                            await client.send_message(
                                u_id,
                                "⚠️ আপনার সেভ করা সকল পোস্ট পাঠানো শেষ হয়ে গেছে! অটো-পোস্ট বন্ধ করা হলো।"
                            )
                        except Exception:
                            pass

        except Exception as err:
            print(f"Loop error: {err}")


# --- STARTUP RUNNER ---
async def main():
    async with app:
        print("Jisan Userbot is Starting with Session String & Post Queue...")
        asyncio.create_task(auto_poster_loop())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
        
