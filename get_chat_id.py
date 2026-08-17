import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

response = requests.get(url, timeout=20)
response.raise_for_status()

data = response.json()

for update in data.get("result", []):
    message = update.get("message")

    if message:
        chat = message["chat"]
        print("CHAT_ID:", chat["id"])
        print("NAME:", chat.get("first_name", ""))
