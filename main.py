"""
Tiaki Waitlist — iMessage via BlueBubbles polling + AI conversation
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
from contextlib import asynccontextmanager
from collections import defaultdict

import httpx
from fastapi import FastAPI

from config import config
from db import init_db, get_or_create_session, save_field, mark_complete, is_already_signed_up
from conversation import get_ai_response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

POLL_INTERVAL = 2

# In-memory conversation history and locks per handle
conversation_history: dict = defaultdict(list)
user_locks: dict = defaultdict(asyncio.Lock)


# ── Send iMessage ─────────────────────────────────────────────────────────────

async def send_imessage(to: str, text: str):
    chat_guid = f"iMessage;-;{to}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{config.bb_url}/api/v1/message/text?password={config.bb_password}",
            json={
                "chatGuid": chat_guid,
                "message": text,
                "method": "apple-script",
                "tempGuid": f"temp-{asyncio.get_event_loop().time()}"
            }
        )
    if resp.status_code == 200:
        logger.info(f"→ {to}: {text[:80]}")
    else:
        logger.error(f"Send failed to {to}: {resp.status_code} {resp.text}")


# ── Process a single message ──────────────────────────────────────────────────

async def process_message(handle: str, text: str):
    async with user_locks[handle]:
        logger.info(f"← {handle}: {text[:60]}")

        handle_type = "phone" if handle.startswith("+") else "email"

        try:
            if await is_already_signed_up(handle):
                await send_imessage(handle, "You're already on the Tiaki waitlist! We'll be in touch when we launch 🎉")
                return

            session = await get_or_create_session(handle, handle_type=handle_type)
            history = conversation_history[handle]

            reply, field, value = await get_ai_response(handle, text, session, history)

            # Update history
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            if len(history) > 20:
                conversation_history[handle] = history[-20:]

            # Persist collected field
            if field and value:
                if field == "complete":
                    await mark_complete(handle)
                    logger.info(f"✅ {handle} signed up to waitlist")
                else:
                    await save_field(handle, field, value)
                    logger.info(f"Saved {field}={value} for {handle}")

            # Send as multiple messages for natural feel
            parts = [p.strip() for p in reply.split("\n") if p.strip()]
            for i, part in enumerate(parts):
                if i > 0:
                    await asyncio.sleep(0.8)
                await send_imessage(handle, part)

        except Exception as e:
            logger.exception(f"Error processing message from {handle}: {e}")
            await send_imessage(handle, "Sorry, something went wrong on my end. Try again in a moment!")


# ── Polling ───────────────────────────────────────────────────────────────────

async def get_messages_since(chat_guid: str, last_rowid: int) -> list:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{config.bb_url}/api/v1/message/query?password={config.bb_password}",
            json={"chatGuid": chat_guid, "limit": 10, "offset": 0, "with": ["chats"]}
        )
    if resp.status_code != 200:
        return []

    messages = resp.json().get("data", [])
    new = []
    for msg in messages:
        rowid = msg.get("originalROWID", 0)
        is_from_me = msg.get("isFromMe", True)
        text = msg.get("text", "")
        handle = msg.get("handle", {}) or {}
        address = handle.get("address", "")
        if rowid > last_rowid and not is_from_me and text and text.strip() and address:
            new.append({"rowid": rowid, "text": text.strip(), "handle": address})

    return sorted(new, key=lambda x: x["rowid"])


async def get_all_active_chats() -> list:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{config.bb_url}/api/v1/chat/query?password={config.bb_password}",
            json={"limit": 100, "offset": 0}
        )
    if resp.status_code != 200:
        return []
    chats = resp.json().get("data", [])
    return [c["guid"] for c in chats if c.get("guid", "").startswith("iMessage;-;")]


async def poll_loop():
    last_rowids: dict = {}

    chats = await get_all_active_chats()
    for chat_guid in chats:
        msgs = await get_messages_since(chat_guid, 0)
        last_rowids[chat_guid] = msgs[-1]["rowid"] if msgs else 0

    logger.info(f"Polling {len(chats)} chats.")

    while True:
        try:
            chats = await get_all_active_chats()
            for chat_guid in chats:
                last = last_rowids.get(chat_guid, 0)
                new_msgs = await get_messages_since(chat_guid, last)
                for msg in new_msgs:
                    last_rowids[chat_guid] = msg["rowid"]
                    asyncio.create_task(process_message(msg["handle"], msg["text"]))
        except Exception as e:
            logger.warning(f"Poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database ready.")
    asyncio.create_task(poll_loop())
    logger.info("Polling started.")
    yield


app = FastAPI(title="Tiaki Waitlist", lifespan=lifespan)

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

from fastapi.responses import RedirectResponse

@app.get("/")
async def root():
    return RedirectResponse(url="/static/landing.html")


@app.get("/health")
async def health():
    return {"status": "ok"}