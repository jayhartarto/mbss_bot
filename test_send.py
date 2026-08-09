"""
Standalone sanity check, run directly in a PythonAnywhere console (not through
Flask/webhook_app.py) to isolate whether outbound calls to Telegram's API work
at all from this environment, independent of our threading/webhook bridging:

    python test_send.py
"""
import asyncio
import os

from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


async def main():
    bot = Bot(TOKEN)
    print("Calling initialize()...")
    await bot.initialize()
    print("initialize() OK, calling get_me()...")
    me = await bot.get_me()
    print(f"get_me() OK: {me.username}")
    print("Calling send_message()...")
    await bot.send_message(chat_id=CHAT_ID, text="Test message from PythonAnywhere console.")
    print("send_message() OK.")


asyncio.run(main())
