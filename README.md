
# 🚀 Telegram Gmail Notifier Bot

![(https://img.shields.io/badge/license-MIT-green)](https://img.shields.io/badge/made%20by-WNNC-white)
![Python](https://img.shields.io/badge/python-3.7%2B-blue)
![lib](https://img.shields.io/badge/aiogram-blue)

A lightweight Telegram bot that polls Gmail for unread messages and delivers them straight to your chat. Ideal for real-time notifications, customizable filters, and secure admin control.

---

## 🔑 Features

- Polls Gmail at configurable intervals  
- Filters by sender email or domain whitelist  
- Sends inline “Read” and “Remind Me” buttons  
- Admin-only setup and group lockdown  
- Runs on any system with Python 3.7+

---

## 🛠️ Quick Start

### 1. Clone & Setup Environment

```bash
git clone https://github.com/vavavoom9/GtelegBot
cd GtelegBot
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

If a module is missing, install it manually:

```bash
pip install <module_name>
```

---

## 🔐 Configure Gmail API (`client_secret.json`)

1. Sign in at https://console.cloud.google.com  
2. Create or select a project  
3. Enable the Gmail API  
   - APIs & Services → Library → Search “Gmail API” → Enable  
4. Configure OAuth consent screen  
   - APIs & Services → OAuth consent screen → External → Create  
   - Fill App name, support email, developer email → Save  
5. Create OAuth 2.0 credentials  
   - APIs & Services → Credentials → Create Credentials → OAuth client ID  
   - Application type: Desktop app → Name → Create  
6. Download and rename JSON  
   - Click download on your new credential  
   - Rename file to `client_secret.json`  
   - Place in the project root

---

## 🤖 Add Telegram Bot Token (`APIKEY`)

1. Chat with [@BotFather](https://t.me/BotFather) on Telegram  
2. Send `/newbot` and follow prompts  
3. Copy the token (e.g., `123456789:ABCDefGhIJKlmNoPQRstuVWXyz`)  
4. Open or create a file named `APIKEY` in the project root  
5. Paste the token exactly (no spaces or quotes) and save

---

## ▶️ Usage

OPTIONAL Activate your virtual environment if not already active:

```bash
source .venv/bin/activate
```

Run the bot:

```bash
python main.py
```

Send `/start` from your admin account in Telegram to authorize and begin receiving emails.

---

## Adding a user to admin list
/myid → copy the user id add it to admins.json it should look something like that:
```json
[12345678, 987654321]
```
---
