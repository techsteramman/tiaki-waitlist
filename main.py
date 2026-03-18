"""
Tiaki Waitlist — iMessage via BlueBubbles webhook + typing indicators
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import config
from db import init_db, get_or_create_session, save_field, mark_complete, is_already_signed_up, get_total_signups
from conversation import get_ai_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_SIGNUPS = 500
MAX_CONCURRENT = 10
MAX_OUTBOUND_PER_MIN = 20

conversation_history: dict = defaultdict(list)
user_locks: dict = defaultdict(asyncio.Lock)
processing_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

_outbound_count = 0
_outbound_window_start = time.time()
_outbound_lock = asyncio.Lock()


# ── Send iMessage ─────────────────────────────────────────────────────────────

async def send_imessage(to: str, text: str):
    global _outbound_count, _outbound_window_start
    async with _outbound_lock:
        now = time.time()
        if now - _outbound_window_start >= 60:
            _outbound_window_start = now
            _outbound_count = 0
        if _outbound_count >= MAX_OUTBOUND_PER_MIN:
            wait = 60 - (now - _outbound_window_start)
            logger.info(f"Rate limit hit — waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            _outbound_window_start = time.time()
            _outbound_count = 0
        _outbound_count += 1

    chat_guid = f"iMessage;-;{to}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{config.bb_url}/api/v1/message/text?password={config.bb_password}",
            json={"chatGuid": chat_guid, "message": text, "method": "apple-script",
                  "tempGuid": f"temp-{asyncio.get_event_loop().time()}"}
        )
    if resp.status_code == 200:
        logger.info(f"→ {to}: {text[:80]}")
    else:
        logger.error(f"Send failed to {to}: {resp.status_code} {resp.text}")


# ── Typing indicator ──────────────────────────────────────────────────────────

async def send_typing(to: str, active: bool = True):
    chat_guid = f"iMessage;-;{to}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{config.bb_url}/api/v1/chat/{chat_guid}/typing?password={config.bb_password}",
                json={"active": active}
            )
    except Exception as e:
        logger.warning(f"Typing indicator failed: {e}")


# ── Process message ───────────────────────────────────────────────────────────

async def process_message(handle: str, text: str):
    async with processing_semaphore:
        async with user_locks[handle]:
            logger.info(f"← {handle}: {text[:60]}")
            handle_type = "phone" if handle.startswith("+") else "email"

            try:
                if await is_already_signed_up(handle):
                    await send_imessage(handle, "You're already on the Tiaki waitlist! We'll be in touch when we launch 🎉")
                    return

                total = await get_total_signups()
                if total >= MAX_SIGNUPS:
                    await send_imessage(handle, "We've hit capacity for now! Keep an eye on tiakiai.com and we'll open up more spots soon.")
                    return

                session = await get_or_create_session(handle, handle_type=handle_type)
                history = conversation_history[handle]

                await send_typing(handle, True)
                await asyncio.sleep(1.2)

                reply, field, value = await get_ai_response(handle, text, session, history)

                await send_typing(handle, False)

                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                if len(history) > 20:
                    conversation_history[handle] = history[-20:]

                if field and value:
                    if field == "complete":
                        await mark_complete(handle)
                        logger.info(f"✅ {handle} signed up (total: {total + 1})")
                    else:
                        await save_field(handle, field, value)
                        logger.info(f"Saved {field}={value} for {handle}")

                parts = [p.strip() for p in reply.split("\n") if p.strip()]
                for i, part in enumerate(parts):
                    if i > 0:
                        await send_typing(handle, True)
                        await asyncio.sleep(0.6)
                        await send_typing(handle, False)
                    await send_imessage(handle, part)

            except Exception as e:
                logger.exception(f"Error processing message from {handle}: {e}")
                await send_typing(handle, False)
                await send_imessage(handle, "Sorry, something went wrong. Try again in a moment!")


# ── Webhook registration ──────────────────────────────────────────────────────

async def register_webhook():
    public_url = getattr(config, 'public_url', 'http://localhost:8001')
    target = f"{public_url}/webhook/bluebubbles"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{config.bb_url}/api/v1/webhook?password={config.bb_password}"
            )
            if resp.status_code == 200:
                existing = resp.json().get("data", [])
                for w in existing:
                    if w.get("url") == target:
                        logger.info(f"Webhook already registered: {target}")
                        return
            resp = await client.post(
                f"{config.bb_url}/api/v1/webhook?password={config.bb_password}",
                json={"url": target, "events": ["new-message"]}
            )
            if resp.status_code == 200:
                logger.info(f"Webhook registered: {target}")
            else:
                logger.warning(f"Webhook registration failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Could not register webhook: {e}")


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database ready.")
    await register_webhook()
    yield


app = FastAPI(title="Tiaki Waitlist", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/static/landing.html")


@app.post("/webhook/bluebubbles")
async def bluebubbles_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    if body.get("type") != "new-message":
        return JSONResponse({"ok": True})

    data = body.get("data", {})
    if data.get("isFromMe", True):
        return JSONResponse({"ok": True})

    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": True})

    handle = (data.get("handle") or {}).get("address", "")
    if not handle:
        return JSONResponse({"ok": True})

    asyncio.create_task(process_message(handle, text))
    return JSONResponse({"ok": True})


@app.post("/waitlist/join")
async def join_waitlist(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    home_airport = data.get("home_airport", "").strip().upper()

    if not name or not email or not home_airport:
        return JSONResponse({"error": "Missing fields"}, status_code=400)

    total = await get_total_signups()
    if total >= MAX_SIGNUPS:
        return JSONResponse({"error": "At capacity"}, status_code=503)

    session = await get_or_create_session(email, handle_type="email")
    await save_field(email, "name", name)
    await save_field(email, "email", email)
    await save_field(email, "routes", home_airport)
    await mark_complete(email)
    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    total = await get_total_signups()
    return {"status": "ok", "signups": total}