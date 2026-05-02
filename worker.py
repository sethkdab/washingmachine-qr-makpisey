import asyncio
import os
import re

import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION_NAME = os.getenv("TG_SESSION_NAME", "aba_listener").strip()
TG_STRING_SESSION = os.getenv("TG_STRING_SESSION", "").strip()

ABA_GROUP_ID = int(os.getenv("ABA_GROUP_ID", "-1002522488273"))
ABA_BOT_ID = int(os.getenv("ABA_BOT_ID", "1148497258"))

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:5000").rstrip("/")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "change-me").strip()
GROUP_MACHINE_CODE = os.getenv("GROUP_MACHINE_CODE", "WM-01").strip()

if not TG_API_ID or not TG_API_HASH:
    raise RuntimeError("Missing TG_API_ID or TG_API_HASH")

ABA_PATTERN = re.compile(
    r"^\$(?P<amount>\d+(?:\.\d+)?) paid by "
    r"(?P<payer_name>.+?) \((?P<masked_account>[^)]+)\) on "
    r"(?P<date_text>.+?) via "
    r"(?P<payment_method>.+?) at "
    r"(?P<merchant>.+?) by "
    r"(?P<receiver>.+?)\. Trx\. ID: "
    r"(?P<trx_id>\d+), APV: "
    r"(?P<apv>\d+)\.$"
)

session_source = StringSession(TG_STRING_SESSION) if TG_STRING_SESSION else TG_SESSION_NAME
client = TelegramClient(session_source, TG_API_ID, TG_API_HASH)


def parse_aba_message(text):
    cleaned = (text or "").strip()
    match = ABA_PATTERN.match(cleaned)
    if not match:
        return None
    parsed = match.groupdict()
    parsed["raw_text"] = cleaned
    return parsed


def send_to_backend(parsed, chat_id, sender_id):
    payload = {
        "machine_code": GROUP_MACHINE_CODE,
        "trx_id": parsed["trx_id"],
        "amount": parsed["amount"],
        "raw_text": parsed["raw_text"],
        "parsed": {
            "payer_name": parsed["payer_name"],
            "masked_account": parsed["masked_account"],
            "date_text": parsed["date_text"],
            "payment_method": parsed["payment_method"],
            "merchant": parsed["merchant"],
            "receiver": parsed["receiver"],
            "apv": parsed["apv"],
        },
        "source_chat_id": chat_id,
        "source_sender_id": sender_id,
    }

    response = requests.post(
        f"{BACKEND_URL}/internal/payment-events",
        json=payload,
        headers={"X-Internal-Secret": INTERNAL_API_SECRET},
        timeout=20,
    )
    print("Backend status:", response.status_code, flush=True)
    print("Backend body:", response.text, flush=True)


@client.on(events.NewMessage(chats=ABA_GROUP_ID))
async def on_message(event):
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None)
    chat_id = event.chat_id
    text = event.raw_text or ""

    print("chat_id:", chat_id, flush=True)
    print("sender_id:", sender_id, flush=True)
    print("text:", text, flush=True)

    if sender_id != ABA_BOT_ID:
        print("Ignored: wrong sender", flush=True)
        return

    parsed = parse_aba_message(text)
    if not parsed:
        print("Ignored: regex did not match", flush=True)
        return

    print("Parsed:", parsed, flush=True)
    send_to_backend(parsed, chat_id, sender_id)


async def main():
    print("Starting worker...", flush=True)
    print("ABA_GROUP_ID:", ABA_GROUP_ID, flush=True)
    print("ABA_BOT_ID:", ABA_BOT_ID, flush=True)
    print("GROUP_MACHINE_CODE:", GROUP_MACHINE_CODE, flush=True)
    print("BACKEND_URL:", BACKEND_URL, flush=True)
    print("SESSION_MODE:", "StringSession" if TG_STRING_SESSION else f"FileSession({TG_SESSION_NAME})", flush=True)
    await client.start()
    me = await client.get_me()
    print("Logged in as:", getattr(me, "username", None), getattr(me, "id", None), flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Worker stopped", flush=True)
