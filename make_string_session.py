import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id = int(os.getenv("TG_API_ID", "0"))
api_hash = os.getenv("TG_API_HASH", "").strip()

if not api_id or not api_hash:
    raise RuntimeError("Missing TG_API_ID or TG_API_HASH in environment")

client = TelegramClient(StringSession(), api_id, api_hash)
client.start()
print("\nTG_STRING_SESSION=")
print(client.session.save())
client.disconnect()
