"""
One-off helper to point Telegram at your deployed webhook (or remove it to
go back to local run_polling()). Run manually after deploying to PythonAnywhere:

    python set_webhook.py set https://<yourusername>.pythonanywhere.com
    python set_webhook.py delete
    python set_webhook.py info
"""
import sys

import requests
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

action = sys.argv[1]

if action == "set":
    base_url = sys.argv[2].rstrip("/")
    resp = requests.post(f"{API}/setWebhook", data={"url": f"{base_url}/webhook/{TOKEN}"})
elif action == "delete":
    resp = requests.post(f"{API}/deleteWebhook")
elif action == "info":
    resp = requests.get(f"{API}/getWebhookInfo")
else:
    print(__doc__)
    sys.exit(1)

print(resp.status_code, resp.json())
