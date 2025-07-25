import os
import sys
import json
import asyncio
import logging
import time
import html
from datetime import datetime
from email.utils import parseaddr

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── FAIL-FAST CHECKS ───────────────────────────────────────────────────────
CLIENT_SECRETS    = "client_secret.json"
CREDENTIALS_FILE  = "credentials.json"
APIKEY_FILE       = "APIKEY"

if not os.path.exists(CLIENT_SECRETS):
    sys.exit("Error: client_secret.json missing. Place your OAuth Desktop JSON here.")
if not os.path.exists(APIKEY_FILE):
    sys.exit("Error: APIKEY file missing. Put your bot token inside.")

# ─── CONFIG ─────────────────────────────────────────────────────────────────
SCOPES        = ["https://www.googleapis.com/auth/gmail.modify"]
POLL_INTERVAL = 60  # seconds

BOT_TOKEN = open(APIKEY_FILE, "r", encoding="utf-8").read().strip()
bot       = Bot(token=BOT_TOKEN)
dp        = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
last_checked_ts            = 0
CHAT_ID                    = None
last_notification_message  = None

# ─── GMAIL HELPERS ───────────────────────────────────────────────────────────
def load_credentials() -> Credentials | None:
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    info = json.load(open(CREDENTIALS_FILE, encoding="utf-8"))
    return Credentials.from_authorized_user_info(info, SCOPES)

def save_credentials(creds: Credentials):
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

def build_gmail_service():
    creds = load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

async def build_main_menu_text() -> str:
    service = build_gmail_service()
    resp = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=1
    ).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return "📭 Inbox is empty."

    msg_id = msgs[0]["id"]
    detail = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From","Subject","Date"]
    ).execute()

    hdrs    = {h["name"]:h["value"] for h in detail["payload"]["headers"]}
    author_h, author_e = parseaddr(hdrs.get("From",""))
    author_display     = html.escape(author_h or author_e)

    subj = html.escape(hdrs.get("Subject","(no subject)"))

    ts_raw = int(detail["internalDate"]) / 1000.0
    dt     = datetime.fromtimestamp(ts_raw)
    time_str = dt.strftime("%H:%M %d.%m")

    # link to Gmail web view
    url   = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
    time_link = f'<a href="{url}">{time_str}</a>'

    return (
        f"Last email:\n"
        f"{author_display}\n"
        f"{subj}\n"
        f"{time_link}"
    )

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Clear binding", callback_data="clear_binding"),
        InlineKeyboardButton(text="Read",           callback_data="read"),
        InlineKeyboardButton(text="Clear",          callback_data="clear_unread"),
        InlineKeyboardButton(text="Refresh now",    callback_data="refresh_now"),
    ]])

def confirm_unbind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, unbind", callback_data="confirm_unbind"),
        InlineKeyboardButton(text="Cancel",      callback_data="cancel_unbind"),
    ]])

def notify_kb(msg_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Read", callback_data=f"read:{msg_id}"),
        InlineKeyboardButton(text="Clear", callback_data="clear_unread"),
    ]])

# ─── /start ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    global last_checked_ts, CHAT_ID
    CHAT_ID = message.chat.id

    creds = load_credentials()
    if not creds or not creds.valid:
        await message.answer("🔐 Opening browser for Gmail auth…")
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
        creds = flow.run_local_server(open_browser=True)
        save_credentials(creds)

    menu_text = await build_main_menu_text()
    await message.answer(menu_text, parse_mode="HTML", reply_markup=main_kb())

    last_checked_ts = int(time.time() * 1000)
    asyncio.create_task(poll_loop())

# ─── POLLING LOOP ────────────────────────────────────────────────────────────
async def poll_loop():
    global last_checked_ts, last_notification_message
    service = build_gmail_service()

    while True:
        try:
            resp = service.users().messages().list(
                userId="me", labelIds=["INBOX"], q="is:unread"
            ).execute()
            msgs = resp.get("messages", [])
            total_unread = len(msgs)

            new_details = []
            for m in msgs:
                d = service.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From","Date"]
                ).execute()
                ts = int(d["internalDate"])
                if ts > last_checked_ts:
                    new_details.append(d)

            for d in sorted(new_details, key=lambda x: x["internalDate"]):
                hdrs   = {h["name"]:h["value"] for h in d["payload"]["headers"]}
                sender = parseaddr(hdrs.get("From",""))[0] or parseaddr(hdrs.get("From",""))[1]
                sender = html.escape(sender)

                if last_notification_message:
                    try:
                        await bot.delete_message(CHAT_ID, last_notification_message)
                    except:
                        pass

                msg = await bot.send_message(
                    CHAT_ID,
                    f"📧 New email from {sender}\nTotal unread: <b>{total_unread}</b>",
                    parse_mode="HTML",
                    reply_markup=notify_kb(d["id"])
                )
                last_notification_message = msg.message_id

            if new_details:
                last_checked_ts = max(int(d["internalDate"]) for d in new_details)

        except Exception:
            logger.exception("Polling error")

        await asyncio.sleep(POLL_INTERVAL)

# ─── CALLBACK: Read specific ─────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("read:"))
async def on_read_specific(query: CallbackQuery):
    await query.answer()
    _, gmail_id = query.data.split(":", 1)
    service = build_gmail_service()

    detail = service.users().messages().get(
        userId="me",
        id=gmail_id,
        format="metadata",
        metadataHeaders=["From","Subject","Date"]
    ).execute()
    hdrs = {h["name"]:h["value"] for h in detail["payload"]["headers"]}

    # parse author
    name, addr = parseaddr(hdrs.get("From",""))
    display = html.escape(name or addr)

    # build buttons: [Author name] → callback shows email, [🔍 see more] → URL, then main menu
    base_rows = main_kb().inline_keyboard
    keyboard  = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=display, callback_data=f"show_email:{addr}")],
            [InlineKeyboardButton(text="🔍 see more", url=f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}")],
        ] + base_rows
    )

    subj = html.escape(hdrs.get("Subject","(no subject)"))
    ts = int(detail["internalDate"]) / 1000.0
    dt = datetime.fromtimestamp(ts)
    time_str = dt.strftime("%H:%M %d.%m")

    text = f"{display}\n{subj}\n{time_str}"
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

# ─── CALLBACK: show raw email on author tap ──────────────────────────────────
@dp.callback_query(F.data.startswith("show_email:"))
async def on_show_email(query: CallbackQuery):
    await query.answer(query.data.split(":",1)[1], show_alert=True)

# ─── CALLBACK: Read all fallback, Clear, Refresh, Unbind ────────────────────
@dp.callback_query(F.data == "read")
async def on_read_all(query: CallbackQuery):
    await query.answer()
    service = build_gmail_service()
    resp = service.users().messages().list(
        userId="me", labelIds=["INBOX"], q="is:unread"
    ).execute()
    msgs = resp.get("messages", [])

    lines = []
    for m in msgs:
        d = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From","Date"]
        ).execute()
        hdrs   = {h["name"]:h["value"] for h in d["payload"]["headers"]}
        sender = parseaddr(hdrs.get("From",""))[0] or parseaddr(hdrs.get("From",""))[1]
        sender = html.escape(sender)
        dt     = html.escape(hdrs.get("Date",""))
        url    = f"https://mail.google.com/mail/u/0/#inbox/{m['id']}"
        lines.append(f'<a href="{url}">{dt}</a> – {sender}')

    text = "\n".join(lines) if lines else "No unread emails."
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "clear_unread")
async def on_clear_all(query: CallbackQuery):
    await query.answer()
    service = build_gmail_service()
    resp = service.users().messages().list(
        userId="me", labelIds=["INBOX"], q="is:unread"
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if ids:
        service.users().messages().batchModify(
            userId="me", body={"ids": ids, "removeLabelIds": ["UNREAD"]}
        ).execute()
    text = await build_main_menu_text()
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "refresh_now")
async def on_refresh(query: CallbackQuery):
    await query.answer("Refreshing…")
    text = await build_main_menu_text()
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "clear_binding")
async def on_clear_binding(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "⚠ Are you sure you want to unbind Gmail?",
        reply_markup=confirm_unbind_kb()
    )

@dp.callback_query(F.data == "cancel_unbind")
async def on_cancel_unbind(query: CallbackQuery):
    await query.answer("Cancelled")
    text = await build_main_menu_text()
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "confirm_unbind")
async def on_confirm_unbind(query: CallbackQuery):
    await query.answer("Unbinding…")
    creds = load_credentials()
    if creds and creds.token:
        requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": creds.token},
            headers={"content-type":"application/x-www-form-urlencoded"}
        )
    if os.path.exists(CREDENTIALS_FILE):
        os.remove(CREDENTIALS_FILE)

    await query.message.edit_text(
        "🔓 Gmail unbound. Use /start to authorize again.",
        reply_markup=None
    )

# ─── RUNNER ─────────────────────────────────────────────────────────────────
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
