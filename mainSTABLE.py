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

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── CONFIG & STATE FILES ────────────────────────────────────────────────────
SCOPES            = ["https://www.googleapis.com/auth/gmail.modify"]
CLIENT_SECRETS    = "client_secret.json"
CREDENTIALS_FILE  = "credentials.json"
WHITELIST_FILE    = "whitelist"
UNREAD_STORE_FILE = "unread_store.json"
STATE_FILE        = "state.json"
APIKEY_FILE       = "APIKEY"

POLL_INTERVAL     = 60       # seconds
FIRST_REMINDER    = 60       # seconds (1 min)
LOOP_REMINDER     = 5 * 60   # seconds (5 min)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

for fn in (CLIENT_SECRETS, APIKEY_FILE):
    if not os.path.exists(fn):
        sys.exit(f"Error: {fn} not found.")

bot = Bot(token=open(APIKEY_FILE, encoding="utf-8").read().strip())
dp  = Dispatcher()

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────
CHAT_ID            = None
_poll_task         = None
_reminder_tasks    = {}     # gmail_id → asyncio.Task
last_checked_ts    = 0      # ms since epoch

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
        "chat_id":        CHAT_ID,
        "last_checked_ts": last_checked_ts
    })

def load_state():
    data = load_json(STATE_FILE, {})
    return data.get("chat_id"), data.get("last_checked_ts")

def strike(text: str) -> str:
    return "".join(ch + "\u0336" for ch in text)

def format_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000).strftime("%H:%M %d.%m")

def get_service():
    if not os.path.exists(CREDENTIALS_FILE):
        raise RuntimeError("Not authorized — run /start first.")
    info  = json.load(open(CREDENTIALS_FILE, encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def fetch_body(msg_id: str) -> str:
    svc = get_service()
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    def walk(parts):
        for p in parts or []:
            if p.get("mimeType")=="text/plain" and p.get("body",{}).get("data"):
                return urlsafe_b64decode(p["body"]["data"]).decode(errors="ignore")
            if p.get("parts"):
                r = walk(p["parts"])
                if r: return r
        return None
    plain = walk(msg.get("payload",{}).get("parts"))
    return (plain or msg.get("snippet","")).strip()

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
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

# ─── ON STARTUP: resume polling if possible ──────────────────────────────────
@dp.startup()
async def on_startup():
    global CHAT_ID, last_checked_ts, _poll_task

    # load stored state
    cid, ts = load_state()
    # if credentials exist and we have a chat_id, resume
    if os.path.exists(CREDENTIALS_FILE) and cid:
        CHAT_ID         = cid
        last_checked_ts = ts or int(time.time()*1000)
        logger.info(f"[startup] Resuming polling for chat_id={CHAT_ID}")
        _poll_task = asyncio.create_task(poll_loop())

# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(F.text == "/start")
async def cmd_start(msg: Message):
    global CHAT_ID, _poll_task, last_checked_ts
    CHAT_ID = msg.chat.id

    # if already bound, ask to rebind
    if os.path.exists(CREDENTIALS_FILE):
        info  = json.load(open(CREDENTIALS_FILE, encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.valid:
            await msg.answer(
                "Already authorized. Change linked Gmail account?",
                reply_markup=kb_confirm_start()
            )
            return

    # OAuth flow
    flow  = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
    creds = flow.run_local_server(open_browser=True)
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # ensure files
    open(WHITELIST_FILE,   "a").close()
    save_json(UNREAD_STORE_FILE, {})

    # reset so only FUTURE messages trigger
    last_checked_ts = int(time.time()*1000)
    save_state()

    await msg.answer("✅ Authorized! I'll DM you new whitelisted emails.")

    # start polling
    if _poll_task:
        _poll_task.cancel()
    _poll_task = asyncio.create_task(poll_loop())

# ─── Confirm /start rebind ────────────────────────────────────────────────────
@dp.callback_query(F.data == "start_confirm_yes")
async def cb_start_yes(q: CallbackQuery):
    await q.answer()
    for fn in (CREDENTIALS_FILE, UNREAD_STORE_FILE):
        try: os.remove(fn)
        except: pass
    # rerun start
    await cmd_start(q.message)

@dp.callback_query(F.data == "start_confirm_no")
async def cb_start_no(q: CallbackQuery):
    await q.answer("Keeping existing authorization.")
    # persist chat and ts, then start polling if not already
    save_state()
    if not _poll_task:
        asyncio.create_task(poll_loop())

# ─── /restart ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "/restart")
async def cmd_restart(msg: Message):
    for fn in (CREDENTIALS_FILE, WHITELIST_FILE, UNREAD_STORE_FILE, STATE_FILE):
        try: os.remove(fn)
        except: pass
    for t in _reminder_tasks.values():
        t.cancel()
    _reminder_tasks.clear()
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None
    await msg.answer("🔄 Restarted; cleared all data.")

# ─── /whitelist ───────────────────────────────────────────────────────────────
@dp.message(F.text == "/whitelist")
async def cmd_whitelist(msg: Message):
    global _adding, _removing
    _adding = _removing = False
    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    text  = ("Whitelist is empty." if not lines
             else "\n".join(f"{i+1}. {html.escape(e)}" for i,e in enumerate(lines)))
    await msg.answer(text, reply_markup=kb_wl(), parse_mode="HTML")

@dp.callback_query(F.data == "wl_add")
async def qb_wl_add(q: CallbackQuery):
    global _adding, _removing
    _adding, _removing = True, False
    await q.answer()
    await q.message.answer(
        "Send exact email (user@domain.com) or wildcard `*@domain.com` to add:",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "wl_remove")
async def qb_wl_remove(q: CallbackQuery):
    global _adding, _removing
    _adding, _removing = False, True
    await q.answer()
    await q.message.answer("Send the number of the entry to remove:", parse_mode="HTML")

@dp.message(lambda m: _adding)
async def on_add(m: Message):
    global _adding
    entry = m.text.strip()
    if "@" not in entry:
        return await m.answer("✖ Invalid format.")
    with open(WHITELIST_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    _adding = False
    await m.answer(f"➕ Added <code>{html.escape(entry)}</code>.", parse_mode="HTML")

@dp.message(lambda m: _removing)
async def on_remove(m: Message):
    global _removing, _remove_index
    idx = m.text.strip()
    if not idx.isdigit():
        return await m.answer("✖ Send a number.")
    lines = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    i     = int(idx) - 1
    if i<0 or i>=len(lines):
        return await m.answer("✖ Invalid index.")
    _remove_index, _removing = i, False
    await m.answer(
        f"Remove <code>{html.escape(lines[i])}</code>?",
        reply_markup=kb_confirm_remove(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "wl_confirm_remove")
async def qb_confirm_remove(q: CallbackQuery):
    global _remove_index
    lines   = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
    removed = lines.pop(_remove_index)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    _remove_index = None
    await q.answer("✅ Removed.")
    await q.message.edit_text(
        f"❌ Removed <code>{html.escape(removed)}</code>.", parse_mode="HTML"
    )

@dp.callback_query(F.data == "wl_cancel_remove")
async def qb_cancel_remove(q: CallbackQuery):
    await q.answer("Cancelled.")
    await q.message.edit_text("🚫 Removal cancelled.")

# ─── POLLING & NOTIFICATION ───────────────────────────────────────────────────
async def poll_loop():
    await asyncio.sleep(3)
    store = load_json(UNREAD_STORE_FILE, {})

    global last_checked_ts
    while True:
        logger.info(f"[poll] at {datetime.now().isoformat()} ts={last_checked_ts}")
        try:
            svc = get_service()
            # build whitelists
            entries         = open(WHITELIST_FILE, encoding="utf-8").read().splitlines()
            email_wl        = set()
            domain_wl       = set()
            for e in entries:
                if e.startswith("*@"):
                    domain_wl.add(e[2:])
                else:
                    email_wl.add(e)

            resp = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], q="is:unread"
            ).execute()
            msgs = resp.get("messages", [])
            logger.info(f"[poll] unread={len(msgs)}")

            new_ts = last_checked_ts
            for m in msgs:
                gid = m["id"]
                detail = svc.users().messages().get(
                    userId="me", id=gid, format="metadata",
                    metadataHeaders=["From","Date"]
                ).execute()
                ts = int(detail["internalDate"])
                new_ts = max(new_ts, ts)
                if ts <= last_checked_ts:
                    logger.info(f" skip old {gid}")
                    continue

                hdrs   = {h["name"]:h["value"] for h in detail["payload"]["headers"]}
                addr   = parseaddr(hdrs["From"])[1]
                domain = addr.split("@",1)[1]
                allow  = addr in email_wl or domain in domain_wl
                logger.info(f" {gid} from {addr} => {'ALLOW' if allow else 'BLOCK'}")

                if not allow:
                    store[gid] = {"tg_msg_id": None, "time": time.time()}
                    save_json(UNREAD_STORE_FILE, store)
                    continue

                body = fetch_body(gid)
                text = (f"📧 {html.escape(addr)}\n"
                        f"{html.escape(body)}\n"
                        f"{format_ts(ts)}")
                sent = await bot.send_message(
                    CHAT_ID, text, parse_mode="HTML", reply_markup=kb_read(gid)
                )
                logger.info(f" sent DM id={sent.message_id}")

                store[gid] = {"tg_msg_id": sent.message_id, "time": time.time()}
                save_json(UNREAD_STORE_FILE, store)
                _reminder_tasks[gid] = asyncio.create_task(reminder_loop(gid))

            last_checked_ts = new_ts
            save_state()
            logger.info(f"[poll] done, ts={last_checked_ts}\n")

        except Exception:
            logger.exception("[poll] error")

        await asyncio.sleep(POLL_INTERVAL)

async def reminder_loop(gid: str):
    # 1st follow-up after FIRST_REMINDER
    await asyncio.sleep(FIRST_REMINDER)
    store = load_json(UNREAD_STORE_FILE, {})
    info  = store.get(gid)
    if not info:
        return
    tg_id = info["tg_msg_id"]
    if tg_id:
        await bot.send_message(
            CHAT_ID,
            "you have unread message",
            reply_to_message_id=tg_id
        )

    # 2nd (and final) follow-up after LOOP_REMINDER
    await asyncio.sleep(LOOP_REMINDER)
    store = load_json(UNREAD_STORE_FILE, {})
    info  = store.get(gid)
    if not info:
        return
    tg_id = info["tg_msg_id"]
    if tg_id:
        await bot.send_message(
            CHAT_ID,
            "you have unread message",
            reply_to_message_id=tg_id
        )
    # no further reminders

# ─── MARK READ ────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("mark_read:"))
async def cb_mark_read(q: CallbackQuery):
    gid   = q.data.split(":",1)[1]
    store = load_json(UNREAD_STORE_FILE, {})
    info  = store.get(gid)
    if not info:
        await q.answer()
        return

    orig = q.message.text or ""
    await q.message.edit_text(strike(orig))

    task = _reminder_tasks.pop(gid, None)
    if task:
        task.cancel()

    store.pop(gid, None)
    save_json(UNREAD_STORE_FILE, store)
    await q.answer("Marked as read.")

# ─── RUNNER ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
