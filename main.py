import os
import sys
import json
import asyncio
import logging
import time
import html
from datetime import datetime
from email.utils import parseaddr
from base64 import urlsafe_b64decode
from email.header import decode_header

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from aiogram.client.default import DefaultBotProperties

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── CONFIG & STATE FILES ────────────────────────────────────────────────────
_pending_flows: dict[int, InstalledAppFlow] = {}

SCOPES               = ["https://www.googleapis.com/auth/gmail.modify"]
CLIENT_SECRETS       = "client_secret.json"
CREDENTIALS_FILE     = "credentials.json"
WHITELIST_FILE       = "whitelist"
UNREAD_STORE_FILE    = "unread_store.json"
STATE_FILE           = "state.json"
APIKEY_FILE          = "APIKEY"

ADMIN_FILE           = "admins.json"          # e.g. [12345678]
ALLOWED_GROUPS_FILE  = "allowed_groups.json"  # auto-populated

POLL_INTERVAL        = 60        # seconds
FIRST_REMINDER       = 60        # seconds
LOOP_REMINDER        = 5 * 60    # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ensure required static files exist
for fn in (CLIENT_SECRETS, APIKEY_FILE, ADMIN_FILE):
    if not os.path.exists(fn):
        sys.exit(f"Error: {fn} not found, follow the readme for instructions.")

# ─── BOT INIT ─────────────────────────────────────────────────────────────────
bot = Bot(
    token=open(APIKEY_FILE, encoding="utf-8").read().strip(),
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

# ensure dynamic JSON files exist
open(ALLOWED_GROUPS_FILE,  "a").close()
open(WHITELIST_FILE,       "a").close()
open(UNREAD_STORE_FILE,    "a").close()

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────
CHAT_ID            = None
_poll_task         = None
_reminder_tasks    = {}     # gmail_msg_id → asyncio.Task
last_checked_ts    = 0      # milliseconds since epoch

# whitelist-management flags
_adding, _removing = False, False
_remove_index      = None

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def save_state():
    save_json(STATE_FILE, {
        "chat_id":         CHAT_ID,
        "last_checked_ts": last_checked_ts
    })

def load_state():
    data = load_json(STATE_FILE, {})
    return data.get("chat_id"), data.get("last_checked_ts")

def strike(text: str) -> str:
    return "".join(ch + "\u0336" for ch in text)

def format_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M %d.%m")

def get_service():
    if not os.path.exists(CREDENTIALS_FILE):
        raise RuntimeError("Not authorized — run /start then /auth first.")
    info  = json.load(open(CREDENTIALS_FILE, encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def fetch_body(msg_id: str) -> str:
    svc = get_service()
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    def walk(parts):
        for p in parts or []:
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                return urlsafe_b64decode(p["body"]["data"]).decode(errors="ignore")
            if p.get("parts"):
                r = walk(p["parts"])
                if r:
                    return r
        return None

    plain = walk(msg.get("payload", {}).get("parts"))
    return (plain or msg.get("snippet", "")).strip()

# ─── ACCESS CONTROL ──────────────────────────────────────────────────────────
admins         = set(load_json(ADMIN_FILE, []))
allowed_groups = set(load_json(ALLOWED_GROUPS_FILE, []))

def save_allowed_groups():
    save_json(ALLOWED_GROUPS_FILE, list(allowed_groups))

def is_admin(user_id: int) -> bool:
    return user_id in admins

def is_authorized(chat_id: int, user_id: int, chat_type: str) -> bool:
    if chat_type == "private":
        return is_admin(user_id)
    if chat_id in allowed_groups:
        return True
    if is_admin(user_id):
        allowed_groups.add(chat_id)
        save_allowed_groups()
        return True
    return False

# ─── INLINE KEYBOARDS ─────────────────────────────────────────────────────────
def kb_read(gid: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Read", callback_data=f"mark_read:{gid}")
    ]])

def kb_wl():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Add",    callback_data="wl_add"),
        InlineKeyboardButton(text="Remove", callback_data="wl_remove"),
    ]])

def kb_confirm_remove():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data="wl_confirm_remove"),
        InlineKeyboardButton(text="No",  callback_data="wl_cancel_remove"),
    ]])

def kb_confirm_start():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data="start_confirm_yes"),
        InlineKeyboardButton(text="No",  callback_data="start_confirm_no"),
    ]])

# ─── COMMAND: /rights ─────────────────────────────────────────────────────────
@dp.message(F.text == "/rights")
async def cmd_rights(msg: Message):
    if is_authorized(msg.chat.id, msg.from_user.id, msg.chat.type):
        await msg.answer("✅ You have access to this bot here.")
    else:
        await msg.answer("❌ You do NOT have access to this bot here.")

# ─── COMMAND: /start (admin only) ────────────────────────────────────────────
@dp.message(F.text == "/start")
async def cmd_start(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    auth_flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRETS,
        SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob"
    )
    auth_url, _ = auth_flow.authorization_url(prompt="consent")

    await msg.answer(
        "🔗 Please open this URL in a browser to authorize Gmail:\n\n"
        f"{auth_url}"
    )
    await msg.answer(
        "📝 After granting access, reply with:\n"
        "`/auth YOUR_CODE_HERE`",
        parse_mode="Markdown"
    )

    _pending_flows[msg.from_user.id] = auth_flow

# ─── COMMAND: /auth CODE ─────────────────────────────────────────────────────
@dp.message(F.text.startswith("/auth "))
async def cmd_auth(msg: Message):
    user_id = msg.from_user.id
    if user_id not in _pending_flows:
        return await msg.reply("❌ No pending authorization. Run /start first.")

    code = msg.text.split(" ", 1)[1].strip()
    flow = _pending_flows.pop(user_id)

    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return await msg.reply(f"❌ Token exchange failed:\n{e}")

    creds = flow.credentials
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # Initialize whitelist and unread-store
    open(WHITELIST_FILE,    "a").close()
    save_json(UNREAD_STORE_FILE, {})

    # Persist state and start polling
    global CHAT_ID, _poll_task, last_checked_ts
    CHAT_ID         = msg.chat.id
    last_checked_ts = int(time.time() * 1000)
    save_state()

    if _poll_task:
        _poll_task.cancel()
    _poll_task = asyncio.create_task(poll_loop())

    await msg.answer("✅ Authorization complete! I'll start notifying you of new emails.")

# ─── CONFIRM RESET AUTH ──────────────────────────────────────────────────────
@dp.callback_query(F.data == "start_confirm_yes")
async def cb_start_yes(q: CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("❌ Only admins.", show_alert=True)

    for fn in (CREDENTIALS_FILE, UNREAD_STORE_FILE):
        try: os.remove(fn)
        except: pass
    await cmd_start(q.message)

@dp.callback_query(F.data == "start_confirm_no")
async def cb_start_no(q: CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("❌ Only admins.", show_alert=True)

    await q.answer("Keeping existing authorization.")
    # If we have valid creds, resume polling
    global _poll_task
    if not _poll_task and os.path.exists(CREDENTIALS_FILE):
        _poll_task = asyncio.create_task(poll_loop())

# ─── COMMAND: /restart (admin only) ──────────────────────────────────────────
@dp.message(F.text == "/restart")
async def cmd_restart(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Only admins can reset the bot.")

    for fn in (CREDENTIALS_FILE, WHITELIST_FILE,
               UNREAD_STORE_FILE, STATE_FILE):
        try: os.remove(fn)
        except: pass
    for t in _reminder_tasks.values():
        t.cancel()
    _reminder_tasks.clear()

    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None

    await msg.answer("🔄 Bot reset. Re-run /start to authorize again.")

# ─── COMMAND: /whitelist ──────────────────────────────────────────────────────
@dp.message(F.text == "/whitelist")
async def cmd_whitelist(msg: Message):
    if not is_authorized(msg.chat.id, msg.from_user.id, msg.chat.type):
        return await msg.reply("❌ You’re not allowed here.")

    global _adding, _removing
    _adding = _removing = False

    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    if not lines:
        text = "Whitelist is empty."
    else:
        text = "\n".join(f"{i+1}. {html.escape(e)}" for i,e in enumerate(lines))

    await msg.answer(text, reply_markup=kb_wl())

@dp.callback_query(F.data == "wl_add")
async def cb_wl_add(q: CallbackQuery):
    if not is_authorized(q.message.chat.id, q.from_user.id, q.message.chat.type):
        return await q.answer("❌ Not allowed here.", show_alert=True)
    global _adding, _removing
    _adding, _removing = True, False
    await q.answer("Send the email or domain to ADD (e.g. user@domain.com or *@domain.com).")

@dp.callback_query(F.data == "wl_remove")
async def cb_wl_remove(q: CallbackQuery):
    if not is_authorized(q.message.chat.id, q.from_user.id, q.message.chat.type):
        return await q.answer("❌ Not allowed here.", show_alert=True)

    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    if not lines:
        return await q.answer("Whitelist is empty.", show_alert=True)

    global _adding, _removing
    _adding, _removing = False, True
    await q.answer("Send the number of the entry to REMOVE.")

@dp.message(lambda m: _adding)
async def on_add(m: Message):
    global _adding
    entry = m.text.strip()
    if "@" not in entry:
        return await m.reply("❌ Invalid format.")
    with open(WHITELIST_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    _adding = False
    await m.reply(f"➕ Added `{html.escape(entry)}`.", parse_mode="Markdown")

@dp.message(lambda m: _removing)
async def on_remove(m: Message):
    global _removing, _remove_index
    idx = m.text.strip()
    if not idx.isdigit():
        return await m.reply("❌ Send a number.")
    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    i = int(idx) - 1
    if i < 0 or i >= len(lines):
        return await m.reply("❌ Invalid index.")
    _remove_index, _removing = i, False
    await m.reply(
        f"Remove `{html.escape(lines[i])}`?",
        reply_markup=kb_confirm_remove(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "wl_confirm_remove")
async def cb_confirm_remove(q: CallbackQuery):
    if not is_authorized(q.message.chat.id, q.from_user.id, q.message.chat.type):
        return await q.answer("❌ Not allowed.", show_alert=True)

    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    removed = lines.pop(_remove_index)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    await q.answer("✅ Removed.")
    await q.message.edit_text(f"❌ Removed `{html.escape(removed)}`.", parse_mode="Markdown")

@dp.callback_query(F.data == "wl_cancel_remove")
async def cb_cancel_remove(q: CallbackQuery):
    if not is_authorized(q.message.chat.id, q.from_user.id, q.message.chat.type):
        return await q.answer("❌ Not allowed.", show_alert=True)
    await q.answer("Cancelled.")

# ─── MARK‐AS‐READ CALLBACK ─────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("mark_read:"))
async def cb_mark_read(q: CallbackQuery):
    if not is_authorized(q.message.chat.id, q.from_user.id, q.message.chat.type):
        return await q.answer("❌ Not allowed.", show_alert=True)

    gid = q.data.split(":", 1)[1]
    svc = get_service()
    svc.users().messages().modify(
        userId="me", id=gid, body={"removeLabelIds": ["UNREAD"]}
    ).execute()

    store = load_json(UNREAD_STORE_FILE, {})
    info  = store.pop(gid, None)
    save_json(UNREAD_STORE_FILE, store)
    task = _reminder_tasks.pop(gid, None)
    if task:
        task.cancel()

    await q.message.edit_text(strike(q.message.text or ""))
    await q.answer("Marked as read.")

# ─── POLLING & REMINDERS ─────────────────────────────────────────────────────
async def poll_loop():
    await asyncio.sleep(3)
    global last_checked_ts

    store = load_json(UNREAD_STORE_FILE, {})

    while True:
        try:
            svc = get_service()

            entries   = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
            email_wl  = {e for e in entries if not e.startswith("*@")}
            domain_wl = {e[2:] for e in entries if e.startswith("*@")}

            resp   = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], q="is:unread"
            ).execute()
            msgs   = resp.get("messages", [])
            new_ts = last_checked_ts

            for m in msgs:
                gid    = m["id"]
                detail = svc.users().messages().get(
                    userId="me", id=gid, format="metadata",
                    metadataHeaders=["From", "Date", "Subject"]
                ).execute()

                ts = int(detail["internalDate"])
                new_ts = max(new_ts, ts)
                if ts <= last_checked_ts:
                    continue

                hdrs        = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
                raw_subject = hdrs.get("Subject", "(no subject)")
                parts       = decode_header(raw_subject)
                subject     = "".join(
                    part.decode(enc or "utf-8") if isinstance(part, bytes) else part
                    for part, enc in parts
                )

                addr   = parseaddr(hdrs["From"])[1]
                domain = addr.split("@", 1)[1]
                allow  = addr in email_wl or domain in domain_wl

                if not allow:
                    store[gid] = {"tg_msg_id": None, "time": time.time()}
                    save_json(UNREAD_STORE_FILE, store)
                    continue

                body = fetch_body(gid)
                text = (
                    f"📧 From: {html.escape(addr)}\n"
                    f"📝 Subject: {html.escape(subject)}\n\n"
                    f"{html.escape(body)}\n"
                    f"{format_ts(ts)}"
                )

                sent = await bot.send_message(
                    CHAT_ID, text,
                    parse_mode="HTML",
                    reply_markup=kb_read(gid)
                )

                store[gid] = {"tg_msg_id": sent.message_id, "time": time.time()}
                save_json(UNREAD_STORE_FILE, store)

                _reminder_tasks[gid] = asyncio.create_task(reminder_loop(gid))

            last_checked_ts = new_ts
            save_state()

        except Exception:
            logger.exception("[poll] error")

        await asyncio.sleep(POLL_INTERVAL)

async def reminder_loop(gid: str):
    await asyncio.sleep(FIRST_REMINDER)
    info = load_json(UNREAD_STORE_FILE, {}).get(gid)
    if info and info.get("tg_msg_id"):
        await bot.send_message(
            CHAT_ID,
            "📌 Reminder: you still have an unread message.",
            reply_to_message_id=info["tg_msg_id"]
        )

    await asyncio.sleep(LOOP_REMINDER)
    info = load_json(UNREAD_STORE_FILE, {}).get(gid)
    if info and info.get("tg_msg_id"):
        await bot.send_message(
            CHAT_ID,
            "📌 Final reminder: please mark that message as read.",
            reply_to_message_id=info["tg_msg_id"]
        )

# ─── COMMAND: /myid ───────────────────────────────────────────────────────────
@dp.message(F.text == "/myid")
async def cmd_myid(m: Message):
    await m.answer(
        f"Chat ID: <code>{m.chat.id}</code>\nUser ID: <code>{m.from_user.id}</code>",
        parse_mode="HTML"
    )

# ─── RUNNER ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def main():
        global CHAT_ID, last_checked_ts, _poll_task
        # load previous state
        CHAT_ID, last_checked_ts = load_state()

        # resume polling if we have valid creds + chat
        if os.path.exists(CREDENTIALS_FILE) and CHAT_ID:
            _poll_task = asyncio.create_task(poll_loop())

        await dp.start_polling(bot)

    asyncio.run(main())
