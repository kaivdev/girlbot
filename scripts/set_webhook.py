from __future__ import annotations

"""Simple script to set Telegram webhook.

Usage:
  python scripts/set_webhook.py

The script now auto-loads `.env` from the project root (or parent dirs)
using python-dotenv, so you don't need to export env vars manually.

Requires env TELEGRAM_BOT_TOKEN, PUBLIC_BASE_URL, WEBHOOK_SECRET.
"""

import os
import sys

import httpx
from dotenv import load_dotenv, find_dotenv


def main() -> None:
    # Auto-load .env (search upwards)
    load_dotenv(find_dotenv(), override=False)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    base = os.environ.get("PUBLIC_BASE_URL")
    secret = os.environ.get("WEBHOOK_SECRET")
    if not token or not base or not secret:
        print("Please set TELEGRAM_BOT_TOKEN, PUBLIC_BASE_URL, WEBHOOK_SECRET", file=sys.stderr)
        sys.exit(1)
    url = f"{base.rstrip('/')}/tg/webhook?secret={secret}"
    api = f"https://api.telegram.org/bot{token}/setWebhook"
    resp = httpx.post(api, json={"url": url})
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
