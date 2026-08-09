"""
WSGI adapter that runs the MBSS Telegram bot in webhook mode instead of
run_polling() — needed because PythonAnywhere's free tier only offers a
request-driven web app (no persistent background process for a long-lived
polling loop). PythonAnywhere's WSGI config file should import `flask_app`
from this module and alias it to `application`.

Each webhook call gets its own short-lived Application + event loop via
asyncio.run(), fully contained within that one request. An earlier version
kept one Application alive in a persistent background thread instead, but
uWSGI (which PythonAnywhere's manual-configuration web apps run on) doesn't
give background threads real execution time unless threading support is
explicitly enabled — that version worked once at import time, then silently
stalled on every actual webhook request. Rebuilding per-request costs a bit
of overhead (new handlers + HTTP client each call) but that's a non-issue at
this bot's request volume, and it sidesteps the threading pitfall entirely.
"""
import asyncio
import traceback

from flask import Flask, request
from telegram import Update

from bot import build_app, TELEGRAM_BOT_TOKEN

flask_app = Flask(__name__)

# Bot token doubles as the URL secret — same convention Telegram's own docs use,
# so the webhook path can't be guessed/hit by scanners.
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"


async def _handle_update(payload: dict):
    app = build_app()
    async with app:  # calls app.initialize() on enter, app.shutdown() on exit
        update = Update.de_json(payload, app.bot)
        await app.process_update(update)


@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    payload = request.get_json(force=True)
    print(f"📩 Webhook received: update_id={payload.get('update_id')}")
    try:
        asyncio.run(_handle_update(payload))
    except Exception:
        print("❌ Unhandled error while processing webhook update:")
        traceback.print_exc()
    return "OK"


@flask_app.route("/")
def health():
    return "MBSS bot webhook is running."
