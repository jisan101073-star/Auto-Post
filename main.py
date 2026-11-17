# -*- coding: utf-8 -*-
"""
JisanV2 Post Management Bot  (multi-account edition)
- Telethon controller bot + many userbot accounts
- Accounts are added from inside the bot; sessions are ENCRYPTED and
  stored in Firebase Realtime Database
- Premium inline-button panel (all old commands still work)
"""
import os
import sys
import json
import time
import random
import base64
import hashlib
import asyncio
import logging
import functools
from collections import deque

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import InputPeerUser, MessageMediaWebPage

# The command-menu feature is optional: a Telethon version difference must
# never stop the whole bot from starting.
try:
    from telethon.tl.functions.bots import SetBotCommandsRequest
    from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopePeer
    MENU_OK = True
except ImportError:
    MENU_OK = False
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("jisanv2")


# ------------------------------------------------------------------
# Environment variables
# ------------------------------------------------------------------
def require_env(name, cast=str):
    raw = os.environ.get(name)
    if not raw:
        logger.critical("Missing environment variable: %s", name)
        sys.exit(1)
    try:
        return cast(raw)
    except ValueError:
        logger.critical("Invalid value for environment variable: %s", name)
        sys.exit(1)


API_ID = require_env("API_ID", int)
API_HASH = require_env("API_HASH")
BOT_TOKEN = require_env("BOT_TOKEN")
ADMIN_ID = require_env("ADMIN_ID", int)

# Optional: the old single account keeps working as the "Main" account
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "20") or 20)


# ------------------------------------------------------------------
# Local JSON helpers (fallback when Firebase is not configured)
# ------------------------------------------------------------------
GROUPS_FILE = "selected_groups.json"   # legacy: Main account groups
SETTINGS_FILE = "settings.json"
AUTOPOST_FILE = "autopost_ref.json"
PAYLOAD_DIR = "payload"


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to read %s: %s", path, e)
    return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.error("Failed to save %s: %s", path, e)


# ------------------------------------------------------------------
# Firebase Realtime Database
# ------------------------------------------------------------------
class Store:
    """Thin async wrapper around firebase-admin (blocking calls run in threads)."""

    def __init__(self):
        self.ok = False
        self.db = None
        self.last_error = ""

    def init(self):
        creds_raw = (os.environ.get("FIREBASE_CREDENTIALS")
                     or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
        db_url = (os.environ.get("FIREBASE_DB_URL")
                  or os.environ.get("FIREBASE_DATABASE_URL") or "").strip()
        if not creds_raw or not db_url:
            logger.warning("Firebase not configured (need FIREBASE_SERVICE_ACCOUNT_JSON / "
                           "FIREBASE_DATABASE_URL). Using local files; multi-account is disabled.")
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, db

            cred = credentials.Certificate(json.loads(creds_raw))
            firebase_admin.initialize_app(cred, {"databaseURL": db_url})
            self.db = db
            self.ok = True
            logger.info("Firebase connected.")
        except Exception as e:
            self.last_error = type(e).__name__
            logger.error("Firebase init failed: %s", type(e).__name__)

    async def _run(self, fn):
        try:
            result = await asyncio.to_thread(fn)
            self.last_error = ""
            return result
        except Exception as e:
            self.last_error = type(e).__name__
            logger.error("Firebase error: %s", type(e).__name__)
            raise

    async def get(self, path):
        return await self._run(lambda: self.db.reference(path).get())

    async def set(self, path, value):
        return await self._run(lambda: self.db.reference(path).set(value))

    async def update(self, path, value):
        return await self._run(lambda: self.db.reference(path).update(value))

    async def delete(self, path):
        return await self._run(lambda: self.db.reference(path).delete())


store = Store()


async def kv_get(path, local_file):
    try:
        if store.ok:
            return await store.get(path)
    except Exception:
        return None
    return load_json(local_file, None)


async def kv_set(path, local_file, value):
    try:
        if store.ok:
            await store.set(path, value)
        else:
            save_json(local_file, value)
    except Exception:
        pass  # already logged by Store


# ------------------------------------------------------------------
# Encryption for session strings
# ------------------------------------------------------------------
def make_fernet():
    key = os.environ.get("ENCRYPTION_KEY", "")
    if len(key) < 16:
        return None
    dk = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), b"jisanv2-sessions", 200_000)
    return Fernet(base64.urlsafe_b64encode(dk))


fernet = make_fernet()


def accounts_ready():
    """Multi-account needs Firebase AND an encryption key."""
    return store.ok and fernet is not None


# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------
START_TIME = time.time()
COOLDOWN_TIME = 3600          # manual-post cooldown, per account
GROUPS_PER_PAGE = 8
GROUP_CACHE_TTL = 60
MIN_INTERVAL_H = 1
MAX_INTERVAL_H = 168

admin_hash = None  # lets the bot reach the admin after a redeploy
notify_enabled = True
auto_delete_enabled = True

waiting_session_users = set()
bot_users = set()
wiz = {}                     # the admin's current wizard state (single admin)

last_sent_messages = {}      # {(acc_id, group_id): [msg_ids]}
groups_cache = {}            # {acc_id: {"time": t, "items": [...]}}
activity = deque(maxlen=25)  # recent events: (timestamp, text)
jobs = {}                    # job_id -> Job
JOBS_FILE = "jobs.json"


class Account:
    def __init__(self, acc_id, name, session, enabled=True, groups=None,
                 is_main=False, user_id=None):
        self.id = acc_id
        self.name = name or "Account"
        self.session = session
        self.enabled = enabled
        self.groups = set(groups or [])
        self.is_main = is_main
        self.user_id = user_id
        self.client = None
        self.connected = False
        self.error = ""
        self.last_post = 0

    @property
    def label(self):
        return ("⭐ " if self.is_main else "") + self.name

    def icon(self):
        if not self.enabled:
            return "⏸"
        return "🟢" if self.connected else "🔴"

    def record(self, include_session=False):
        rec = {
            "name": self.name,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "groups": sorted(self.groups),
        }
        if include_session and not self.is_main and self.session and fernet:
            rec["session_enc"] = fernet.encrypt(self.session.encode("utf-8")).decode("utf-8")
        return rec


accounts = {}  # acc_id -> Account

bot = TelegramClient("bot_session", API_ID, API_HASH)


def admin_peer():
    if admin_hash:
        return InputPeerUser(ADMIN_ID, admin_hash)
    return None


def total_targets():
    return sum(len(a.groups) for a in accounts.values() if a.enabled)


def active_accounts():
    return [a for a in accounts.values() if a.enabled and a.connected]


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------
async def save_settings():
    await kv_set("settings", SETTINGS_FILE, {
        "auto_delete": auto_delete_enabled,
        "notify": notify_enabled,
        "admin_hash": admin_hash,
    })


async def persist_account(acc):
    try:
        if store.ok:
            await store.update(f"accounts/{acc.id}", acc.record())
        elif acc.is_main:
            save_json(GROUPS_FILE, sorted(acc.groups))
    except Exception:
        pass


def display_name(me):
    name = (getattr(me, "first_name", None) or getattr(me, "username", None) or "Account")
    return clean_name(name)[:30]


async def connect_account(acc):
    if not acc.session:
        acc.connected = False
        acc.error = "Session decrypt failed (ENCRYPTION_KEY?)"
        return False
    try:
        if acc.client is None:
            acc.client = TelegramClient(StringSession(acc.session), API_ID, API_HASH)
        await asyncio.wait_for(acc.client.connect(), 30)
        if not await acc.client.is_user_authorized():
            acc.connected = False
            acc.error = "Session expired / invalid"
            return False
        me = await acc.client.get_me()
        acc.user_id = me.id
        if acc.is_main or not acc.name or acc.name == "Account":
            acc.name = display_name(me)
        acc.connected = True
        acc.error = ""
        return True
    except Exception as e:
        acc.connected = False
        acc.error = type(e).__name__
        logger.error("Account %s connect failed: %s", acc.id, type(e).__name__)
        return False


async def safe_disconnect(acc):
    try:
        if acc.client:
            await acc.client.disconnect()
    except Exception:
        pass
    acc.connected = False


def new_account_id():
    base = f"a{int(time.time())}"
    acc_id, n = base, 1
    while acc_id in accounts:
        n += 1
        acc_id = f"{base}{n}"
    return acc_id


async def add_account(session_text):
    """Validate + store a new session. Returns (ok, message, account)."""
    if not accounts_ready():
        return False, "Firebase ও ENCRYPTION_KEY সেট করা নেই।", None
    if len(accounts) >= MAX_ACCOUNTS:
        return False, f"সর্বোচ্চ {MAX_ACCOUNTS}টি অ্যাকাউন্ট যোগ করা যাবে।", None
    try:
        StringSession(session_text)
    except Exception:
        return False, "এটি সঠিক Telethon session string নয়।", None

    acc = Account(new_account_id(), "", session_text)
    if not await connect_account(acc):
        reason = acc.error
        await safe_disconnect(acc)
        return False, f"Session কাজ করছে না ({reason})।", None

    if any(a.user_id == acc.user_id for a in accounts.values()):
        await safe_disconnect(acc)
        return False, "এই অ্যাকাউন্টটি আগেই যোগ করা আছে।", None

    acc.record_added = int(time.time())
    try:
        rec = acc.record(include_session=True)
        rec["added_at"] = acc.record_added
        await store.set(f"accounts/{acc.id}", rec)
    except Exception:
        await safe_disconnect(acc)
        return False, "Firebase-এ সেভ করা যায়নি।", None

    accounts[acc.id] = acc
    return True, "ok", acc


async def remove_account(acc):
    await safe_disconnect(acc)
    accounts.pop(acc.id, None)
    groups_cache.pop(acc.id, None)
    for j in jobs.values():
        j.accounts.discard(acc.id)
    await save_jobs()
    if store.ok:
        try:
            await store.delete(f"accounts/{acc.id}")
        except Exception:
            return False
    return True


async def load_state():
    """Load settings, accounts and jobs at startup."""
    global auto_delete_enabled, notify_enabled, admin_hash

    s = await kv_get("settings", SETTINGS_FILE) or {}
    if isinstance(s.get("auto_delete"), bool):
        auto_delete_enabled = s["auto_delete"]
    if isinstance(s.get("notify"), bool):
        notify_enabled = s["notify"]
    if isinstance(s.get("admin_hash"), int):
        admin_hash = s["admin_hash"]

    data = {}
    if store.ok:
        try:
            data = await store.get("accounts") or {}
        except Exception:
            data = {}

    legacy_groups = load_json(GROUPS_FILE, [])

    if SESSION_STRING:
        main = Account("main", "Main", SESSION_STRING, is_main=True)
        rec = data.get("main") or {}
        main.enabled = rec.get("enabled", True)
        if "groups" in rec:
            main.groups = set(rec.get("groups") or [])
        else:
            main.groups = set(legacy_groups)
        accounts["main"] = main
        if store.ok and "main" not in data:
            await persist_account(main)  # one-time migration of old groups

    for acc_id, rec in data.items():
        if acc_id == "main" or not isinstance(rec, dict):
            continue
        session = ""
        token = rec.get("session_enc")
        if token and fernet:
            try:
                session = fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except InvalidToken:
                logger.error("Cannot decrypt session of %s (wrong ENCRYPTION_KEY?)", acc_id)
        accounts[acc_id] = Account(
            acc_id, rec.get("name"), session,
            enabled=bool(rec.get("enabled", True)),
            groups=rec.get("groups") or [],
            user_id=rec.get("user_id"),
        )

    if accounts:
        await asyncio.gather(*(connect_account(a) for a in accounts.values()))
        logger.info("Accounts loaded: %d (connected: %d)", len(accounts), len(active_accounts()))

    # Jobs
    raw = await kv_get("jobs", JOBS_FILE) or {}
    for job_id, rec in raw.items():
        if isinstance(rec, dict):
            jobs[job_id] = Job.from_record(job_id, rec)

    # One-time migration of the old single Auto-Post into the "default" job
    if not jobs:
        ref = await kv_get("autopost", AUTOPOST_FILE)
        if isinstance(ref, dict) and ref.get("msg_id"):
            old_interval = s.get("interval") if isinstance(s.get("interval"), int) else 3600
            jobs["default"] = Job("default", "Default · সব অ্যাকাউন্ট", "all",
                                  interval=max(old_interval, 3600), msg_id=ref.get("msg_id"),
                                  running=bool(s.get("auto_running")))
            await save_jobs()
            logger.info("Old Auto-Post migrated to the default job.")

    for job in jobs.values():
        await restore_job_content(job)

    for job in list(jobs.values()):
        if job.running:
            job.running = False  # start_job sets it again if it can really start
            ok, msg = await start_job(job)
            logger.info("Job %s resumed: %s", job.id, ok)
            if not ok:
                await save_jobs()


# ------------------------------------------------------------------
# Render keep-alive web server
# ------------------------------------------------------------------
async def handle(request):
    return web.Response(text="JisanV2 Engine is Alive!")


async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Dummy Web Server running on port %s", port)


# ------------------------------------------------------------------
# Design helpers
# ------------------------------------------------------------------
DIV = "━━━━━━━━━━━━━━━━━━"

DENIED_TEXT = (
    "🔒 **Access Denied**\n"
    f"{DIV}\n"
    "এই কমান্ড শুধু **অ্যাডমিন** ব্যবহার করতে পারবে।"
)

PUBLIC_TEXT = (
    "✨ **JISAN V2**\n"
    "__Post Management Panel__\n"
    f"{DIV}\n"
    "🔒 এটি একটি প্রাইভেট বট। শুধু অ্যাডমিন এটি চালাতে পারে।"
)


def clean_name(name):
    name = (name or "Admin").strip()
    for ch in ("*", "_", "`", "[", "]"):
        name = name.replace(ch, "")
    return name or "Admin"


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} দিন")
    if h:
        parts.append(f"{h} ঘণ্টা")
    if m and not d:
        parts.append(f"{m} মিনিট")
    if not parts:
        parts.append(f"{s} সেকেন্ড")
    return " ".join(parts)


def log_activity(text):
    activity.appendleft((time.time(), text))


def onoff(flag):
    return "🟢 ON" if flag else "🔴 OFF"


def db_status_text():
    if not store.ok:
        return "🟡 Firebase নেই (লোকাল ফাইল)"
    if store.last_error:
        return f"🔴 সমস্যা ({store.last_error})"
    return "🟢 Firebase সংযুক্ত"


def nav_row(back=None):
    row = []
    if back:
        row.append(Button.inline("⬅️ Back", back))
    row.append(Button.inline("🏠 Home", "menu_home"))
    return row


async def show(event, text, buttons=None):
    """Edit the message for button presses, reply for commands."""
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
    else:
        await event.reply(text, buttons=buttons)


# ------------------------------------------------------------------
# Screens (each returns (text, buttons))
# ------------------------------------------------------------------
HOURS_PRESETS = (1, 2, 3, 6, 12, 24)


def valid_hours(h):
    return MIN_INTERVAL_H <= h <= MAX_INTERVAL_H


def fmt_interval(seconds):
    return f"{int(seconds) // 3600} ঘণ্টা"


def running_jobs():
    return [j for j in jobs.values() if j.running]


def cooldown_left(acc):
    if not acc.last_post:
        return 0
    return max(0, COOLDOWN_TIME - (time.time() - acc.last_post))


def home_screen(name):
    text = (
        "✨ **JISAN V2** ✨\n"
        "__Post Management Panel__\n"
        f"{DIV}\n"
        f"👋 স্বাগতম, **{clean_name(name)}**!\n\n"
        "🟢 বট: **Online**\n"
        f"👥 অ্যাকাউন্ট: `{len(accounts)}` (🟢 সক্রিয় `{len(active_accounts())}`)\n"
        f"🎯 টার্গেট গ্রুপ: `{total_targets()}` টি\n"
        f"🔁 Auto-Post: `{len(running_jobs())}` চালু / `{len(jobs)}` টি\n"
        f"{DIV}\n"
        "নিচের মেনু থেকে বেছে নাও 👇"
    )
    buttons = [
        [Button.inline("📊 Dashboard", "menu_dash"), Button.inline("🎯 Groups", "menu_groups")],
        [Button.inline("📨 Manual Post", "menu_post"), Button.inline("🔁 Auto-Post", "menu_auto")],
        [Button.inline("👥 Accounts", "menu_accounts"), Button.inline("⚙️ Settings", "menu_settings")],
        [Button.inline("❓ Help", "menu_help")],
    ]
    return text, buttons


def dashboard_screen():
    last = max([a.last_post for a in accounts.values()] or [0])
    last_txt = f"`{fmt_duration(time.time() - last)}` আগে" if last else "`কোনো পোস্ট হয়নি`"
    cooling = sum(1 for a in accounts.values() if a.enabled and cooldown_left(a) > 0)

    nxt = [j.next_run for j in running_jobs() if j.next_run]
    next_txt = f"`{fmt_duration(max(0, min(nxt) - time.time()))}` পরে" if nxt else "—"

    text = (
        "📊 **Dashboard**\n"
        f"{DIV}\n"
        "🤖 **বট**\n"
        "• স্ট্যাটাস: 🟢 Online\n"
        f"• আপটাইম: `{fmt_duration(time.time() - START_TIME)}`\n"
        f"• ইউজার: `{len(bot_users)}`\n\n"
        "👥 **অ্যাকাউন্ট**\n"
        f"• মোট: `{len(accounts)}` · সক্রিয়: `{len(active_accounts())}`\n"
        f"• টার্গেট গ্রুপ (সক্রিয়): `{total_targets()}` টি\n\n"
        "🔁 **Auto-Post**\n"
        f"• চালু জব: `{len(running_jobs())}` / `{len(jobs)}`\n"
        f"• পরবর্তী রান: {next_txt}\n"
        f"• Auto-Delete: {onoff(auto_delete_enabled)}\n\n"
        "📨 **ম্যানুয়াল পোস্ট**\n"
        f"• সর্বশেষ পোস্ট: {last_txt}\n"
        f"• কুলডাউনে থাকা অ্যাকাউন্ট: `{cooling}`\n\n"
        "🗄 **ডাটাবেস**\n"
        f"• {db_status_text()}"
    )
    buttons = [
        [Button.inline("🔄 Refresh", "menu_dash"), Button.inline("📜 Activity", "menu_activity")],
        nav_row(),
    ]
    return text, buttons


def activity_screen():
    lines = ["📜 **Recent Activity**", DIV]
    if not activity:
        lines.append("এখনো কোনো অ্যাক্টিভিটি নেই।")
    else:
        now = time.time()
        for ts, txt in list(activity)[:15]:
            lines.append(f"• `{fmt_duration(now - ts)}` আগে — {txt}")
    return "\n".join(lines), [[Button.inline("🔄 Refresh", "menu_activity")], nav_row("menu_dash")]


# ---- Auto-Post (jobs) -----------------------------------------------
def jobs_screen():
    lines = [
        "🔁 **Auto-Post**",
        DIV,
        f"• মোট জব: `{len(jobs)}` · 🟢 চালু: `{len(running_jobs())}`",
        "",
    ]
    if not jobs:
        lines.append("এখনো কোনো জব নেই। ➕ চেপে প্রথমটা বানাও।")
    else:
        lines.append("প্রতিটা জবের নিজের টার্গেট, কন্টেন্ট ও সময় আছে 👇")
    buttons = []
    for job in jobs.values():
        buttons.append([Button.inline(
            f"{job.icon()} {job.name[:20]} · {fmt_interval(job.interval)}", f"job:{job.id}")])
    row = [Button.inline("➕ নতুন Auto-Post", "job_new")]
    if running_jobs():
        row.append(Button.inline("🛑 সব বন্ধ", "jobs_stop_all"))
    buttons.append(row)
    buttons.append(nav_row())
    return "\n".join(lines), buttons


def job_detail_screen(job, created=False):
    last = "`এখনো হয়নি`"
    if job.last_run:
        ok, fail = job.last_result
        last = f"`{fmt_duration(time.time() - job.last_run)}` আগে · ✅ {ok} ❌ {fail}"
    if job.running and job.next_run:
        nxt = f"`{fmt_duration(max(0, job.next_run - time.time()))}` পরে"
    else:
        nxt = "—"
    head = "✅ **Auto-Post জব তৈরি হয়েছে!**\n\n" if created else ""
    text = (
        f"{head}🔁 **{job.name}**\n"
        f"{DIV}\n"
        f"• স্ট্যাটাস: {'🟢 চালু' if job.running else '🔴 বন্ধ'}\n"
        f"• টার্গেট: {job.target_text()}\n"
        f"• গ্রুপ: `{job.groups_count()}` টি\n"
        f"• ব্যবধান: `{fmt_interval(job.interval)}`\n"
        f"• কন্টেন্ট: {'✅ সেভ আছে' if job.payload else '❌ নেই'}\n"
        f"• শেষ রান: {last}\n"
        f"• পরবর্তী পোস্ট: {nxt}"
    )
    main_btn = (Button.inline("🛑 বন্ধ করুন", f"job_stop:{job.id}") if job.running
                else Button.inline("🚀 চালু করুন", f"job_start:{job.id}"))
    buttons = [
        [main_btn],
        [Button.inline("📝 কন্টেন্ট", f"job_content:{job.id}"), Button.inline("⏱ সময়", f"job_time:{job.id}")],
        [Button.inline("🗑 মুছে ফেলুন", f"job_del:{job.id}")],
        nav_row("menu_auto"),
    ]
    return text, buttons


def job_delete_screen(job):
    text = (
        "🗑 **জব মুছবে?**\n"
        f"{DIV}\n"
        f"**{job.name}** মুছে যাবে। আগে পাঠানো পোস্টে কিছু হবে না।"
    )
    buttons = [[Button.inline("✅ হ্যাঁ, মুছুন", f"job_del_yes:{job.id}"),
                Button.inline("↩️ না", f"job:{job.id}")]]
    return text, buttons


def interval_screen(title, prefix, custom_cb, back=None, current=None):
    cur = f"এখনকার ব্যবধান: `{fmt_interval(current)}`\n\n" if current else ""
    text = (
        f"⏱ **{title}**\n"
        f"{DIV}\n"
        f"{cur}"
        "কত ঘণ্টা পরপর পোস্ট হবে? 👇"
    )
    rows = []
    for i in range(0, len(HOURS_PRESETS), 3):
        rows.append([Button.inline(f"{h} ঘণ্টা", f"{prefix}:{h}") for h in HOURS_PRESETS[i:i + 3]])
    rows.append([Button.inline("✏️ অন্য সময়", custom_cb)])
    rows.append(nav_row(back))
    return text, rows


def interval_custom_prompt_screen():
    text = (
        "✏️ **অন্য সময়**\n"
        f"{DIV}\n"
        f"ঘণ্টার সংখ্যাটি লিখে পাঠাও ({MIN_INTERVAL_H} থেকে {MAX_INTERVAL_H})।\n"
        "যেমন: `5`"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


# ---- Post wizard (manual + auto) -------------------------------------
def wiz_title():
    return "📨 **Manual Post**" if wiz.get("purpose") == "man" else "➕ **নতুন Auto-Post**"


def wiz_targets():
    if wiz.get("mode") == "all":
        return [a for a in accounts.values() if a.enabled]
    return [accounts[i] for i in wiz.get("accts", ()) if i in accounts and accounts[i].enabled]


def wiz_target_text():
    targets = wiz_targets()
    if wiz.get("mode") == "all":
        return f"👥 সব অ্যাকাউন্ট ({len(targets)})"
    if len(targets) == 1:
        return f"👤 {targets[0].label}"
    return f"🎛 {len(targets)}টি অ্যাকাউন্ট"


def wiz_mode_screen():
    enabled = [a for a in accounts.values() if a.enabled]
    if not enabled:
        text = (
            f"{wiz_title()}\n{DIV}\n"
            "⚠️ কোনো সক্রিয় অ্যাকাউন্ট নেই। আগে 👥 **Accounts** থেকে অ্যাকাউন্ট যোগ বা চালু করো।"
        )
        return text, [[Button.inline("👥 Accounts", "menu_accounts")], nav_row()]
    text = (
        f"{wiz_title()}\n"
        f"{DIV}\n"
        "কোন অ্যাকাউন্ট থেকে পোস্ট হবে? বেছে নাও 👇\n\n"
        "👤 **একটি** — শুধু একটা অ্যাকাউন্ট\n"
        "👥 **সব** — সক্রিয় সবগুলো একসাথে\n"
        "🎛 **পছন্দমতো** — যেগুলো ইচ্ছা"
    )
    buttons = [
        [Button.inline("👤 একটি অ্যাকাউন্ট", "w_mode:single")],
        [Button.inline(f"👥 সব অ্যাকাউন্ট ({len(enabled)})", "w_mode:all")],
        [Button.inline("🎛 পছন্দমতো বাছাই", "w_mode:custom")],
        nav_row(),
    ]
    return text, buttons


def wiz_pick_screen():
    custom = wiz.get("mode") == "custom"
    chosen = wiz.setdefault("accts", set())
    if custom:
        head = "🎛 **অ্যাকাউন্ট বাছাই**\n" + DIV + f"\n✅ বাছা হয়েছে: `{len(chosen)}`\nচাপ দিয়ে চালু/বন্ধ করো 👇"
    else:
        head = "👤 **অ্যাকাউন্ট বাছাই**\n" + DIV + "\nযেটা থেকে পোস্ট করবে সেটায় চাপ দাও 👇"
    buttons = []
    for acc in accounts.values():
        if not acc.enabled:
            continue
        if custom:
            mark = "☑️" if acc.id in chosen else "⬜"
            label = f"{mark} {acc.label[:20]} · {len(acc.groups)} গ্রুপ"
            buttons.append([Button.inline(label, f"w_tog:{acc.id}")])
        else:
            label = f"{acc.icon()} {acc.label[:20]} · {len(acc.groups)} গ্রুপ"
            buttons.append([Button.inline(label, f"w_pick:{acc.id}")])
    if custom:
        buttons.append([Button.inline(f"✅ এগিয়ে যান ({len(chosen)})", "w_done")])
    buttons.append(nav_row(f"w_start:{wiz.get('purpose', 'man')}"))
    return head, buttons


def wiz_prompt_screen():
    """Validate the chosen targets, then ask for the content."""
    targets = wiz_targets()
    groups = sum(len(a.groups) for a in targets)
    back = f"w_start:{wiz.get('purpose', 'man')}"
    if not targets or groups == 0:
        wiz["stage"] = None
        text = (
            f"{wiz_title()}\n{DIV}\n"
            "⚠️ বাছা অ্যাকাউন্টগুলোতে কোনো টার্গেট গ্রুপ নেই। আগে 🎯 **Groups** থেকে গ্রুপ বেছে নাও।"
        )
        return text, [[Button.inline("🎯 Groups", "menu_groups")], nav_row(back)]

    extra = ""
    if wiz.get("purpose") == "man":
        ready = [a for a in targets if a.groups and cooldown_left(a) == 0]
        cooling = [a for a in targets if a.groups and cooldown_left(a) > 0]
        if not ready:
            wiz["stage"] = None
            mins = int(min(cooldown_left(a) for a in cooling) // 60) + 1
            text = (
                f"⏳ **কুলডাউন চলছে**\n{DIV}\n"
                f"বাছা সব অ্যাকাউন্টই কুলডাউনে। প্রায় **{mins} মিনিট** পরে চেষ্টা করো।"
            )
            return text, [nav_row(back)]
        if cooling:
            extra = f"\n⏳ `{len(cooling)}` অ্যাকাউন্ট কুলডাউনে, এবার বাদ যাবে।"
        wiz["stage"] = "man_content"
    else:
        wiz["stage"] = "job_content"

    text = (
        f"{wiz_title()}\n"
        f"{DIV}\n"
        f"• টার্গেট: {wiz_target_text()}\n"
        f"• গ্রুপ: `{groups}` টি{extra}\n\n"
        "এখন তোমার মেসেজ (Text / Photo / Video) সেন্ড বা ফরওয়ার্ড করো 👇"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


def job_content_prompt_screen(job):
    text = (
        "📝 **নতুন কন্টেন্ট**\n"
        f"{DIV}\n"
        f"জব: **{job.name}**\n\n"
        "এখন নতুন মেসেজ (Text / Photo / Video) সেন্ড বা ফরওয়ার্ড করো 👇\n"
        "__এই মেসেজটা বটের চ্যাট থেকে মুছো না, নইলে রিস্টার্টের পর কন্টেন্ট হারিয়ে যাবে।__"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


def settings_screen():
    text = (
        "⚙️ **Settings**\n"
        f"{DIV}\n"
        f"🗑 Auto-Delete: **{onoff(auto_delete_enabled)}**\n"
        "__নতুন পোস্টের সময় আগের পুরোনো পোস্ট মুছে যায় (প্রতি গ্রুপে সর্বশেষ ২টি রেখে)।__\n\n"
        f"🔔 নোটিফিকেশন: **{onoff(notify_enabled)}**\n"
        "__প্রতিটা Auto-Post রান শেষ হলে তোমাকে রিপোর্ট পাঠায়।__\n\n"
        f"⏱ ম্যানুয়াল কুলডাউন: `{fmt_duration(COOLDOWN_TIME)}` (প্রতি অ্যাকাউন্টে)"
    )
    del_label = "🗑 Auto-Delete বন্ধ করুন" if auto_delete_enabled else "🗑 Auto-Delete চালু করুন"
    note_label = "🔔 নোটিফিকেশন বন্ধ করুন" if notify_enabled else "🔔 নোটিফিকেশন চালু করুন"
    buttons = [
        [Button.inline(del_label, "del_toggle")],
        [Button.inline(note_label, "notify_toggle")],
        nav_row(),
    ]
    return text, buttons


def help_screen():
    text = (
        "❓ **Help**\n"
        f"{DIV}\n"
        "**কীভাবে ব্যবহার করবে**\n"
        "1️⃣ 👥 **Accounts** থেকে অ্যাকাউন্ট যোগ করো\n"
        "2️⃣ 🎯 **Groups** থেকে প্রতি অ্যাকাউন্টের টার্গেট গ্রুপ ✅ ON করো\n"
        "3️⃣ 📨 **Manual Post** বা 🔁 **Auto-Post** চালাও\n\n"
        "**পোস্টের তিনটি উপায়**\n"
        "👤 একটি অ্যাকাউন্ট থেকে\n"
        "👥 সব অ্যাকাউন্ট থেকে একসাথে\n"
        "🎛 পছন্দমতো কয়েকটি অ্যাকাউন্ট থেকে\n\n"
        "প্রতিটা Auto-Post জবের আলাদা কন্টেন্ট ও সময় থাকে, "
        "আর একসাথে অনেকগুলো জব চলতে পারে।\n\n"
        "**কমান্ড (বাটনের বিকল্প)**\n"
        "• `/menu` — মূল মেনু\n"
        "• `/accounts` — অ্যাকাউন্ট ম্যানেজার\n"
        "• `/post` — ম্যানুয়াল পোস্ট\n"
        "• `/autopost` — Auto-Post জব\n"
        "• `/setautopost` — ডিফল্ট জবের কন্টেন্ট\n"
        "• `/autopost start` / `stop` / `time <ঘণ্টা>` — ডিফল্ট জব\n"
        "• `/autodelete on` / `off` — অটো-ডিলিট\n"
        "• `/groups` — টার্গেট গ্রুপ\n"
        "• `/stats` — ড্যাশবোর্ড\n"
        "• `/cancel` — চলমান ইনপুট বাতিল\n\n"
        "ℹ️ একসাথে অনেক অ্যাকাউন্ট থেকে পোস্ট একটার পর একটা যায় (বিরতি দিয়ে), "
        "যাতে FloodWait কম হয়।"
    )
    return text, [nav_row()]


# ---- Accounts screens -------------------------------------------------
def accounts_screen():
    lines = [
        "👥 **Accounts**",
        DIV,
        f"• মোট: `{len(accounts)}` · সক্রিয়: `{len(active_accounts())}`",
        f"• 🗄 {db_status_text()}",
    ]
    if not accounts_ready():
        lines.append("")
        lines.append("⚠️ নতুন অ্যাকাউন্ট যোগ করতে Render-এ Firebase-এর দুটো ভেরিয়েবল "
                     "ও `ENCRYPTION_KEY` সেট করতে হবে।")
    elif not accounts:
        lines.append("")
        lines.append("এখনো কোনো অ্যাকাউন্ট নেই। ➕ চেপে যোগ করো।")
    else:
        lines.append("")
        lines.append("অ্যাকাউন্টে চাপ দিয়ে বিস্তারিত দেখো 👇")

    buttons = []
    for acc in accounts.values():
        buttons.append([Button.inline(
            f"{acc.icon()} {acc.label[:22]} · {len(acc.groups)} গ্রুপ", f"acc:{acc.id}")])
    if accounts_ready():
        buttons.append([Button.inline("➕ নতুন অ্যাকাউন্ট যোগ করুন", "acc_add")])
    buttons.append(nav_row())
    return "\n".join(lines), buttons


def account_detail_screen(acc):
    if not acc.enabled:
        status = "⏸ বন্ধ করা"
    elif acc.connected:
        status = "🟢 সংযুক্ত"
    else:
        status = f"🔴 সংযোগ নেই ({acc.error or 'unknown'})"
    text = (
        f"👤 **{acc.label}**\n"
        f"{DIV}\n"
        f"• স্ট্যাটাস: {status}\n"
        f"• টার্গেট গ্রুপ: `{len(acc.groups)}` টি\n"
        f"• ধরন: {'Main (Render env)' if acc.is_main else 'Firebase-এ সংরক্ষিত'}"
    )
    toggle = "⏸ বন্ধ করুন" if acc.enabled else "▶️ চালু করুন"
    buttons = [
        [Button.inline(toggle, f"acc_toggle:{acc.id}"), Button.inline("🎯 গ্রুপ", f"gacc:{acc.id}")],
        [Button.inline("🔄 Reconnect", f"acc_re:{acc.id}")],
    ]
    if not acc.is_main:
        buttons.append([Button.inline("❌ মুছে ফেলুন", f"acc_del:{acc.id}")])
    buttons.append(nav_row("menu_accounts"))
    return text, buttons


def account_delete_screen(acc):
    text = (
        "❌ **অ্যাকাউন্ট মুছবে?**\n"
        f"{DIV}\n"
        f"**{acc.label}** বট ও Firebase থেকে মুছে যাবে।\n\n"
        "ℹ️ Telegram-এ ওই সেশনটা চালু থেকে যায়। পুরোপুরি বন্ধ করতে ওই অ্যাকাউন্টের "
        "Settings → Devices থেকে সেশনটা Terminate করো।"
    )
    buttons = [
        [Button.inline("✅ হ্যাঁ, মুছুন", f"acc_del_yes:{acc.id}"),
         Button.inline("↩️ না", f"acc:{acc.id}")],
    ]
    return text, buttons


def account_add_prompt_screen():
    text = (
        "➕ **নতুন অ্যাকাউন্ট**\n"
        f"{DIV}\n"
        "অ্যাকাউন্টের **Telethon session string** এখন পাঠাও 👇\n\n"
        "🔒 বট যাচাই করে এনক্রিপ্ট করে সেভ করবে, আর তোমার মেসেজটা সাথে সাথে মুছে দেবে।\n"
        "⚠️ শুধু **নিজের** অ্যাকাউন্টের session দাও।"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


# ---- Groups screens ---------------------------------------------------
def group_accounts_screen():
    text = (
        "🎯 **কোন অ্যাকাউন্টের গ্রুপ?**\n"
        f"{DIV}\n"
        "প্রতিটা অ্যাকাউন্টের গ্রুপ আলাদা। একটা বেছে নাও 👇"
    )
    buttons = []
    for acc in accounts.values():
        buttons.append([Button.inline(
            f"{acc.icon()} {acc.label[:22]} · {len(acc.groups)}", f"gacc:{acc.id}")])
    buttons.append(nav_row())
    return text, buttons


async def get_groups(acc, force=False):
    if not acc.connected:
        raise RuntimeError(acc.error or "অ্যাকাউন্ট সংযুক্ত নেই")
    cache = groups_cache.get(acc.id)
    now = time.time()
    if force or not cache or now - cache["time"] > GROUP_CACHE_TTL:
        dialogs = await acc.client.get_dialogs()
        items = [(d.id, d.title or "Unnamed") for d in dialogs if d.is_group]
        items.sort(key=lambda x: x[1].lower())
        cache = {"time": now, "items": items}
        groups_cache[acc.id] = cache
    return cache["items"]


async def groups_screen(acc_id, page=0, force=False):
    acc = accounts.get(acc_id)
    if not acc:
        return accounts_screen()

    try:
        items = await get_groups(acc, force)
    except Exception as e:
        text = (
            "🎯 **টার্গেট গ্রুপ**\n"
            f"{DIV}\n"
            f"❌ **{acc.label}** থেকে গ্রুপ লোড করা যায়নি: `{str(e)[:80]}`"
        )
        return text, [[Button.inline("🔄 Reconnect", f"acc_re:{acc.id}")], nav_row("menu_groups")]

    if not items:
        text = (
            "🎯 **টার্গেট গ্রুপ**\n"
            f"{DIV}\n"
            f"❌ **{acc.label}** অ্যাকাউন্টে কোনো গ্রুপ পাওয়া যায়নি।"
        )
        return text, [nav_row("menu_groups")]

    total_pages = max(1, (len(items) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = min(max(page, 0), total_pages - 1)
    chunk = items[page * GROUPS_PER_PAGE:(page + 1) * GROUPS_PER_PAGE]

    text = (
        f"🎯 **টার্গেট গ্রুপ** · {acc.label}\n"
        f"{DIV}\n"
        f"✅ সিলেক্টেড: `{len(acc.groups)}` / {len(items)}\n"
        f"📄 পেজ: `{page + 1}/{total_pages}`\n\n"
        "বাটনে চাপ দিয়ে চালু/বন্ধ করো:\n"
        "• ✅ = এখানে পোস্ট যাবে\n"
        "• ❌ = এখানে পোস্ট যাবে না"
    )

    buttons = []
    for gid, title in chunk:
        symbol = "✅" if gid in acc.groups else "❌"
        buttons.append([Button.inline(f"{symbol} {title[:28]}", f"tg:{acc.id}:{gid}:{page}")])

    pager = []
    if page > 0:
        pager.append(Button.inline("◀️ আগে", f"grp_page:{acc.id}:{page - 1}"))
    if page < total_pages - 1:
        pager.append(Button.inline("পরে ▶️", f"grp_page:{acc.id}:{page + 1}"))
    if pager:
        buttons.append(pager)

    buttons.append([Button.inline("💾 Save & Close", "close_groups")])
    buttons.append(nav_row("menu_groups"))
    return text, buttons


def groups_saved_screen():
    text = (
        "✅ **টার্গেট গ্রুপ সেভ হয়েছে!**\n"
        f"{DIV}\n"
        f"🎯 মোট সিলেক্টেড (সক্রিয় অ্যাকাউন্ট): `{total_targets()}` টি\n\n"
        "এখন পোস্ট করতে পারো 👇"
    )
    buttons = [
        [Button.inline("📨 Manual Post", "menu_post"), Button.inline("🔁 Auto-Post", "menu_auto")],
        nav_row(),
    ]
    return text, buttons


# ------------------------------------------------------------------
# Posting engine
# ------------------------------------------------------------------
async def prepare_payload(message, dest):
    """Turn a bot-chat message into something every account can send."""
    media_path = None
    if message.media and not isinstance(message.media, MessageMediaWebPage):
        os.makedirs(PAYLOAD_DIR, exist_ok=True)
        media_path = await message.download_media(file=dest)
    return {
        "text": message.message or "",
        "entities": message.entities,
        "media": media_path,
    }


def cleanup_payload(payload, keep=None):
    try:
        path = payload and payload.get("media")
        if path and path != keep and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _send(client, group_id, payload):
    if payload["media"]:
        return await client.send_file(
            group_id, payload["media"],
            caption=payload["text"], formatting_entities=payload["entities"])
    return await client.send_message(
        group_id, payload["text"], formatting_entities=payload["entities"])


async def send_to_group(acc, group_id, payload):
    """Delete old post (if enabled) and send the new one from this account."""
    key = (acc.id, group_id)
    sent = last_sent_messages.setdefault(key, [])

    if auto_delete_enabled and len(sent) >= 2:
        old_id = sent.pop(0)
        try:
            await acc.client.delete_messages(group_id, old_id)
        except Exception as e:
            logger.warning("Delete failed (%s / %s): %s", acc.id, group_id, type(e).__name__)

    try:
        msg = await _send(acc.client, group_id, payload)
    except FloodWaitError as e:
        if e.seconds > 300:
            raise
        logger.warning("FloodWait %ss (%s / %s), waiting...", e.seconds, acc.id, group_id)
        await asyncio.sleep(e.seconds + 1)
        msg = await _send(acc.client, group_id, payload)

    sent.append(msg.id)


async def broadcast(payload, targets):
    """Send the payload from each given account to its own groups, one account
    after another. Returns a list of (account, ok, failed, note)."""
    results = []
    for acc in targets:
        if not acc.enabled or not acc.groups:
            continue
        if not acc.connected:
            await connect_account(acc)  # one reconnect attempt
        if not acc.connected:
            results.append((acc, 0, len(acc.groups), acc.error or "সংযোগ নেই"))
            continue

        ok = failed = 0
        for group_id in list(acc.groups):
            try:
                await send_to_group(acc, group_id, payload)
                ok += 1
                await asyncio.sleep(random.randint(2, 5))
            except Exception as e:
                failed += 1
                logger.error("Post failed (%s / %s): %s", acc.id, group_id, type(e).__name__)
                await asyncio.sleep(1)
        if ok:
            acc.last_post = time.time()
        results.append((acc, ok, failed, ""))
    return results


# ------------------------------------------------------------------
# Auto-Post jobs
# ------------------------------------------------------------------
class Job:
    """One Auto-Post job: its own targets, content and interval."""

    def __init__(self, job_id, name, mode, accts=None, interval=3600,
                 msg_id=None, running=False):
        self.id = job_id
        self.name = name
        self.mode = mode                  # "all" | "single" | "custom"
        self.accounts = set(accts or [])  # used when mode != "all"
        self.interval = interval
        self.msg_id = msg_id              # content message in the admin chat
        self.running = running
        self.payload = None
        self.task = None
        self.last_run = 0
        self.last_result = (0, 0)
        self.next_run = 0

    def targets(self):
        if self.mode == "all":
            return [a for a in accounts.values() if a.enabled]
        return [accounts[i] for i in self.accounts if i in accounts and accounts[i].enabled]

    def groups_count(self):
        return sum(len(a.groups) for a in self.targets())

    def icon(self):
        return "🟢" if self.running else "🔴"

    def target_text(self):
        if self.mode == "all":
            return f"👥 সব অ্যাকাউন্ট ({len(self.targets())})"
        names = [accounts[i].label for i in self.accounts if i in accounts]
        if len(names) == 1:
            return f"👤 {names[0]}"
        return f"🎛 {len(names)}টি অ্যাকাউন্ট"

    def record(self):
        return {
            "name": self.name,
            "mode": self.mode,
            "accounts": sorted(self.accounts),
            "interval": self.interval,
            "msg_id": self.msg_id,
            "running": self.running,
        }

    @staticmethod
    def from_record(job_id, rec):
        interval = rec.get("interval")
        if not isinstance(interval, int) or interval < MIN_INTERVAL_H * 3600:
            interval = 3600
        return Job(
            job_id, rec.get("name") or "Job", rec.get("mode") or "all",
            rec.get("accounts") or [], interval, rec.get("msg_id"),
            bool(rec.get("running")),
        )


def new_job_id():
    base = f"j{int(time.time())}"
    job_id, n = base, 1
    while job_id in jobs:
        n += 1
        job_id = f"{base}{n}"
    return job_id


async def save_jobs():
    await kv_set("jobs", JOBS_FILE, {j.id: j.record() for j in jobs.values()})


async def restore_job_content(job):
    peer = admin_peer()
    if not (job.msg_id and peer):
        return
    try:
        msg = await bot.get_messages(peer, ids=int(job.msg_id))
        if msg:
            job.payload = await prepare_payload(
                msg, os.path.join(PAYLOAD_DIR, f"job_{job.id}"))
    except Exception as e:
        logger.warning("Could not restore content of job %s: %s", job.id, type(e).__name__)


async def notify_admin(text):
    if not notify_enabled:
        return
    peer = admin_peer()
    if not peer:
        return
    try:
        await bot.send_message(peer, text)
    except Exception as e:
        logger.debug("notify failed: %s", e)


async def job_loop(job):
    while job.running:
        job.next_run = time.time() + job.interval
        await asyncio.sleep(job.interval)
        if not job.running:
            break
        targets = job.targets()
        if not (job.payload and targets and job.groups_count()):
            continue

        logger.info("⚡ Running job %s", job.id)
        try:
            results = await broadcast(job.payload, targets)
            ok = sum(r[1] for r in results)
            fail = sum(r[2] for r in results)
            job.last_run = time.time()
            job.last_result = (ok, fail)
            log_activity(f"🔁 {job.name}: ✅ {ok} ❌ {fail}")
            await notify_admin(
                f"🔁 **{job.name}**\n{DIV}\n✅ সফল: `{ok}` · ❌ ব্যর্থ: `{fail}`")
        except Exception as e:
            # The loop must never die because of one bad cycle
            logger.error("Job %s cycle failed: %s", job.id, type(e).__name__)


def can_start(job):
    if not job.payload:
        return False, "❌ আগে কন্টেন্ট সেট করো!"
    if not job.targets() or job.groups_count() == 0:
        return False, "❌ টার্গেট অ্যাকাউন্টে কোনো গ্রুপ নেই!"
    return True, ""


async def start_job(job):
    if job.running and job.task and not job.task.done():
        return False, "⚠️ জব আগে থেকেই চালু আছে!"
    ok, msg = can_start(job)
    if not ok:
        return False, msg
    job.running = True
    job.task = asyncio.create_task(job_loop(job))
    await save_jobs()
    log_activity(f"🚀 {job.name} চালু")
    return True, "🚀 চালু হয়েছে!"


async def stop_job(job):
    if not job.running:
        return False, "⚠️ জব আগে থেকেই বন্ধ আছে।"
    job.running = False
    job.next_run = 0
    if job.task and not job.task.done():
        job.task.cancel()
    await save_jobs()
    log_activity(f"🛑 {job.name} বন্ধ")
    return True, "🛑 বন্ধ হয়েছে!"


async def delete_job(job):
    if job.running:
        await stop_job(job)
    jobs.pop(job.id, None)
    cleanup_payload(job.payload)
    await save_jobs()
    log_activity(f"🗑 {job.name} মুছে ফেলা হয়েছে")


async def get_or_create_default_job():
    job = jobs.get("default")
    if not job:
        job = Job("default", "Default · সব অ্যাকাউন্ট", "all")
        jobs["default"] = job
        await save_jobs()
    return job


# ------------------------------------------------------------------
# Telegram "Menu" button (command list)
# ------------------------------------------------------------------
if MENU_OK:
    PUBLIC_COMMANDS = [
        BotCommand(command="start", description="🏠 মূল মেনু খুলুন"),
        BotCommand(command="help", description="❓ কমান্ড ও সাহায্য দেখুন"),
    ]

    ADMIN_COMMANDS = [
        BotCommand(command="start", description="🏠 মূল মেনু খুলুন"),
        BotCommand(command="stats", description="📊 ড্যাশবোর্ড ও স্ট্যাটাস"),
        BotCommand(command="accounts", description="👥 অ্যাকাউন্ট ম্যানেজার"),
        BotCommand(command="groups", description="🎯 টার্গেট গ্রুপ বাছুন"),
        BotCommand(command="post", description="📨 ম্যানুয়াল ব্রডকাস্ট"),
        BotCommand(command="setautopost", description="📝 অটো-পোস্টের কন্টেন্ট সেট"),
        BotCommand(command="autopost", description="🔁 অটো-পোস্ট নিয়ন্ত্রণ"),
        BotCommand(command="autodelete", description="🗑 অটো-ডিলিট সেটিং"),
        BotCommand(command="cancel", description="❌ চলমান ইনপুট বাতিল"),
        BotCommand(command="help", description="❓ কমান্ড ও সাহায্য দেখুন"),
    ]
else:
    PUBLIC_COMMANDS = []
    ADMIN_COMMANDS = []


async def set_public_menu():
    if not MENU_OK:
        return
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="", commands=PUBLIC_COMMANDS))
    except Exception as e:
        logger.warning("Could not set public command menu: %s", e)


async def set_admin_menu(peer=None):
    """Full command menu, visible only in the admin's chat."""
    if not MENU_OK:
        return
    try:
        peer = peer or admin_peer()
        if peer is None:
            return
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopePeer(peer=peer), lang_code="", commands=ADMIN_COMMANDS))
        logger.info("Admin command menu set.")
    except Exception as e:
        logger.warning("Admin command menu not set yet: %s", e)


async def remember_admin(event):
    """Store the admin's access hash so the bot can reach them after redeploys."""
    global admin_hash
    try:
        peer = await event.get_input_sender()
        h = getattr(peer, "access_hash", None)
        if h and h != admin_hash:
            admin_hash = h
            await save_settings()
        return peer
    except Exception as e:
        logger.debug("remember_admin failed: %s", e)
        return None


# ------------------------------------------------------------------
# Admin guard + helpers for text commands
# ------------------------------------------------------------------
def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(event):
        if event.sender_id != ADMIN_ID:
            await event.reply(DENIED_TEXT)
            return
        await handler(event)

    return wrapper


async def sender_name(event):
    try:
        sender = await event.get_sender()
        return getattr(sender, "first_name", None) or "Admin"
    except Exception:
        return "Admin"


# ------------------------------------------------------------------
# Wizard helpers
# ------------------------------------------------------------------
async def reply_screen(event, scr):
    await event.reply(scr[0], buttons=scr[1])


async def create_job_from_wizard(hours):
    targets = wiz_targets()
    msg = wiz.get("message")
    if not targets or msg is None:
        return None
    mode = wiz.get("mode") or "custom"
    accts = set() if mode == "all" else {a.id for a in targets}
    if mode == "all":
        base = "সব অ্যাকাউন্ট"
    elif len(targets) == 1:
        base = targets[0].name
    else:
        base = f"কাস্টম ({len(targets)})"
    job = Job(new_job_id(), f"#{len(jobs) + 1} {base}", mode, accts, hours * 3600, msg_id=msg.id)
    job.payload = await prepare_payload(msg, os.path.join(PAYLOAD_DIR, f"job_{job.id}"))
    jobs[job.id] = job
    await save_jobs()
    log_activity(f"➕ {job.name} তৈরি")
    wiz.clear()
    return job


async def apply_interval(job, hours):
    job.interval = hours * 3600
    if job.running:
        if job.task and not job.task.done():
            job.task.cancel()
        job.task = asyncio.create_task(job_loop(job))
    await save_jobs()


async def run_manual(event):
    """Manual post: send the message the admin just gave from the chosen accounts."""
    targets = [a for a in wiz_targets() if a.groups]
    eligible = [a for a in targets if cooldown_left(a) == 0]
    skipped = [a for a in targets if cooldown_left(a) > 0]
    wiz.clear()

    if not eligible:
        await event.reply("⏳ **সব অ্যাকাউন্ট কুলডাউনে আছে।** পরে চেষ্টা করো।", buttons=[nav_row()])
        return

    groups = sum(len(a.groups) for a in eligible)
    processing = await event.reply(
        "⏳ **প্রসেসিং...**\n"
        f"{DIV}\n"
        f"👥 `{len(eligible)}` অ্যাকাউন্ট থেকে `{groups}` টি গ্রুপে পোস্ট পাঠানো হচ্ছে।"
    )

    payload = None
    try:
        payload = await prepare_payload(
            event.message, os.path.join(PAYLOAD_DIR, f"manual_{int(time.time())}"))
        results = await broadcast(payload, eligible)
    except Exception as e:
        logger.error("Manual broadcast failed: %s", type(e).__name__)
        await processing.edit("❌ **ব্রডকাস্ট ব্যর্থ হয়েছে।** আবার চেষ্টা করো।", buttons=[nav_row()])
        return
    finally:
        cleanup_payload(payload)

    total_ok = sum(r[1] for r in results)
    total_fail = sum(r[2] for r in results)
    log_activity(f"📨 Manual: ✅ {total_ok} ❌ {total_fail} ({len(results)} অ্যাকাউন্ট)")

    lines = [
        "✅ **ব্রডকাস্ট সম্পন্ন**",
        DIV,
        f"• সফল: `{total_ok}` টি গ্রুপ ✅",
        f"• ব্যর্থ: `{total_fail}` টি গ্রুপ ❌",
        "",
        "👥 **অ্যাকাউন্ট অনুযায়ী**",
    ]
    for acc, ok_n, fail_n, note in results:
        suffix = f" — {note}" if note else ""
        lines.append(f"• {acc.label[:20]}: ✅ {ok_n} · ❌ {fail_n}{suffix}")
    for acc in skipped:
        lines.append(f"• {acc.label[:20]}: ⏳ কুলডাউন (`{int(cooldown_left(acc) // 60) + 1}` মিনিট বাকি)")
    await processing.edit("\n".join(lines), buttons=[nav_row()])


# ------------------------------------------------------------------
# Text commands
# ------------------------------------------------------------------
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    bot_users.add(event.sender_id)
    if event.sender_id != ADMIN_ID:
        await event.reply(PUBLIC_TEXT)
        return
    await show(event, *home_screen(await sender_name(event)))
    peer = await remember_admin(event)
    await set_admin_menu(peer)


@bot.on(events.NewMessage(pattern=r"^/menu$"))
@admin_only
async def menu_handler(event):
    await show(event, *home_screen(await sender_name(event)))


@bot.on(events.NewMessage(pattern=r"^/help"))
async def help_handler(event):
    bot_users.add(event.sender_id)
    if event.sender_id != ADMIN_ID:
        await event.reply(PUBLIC_TEXT)
        return
    await show(event, *help_screen())


@bot.on(events.NewMessage(pattern=r"^/stats"))
@admin_only
async def stats_handler(event):
    await show(event, *dashboard_screen())


@bot.on(events.NewMessage(pattern=r"^/accounts$"))
@admin_only
async def accounts_handler(event):
    await show(event, *accounts_screen())


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
@admin_only
async def cancel_handler(event):
    wiz.clear()
    waiting_session_users.discard(event.sender_id)
    await event.reply("❌ বাতিল করা হয়েছে।", buttons=[nav_row()])


@bot.on(events.NewMessage(pattern=r"^/groups"))
@admin_only
async def groups_manager(event):
    if not accounts:
        await show(event, *accounts_screen())
        return
    if len(accounts) == 1:
        acc_id = next(iter(accounts))
        msg = await event.reply("⏳ **গ্রুপ লোড হচ্ছে...**")
        text, buttons = await groups_screen(acc_id, 0, force=True)
        await msg.edit(text, buttons=buttons)
    else:
        await show(event, *group_accounts_screen())


@bot.on(events.NewMessage(pattern=r"^/autodelete\s+(on|off)$"))
@admin_only
async def autodelete_toggle(event):
    global auto_delete_enabled
    auto_delete_enabled = event.pattern_match.group(1).lower() == "on"
    await save_settings()
    await show(event, *settings_screen())


@bot.on(events.NewMessage(pattern=r"^/post$"))
@admin_only
async def post_handler(event):
    bot_users.add(event.sender_id)
    waiting_session_users.discard(event.sender_id)
    wiz.clear()
    wiz.update(purpose="man")
    await show(event, *wiz_mode_screen())


@bot.on(events.NewMessage(pattern=r"^/setautopost$"))
@admin_only
async def set_autopost_prompt(event):
    """Legacy command: sets the content of the Default job (all accounts)."""
    waiting_session_users.discard(event.sender_id)
    job = await get_or_create_default_job()
    wiz.clear()
    wiz.update(purpose="edit", job_id=job.id, stage="edit_content")
    await show(event, *job_content_prompt_screen(job))


@bot.on(events.NewMessage(pattern=r"^/autopost(?:\s+(start|stop|time(?:\s+\d+)?))?$"))
@admin_only
async def autopost_control(event):
    args = event.text.split()
    action = args[1].lower() if len(args) > 1 else ""

    if action in ("start", "stop"):
        job = jobs.get("default")
        if not job:
            await event.reply("⚠️ আগে `/setautopost` দিয়ে কন্টেন্ট সেট করো, "
                              "অথবা 🔁 Auto-Post মেনু থেকে নতুন জব বানাও।")
            return
        ok, msg = await (start_job(job) if action == "start" else stop_job(job))
        await event.reply(msg)
        await show(event, *job_detail_screen(job))
    elif action == "time":
        if len(args) > 2 and args[2].isdigit():
            hours = int(args[2])
            if not valid_hours(hours):
                await event.reply(f"❌ সময় {MIN_INTERVAL_H} থেকে {MAX_INTERVAL_H} ঘণ্টার মধ্যে হতে হবে!")
                return
            job = await get_or_create_default_job()
            await apply_interval(job, hours)
            await event.reply(f"⏱ ডিফল্ট জবের ব্যবধান: প্রতি `{hours}` ঘণ্টা।")
            await show(event, *job_detail_screen(job))
        else:
            await event.reply("⚠️ নিয়ম: `/autopost time <ঘণ্টা>` (যেমন: `/autopost time 2`)")
    else:
        await show(event, *jobs_screen())


# ------------------------------------------------------------------
# Button router
# ------------------------------------------------------------------
@bot.on(events.CallbackQuery)
async def callback_router(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("🔒 শুধু অ্যাডমিনের জন্য!", alert=True)
        return

    global auto_delete_enabled, notify_enabled
    data = event.data.decode("utf-8", "ignore")
    parts = data.split(":")
    cmd = parts[0]
    uid = event.sender_id
    toast = ""
    alert = False
    answered = False

    async def go_home():
        await show(event, *home_screen(await sender_name(event)))

    async def stale():
        wiz.clear()
        await go_home()

    try:
        # ---- general menus ----
        if cmd == "menu_home":
            wiz.clear()
            waiting_session_users.discard(uid)
            await go_home()

        elif cmd == "menu_dash":
            await show(event, *dashboard_screen())

        elif cmd == "menu_activity":
            await show(event, *activity_screen())

        elif cmd == "menu_help":
            await show(event, *help_screen())

        elif cmd == "menu_settings":
            await show(event, *settings_screen())

        elif cmd == "del_toggle":
            auto_delete_enabled = not auto_delete_enabled
            await save_settings()
            toast = f"Auto-Delete {'ON' if auto_delete_enabled else 'OFF'}"
            await show(event, *settings_screen())

        elif cmd == "notify_toggle":
            notify_enabled = not notify_enabled
            await save_settings()
            toast = f"নোটিফিকেশন {'ON' if notify_enabled else 'OFF'}"
            await show(event, *settings_screen())

        elif cmd == "cancel_wait":
            wiz.clear()
            waiting_session_users.discard(uid)
            toast = "বাতিল করা হয়েছে"
            await go_home()

        # ---- accounts ----
        elif cmd == "menu_accounts":
            wiz.clear()
            waiting_session_users.discard(uid)
            await show(event, *accounts_screen())

        elif cmd == "acc":
            acc = accounts.get(parts[1])
            if acc:
                await show(event, *account_detail_screen(acc))
            else:
                await show(event, *accounts_screen())

        elif cmd == "acc_add":
            if not accounts_ready():
                toast, alert = "⚠️ Firebase ও ENCRYPTION_KEY সেট করা নেই", True
            else:
                wiz.clear()
                waiting_session_users.add(uid)
                await show(event, *account_add_prompt_screen())

        elif cmd == "acc_toggle":
            acc = accounts.get(parts[1])
            if acc:
                acc.enabled = not acc.enabled
                await persist_account(acc)
                toast = "▶️ চালু করা হয়েছে" if acc.enabled else "⏸ বন্ধ করা হয়েছে"
                await show(event, *account_detail_screen(acc))

        elif cmd == "acc_re":
            acc = accounts.get(parts[1])
            if acc:
                await event.answer("🔄 সংযোগ করা হচ্ছে...")
                answered = True
                await safe_disconnect(acc)
                acc.client = None
                await connect_account(acc)
                await show(event, *account_detail_screen(acc))

        elif cmd == "acc_del":
            acc = accounts.get(parts[1])
            if acc and not acc.is_main:
                await show(event, *account_delete_screen(acc))

        elif cmd == "acc_del_yes":
            acc = accounts.get(parts[1])
            if acc and not acc.is_main:
                ok = await remove_account(acc)
                log_activity(f"❌ অ্যাকাউন্ট মুছেছে: {acc.label}")
                toast = "✅ মুছে ফেলা হয়েছে" if ok else "⚠️ Firebase থেকে মুছতে সমস্যা হয়েছে"
                alert = not ok
            await show(event, *accounts_screen())

        # ---- groups ----
        elif cmd == "menu_groups":
            if not accounts:
                await show(event, *accounts_screen())
            elif len(accounts) == 1:
                await event.answer("⏳ লোড হচ্ছে...")
                answered = True
                await show(event, *await groups_screen(next(iter(accounts)), 0, force=True))
            else:
                await show(event, *group_accounts_screen())

        elif cmd == "gacc":
            await event.answer("⏳ লোড হচ্ছে...")
            answered = True
            await show(event, *await groups_screen(parts[1], 0, force=True))

        elif cmd == "grp_page":
            await show(event, *await groups_screen(parts[1], int(parts[2])))

        elif cmd == "tg":
            acc = accounts.get(parts[1])
            gid = int(parts[2])
            if acc:
                if gid in acc.groups:
                    acc.groups.discard(gid)
                    toast = "❌ বন্ধ করা হয়েছে"
                else:
                    acc.groups.add(gid)
                    toast = "✅ চালু করা হয়েছে"
                await persist_account(acc)
                await show(event, *await groups_screen(acc.id, int(parts[3])))

        elif cmd == "close_groups":
            await show(event, *groups_saved_screen())

        # ---- post wizard ----
        elif cmd == "menu_post":
            wiz.clear()
            wiz.update(purpose="man")
            await show(event, *wiz_mode_screen())

        elif cmd == "job_new":
            wiz.clear()
            wiz.update(purpose="job")
            await show(event, *wiz_mode_screen())

        elif cmd == "w_start":
            wiz.clear()
            wiz.update(purpose=parts[1])
            await show(event, *wiz_mode_screen())

        elif cmd == "w_mode":
            if not wiz.get("purpose"):
                await stale()
            else:
                wiz["mode"] = parts[1]
                wiz["accts"] = set()
                wiz["stage"] = None
                if parts[1] == "all":
                    await show(event, *wiz_prompt_screen())
                else:
                    await show(event, *wiz_pick_screen())

        elif cmd == "w_pick":
            if not wiz.get("purpose"):
                await stale()
            else:
                wiz["mode"] = "single"
                wiz["accts"] = {parts[1]}
                await show(event, *wiz_prompt_screen())

        elif cmd == "w_tog":
            if not wiz.get("purpose"):
                await stale()
            else:
                chosen = wiz.setdefault("accts", set())
                if parts[1] in chosen:
                    chosen.discard(parts[1])
                else:
                    chosen.add(parts[1])
                await show(event, *wiz_pick_screen())

        elif cmd == "w_done":
            if not wiz.get("purpose"):
                await stale()
            elif not wiz.get("accts"):
                toast, alert = "⚠️ অন্তত একটা অ্যাকাউন্ট বাছো", True
            else:
                await show(event, *wiz_prompt_screen())

        elif cmd == "w_int":
            hours = int(parts[1])
            if wiz.get("purpose") != "job" or not valid_hours(hours):
                await stale()
            else:
                await event.answer("⏳ তৈরি হচ্ছে...")
                answered = True
                job = await create_job_from_wizard(hours)
                if job:
                    await show(event, *job_detail_screen(job, created=True))
                else:
                    await stale()

        elif cmd == "w_int_c":
            if wiz.get("purpose") != "job":
                await stale()
            else:
                wiz["stage"] = "int_custom_new"
                await show(event, *interval_custom_prompt_screen())

        # ---- Auto-Post jobs ----
        elif cmd == "menu_auto":
            wiz.clear()
            await show(event, *jobs_screen())

        elif cmd == "job":
            job = jobs.get(parts[1])
            await show(event, *(job_detail_screen(job) if job else jobs_screen()))

        elif cmd == "job_start":
            job = jobs.get(parts[1])
            if job:
                ok, toast = await start_job(job)
                alert = not ok
                await show(event, *job_detail_screen(job))

        elif cmd == "job_stop":
            job = jobs.get(parts[1])
            if job:
                ok, toast = await stop_job(job)
                alert = not ok
                await show(event, *job_detail_screen(job))

        elif cmd == "jobs_stop_all":
            for job in running_jobs():
                await stop_job(job)
            toast = "🛑 সব জব বন্ধ করা হয়েছে"
            await show(event, *jobs_screen())

        elif cmd == "job_content":
            job = jobs.get(parts[1])
            if job:
                wiz.clear()
                wiz.update(purpose="edit", job_id=job.id, stage="edit_content")
                await show(event, *job_content_prompt_screen(job))

        elif cmd == "job_time":
            job = jobs.get(parts[1])
            if job:
                await show(event, *interval_screen(
                    "জবের ব্যবধান", f"job_t:{job.id}", f"job_tc:{job.id}",
                    back=f"job:{job.id}", current=job.interval))

        elif cmd == "job_t":
            job = jobs.get(parts[1])
            hours = int(parts[2])
            if job and valid_hours(hours):
                await apply_interval(job, hours)
                toast = f"⏱ ব্যবধান: {hours} ঘণ্টা"
                await show(event, *job_detail_screen(job))

        elif cmd == "job_tc":
            job = jobs.get(parts[1])
            if job:
                wiz.clear()
                wiz.update(purpose="edit", job_id=job.id, stage="int_custom_job")
                await show(event, *interval_custom_prompt_screen())

        elif cmd == "job_del":
            job = jobs.get(parts[1])
            if job:
                await show(event, *job_delete_screen(job))

        elif cmd == "job_del_yes":
            job = jobs.get(parts[1])
            if job:
                await delete_job(job)
                toast = "🗑 মুছে ফেলা হয়েছে"
            await show(event, *jobs_screen())

    except Exception as e:
        logger.error("Callback error (%s): %s", cmd, type(e).__name__)
        toast = "❌ সমস্যা হয়েছে, আবার চেষ্টা করো"
        alert = True

    if not answered:
        try:
            await event.answer(toast, alert=alert)
        except Exception:
            pass


# ------------------------------------------------------------------
# Incoming content (session / manual post / auto-post content / hours)
# ------------------------------------------------------------------
@bot.on(events.NewMessage)
async def content_handler(event):
    user_id = event.sender_id
    if user_id != ADMIN_ID:
        return

    text = event.text or ""
    is_cmd = text.startswith("/")

    # 0) New account session string
    if user_id in waiting_session_users:
        if is_cmd:
            return
        waiting_session_users.discard(user_id)
        session_text = text.strip()
        try:
            await event.delete()  # never leave the session string in the chat
        except Exception:
            pass
        status = await bot.send_message(event.chat_id, "⏳ **যাচাই করা হচ্ছে...**")
        ok, msg, acc = await add_account(session_text)
        if ok:
            log_activity(f"➕ অ্যাকাউন্ট যোগ: {acc.label}")
            await status.edit(
                "✅ **অ্যাকাউন্ট যোগ হয়েছে!**\n"
                f"{DIV}\n"
                f"👤 **{acc.label}**\n\n"
                "এখন এর টার্গেট গ্রুপ বেছে নাও 👇",
                buttons=[[Button.inline("🎯 গ্রুপ বাছুন", f"gacc:{acc.id}")],
                         nav_row("menu_accounts")],
            )
        else:
            await status.edit(
                f"❌ **যোগ করা যায়নি**\n{DIV}\n{msg}",
                buttons=[[Button.inline("🔁 আবার চেষ্টা", "acc_add")],
                         nav_row("menu_accounts")],
            )
        return

    stage = wiz.get("stage")
    if not stage or is_cmd:
        return

    # 1) Custom number of hours
    if stage in ("int_custom_new", "int_custom_job"):
        value = text.strip()
        if not value.isdigit() or not valid_hours(int(value)):
            await event.reply(
                f"❌ {MIN_INTERVAL_H} থেকে {MAX_INTERVAL_H}-র মধ্যে একটা সংখ্যা লেখো (ঘণ্টা)।",
                buttons=[[Button.inline("❌ বাতিল", "cancel_wait")]])
            return
        hours = int(value)
        if stage == "int_custom_new":
            job = await create_job_from_wizard(hours)
            if job:
                await reply_screen(event, job_detail_screen(job, created=True))
            else:
                wiz.clear()
                await event.reply("⚠️ সেশন শেষ হয়ে গেছে, আবার শুরু করো।", buttons=[nav_row()])
        else:
            job = jobs.get(wiz.get("job_id"))
            wiz.clear()
            if job:
                await apply_interval(job, hours)
                await reply_screen(event, job_detail_screen(job))
        return

    # 2) Manual post content
    if stage == "man_content":
        await run_manual(event)
        return

    # 3) New job content -> ask for the interval
    if stage == "job_content":
        wiz["message"] = event.message
        wiz["stage"] = None
        await reply_screen(event, interval_screen(
            "Auto-Post ব্যবধান", "w_int", "w_int_c"))
        return

    # 4) Replace the content of an existing job
    if stage == "edit_content":
        job = jobs.get(wiz.get("job_id"))
        if not job:
            wiz.clear()
            await event.reply("⚠️ জবটা পাওয়া যায়নি।", buttons=[nav_row()])
            return
        old = job.payload
        try:
            new = await prepare_payload(
                event.message, os.path.join(PAYLOAD_DIR, f"job_{job.id}_{int(time.time())}"))
        except Exception as e:
            logger.error("Could not prepare job content: %s", type(e).__name__)
            await event.reply("❌ কন্টেন্ট সেভ করা যায়নি, আবার চেষ্টা করো।", buttons=[nav_row()])
            return
        job.payload = new
        job.msg_id = event.message.id
        cleanup_payload(old, keep=new.get("media"))
        await save_jobs()
        wiz.clear()
        log_activity(f"📝 {job.name}: কন্টেন্ট আপডেট")
        await reply_screen(event, job_detail_screen(job))
        return


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def main():
    logger.info("Starting JisanV2 Engine (multi-account)...")
    store.init()
    if store.ok and fernet is None:
        logger.error("ENCRYPTION_KEY missing or shorter than 16 characters: "
                     "adding/loading Firebase accounts is disabled.")
    await start_dummy_server()
    await bot.start(bot_token=BOT_TOKEN)
    await set_public_menu()
    await load_state()
    await set_admin_menu()
    logger.info("🚀 JisanV2 Engine is Online!")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
