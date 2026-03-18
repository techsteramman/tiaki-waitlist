"""
AI-powered waitlist conversation.
Uses GPT-5-nano to handle the conversation naturally.
"""
import json
import logging
from typing import Optional
from openai import AsyncOpenAI
from config import config

logger = logging.getLogger(__name__)
client = AsyncOpenAI(api_key=config.openai_api_key, timeout=20.0, max_retries=1)

SYSTEM_PROMPT = """You are the Tiaki assistant. Tiaki is an AI-powered flight booking agent that works entirely over iMessage. You text where you want to fly and it handles everything: searching, booking, payment, confirmation. No apps, no websites.

Your job is to collect exactly three things from the user, in this order:
1. Their name
2. The contact info you don't already have (see context below)
3. Their home airport

Rules:
- Ask ONE question at a time
- Collect ONLY name, contact info, and home airport. Nothing else
- Once you have all three, tell them they are on the waitlist with some warmth and excitement, then stop
- Only mention the waitlist AFTER you have all three pieces of info
- On the very first message, give a warm friendly intro to Tiaki in 1-2 short sentences, then ask for their name
- Never repeat any phrase you have already used in this conversation
- Never say "Nice to meet you" more than once
- Short messages, iMessage style, warm and conversational like a real person texting
- Genuine enthusiasm without being over the top
- No dashes or em dashes
- Split responses into short lines, each thought on its own line

About Tiaki:
- Books flights via iMessage, just text where you want to go
- Handles search, booking, payment, and confirmation
- Remembers your preferences like cabin class, seat, airlines
- Launching soon
- Named after the Maori word for guardian

IMPORTANT data collection:
- The context will tell you what contact info you already have
- If handle_type is phone: you already have their phone number, only ask for email
- If handle_type is email: you already have their email, only ask for phone number
- When asking for home airport, ask naturally like "What airport do you usually fly out of?"
- Output a JSON block after your message when you collect a field:
  {"_save": {"field": "name|email|phone_number|home_airport", "value": "..."}}
- When all three are collected and confirmed output:
  {"_save": {"field": "complete", "value": "true"}}
- One _save block per message, never show it to the user

Use this as your first message:
Hey! Tiaki is your personal flight agent over iMessage. Just tell me where you want to fly and I'll take care of searching, booking, and even confirming your flights.
What's your name?"""


async def get_ai_response(
    phone: str,
    message: str,
    session: dict,
    history: list
) -> tuple[str, Optional[str], Optional[str]]:
    """Returns (reply_text, field_to_save, value_to_save)."""

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Tell the model what we already have
    known = []
    handle_type = session.get("handle_type", "phone")

    if handle_type == "phone":
        known.append(f"Phone number already known: {phone} (do NOT ask for phone)")
    else:
        known.append(f"Email already known: {phone} (do NOT ask for email)")

    if session.get("name"):
        known.append(f"Name already collected: {session['name']}")
    if session.get("email"):
        known.append(f"Email already collected: {session['email']}")
    if session.get("phone_number"):
        known.append(f"Phone already collected: {session['phone_number']}")
    if session.get("home_airport"):
        known.append(f"Home airport already collected: {session['home_airport']}")

    known.append(f"handle_type: {handle_type}")

    messages.append({
        "role": "system",
        "content": "Context for this user: " + " | ".join(known)
    })

    messages.extend(history)
    messages.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model="gpt-5-nano",
        messages=messages,
        max_completion_tokens=2000,
        reasoning_effort="low",
    )

    full_response = response.choices[0].message.content or ""
    logger.info(f"AI raw response: {repr(full_response[:300])}")

    field = None
    value = None
    clean_lines = []

    for line in full_response.strip().split("\n"):
        stripped = line.strip()
        if '_save' in stripped and stripped.startswith('{'):
            try:
                data = json.loads(stripped)
                save = data.get("_save", {})
                field = save.get("field")
                value = save.get("value")
                logger.info(f"Parsed _save: field={field} value={value}")
            except Exception as e:
                logger.warning(f"Failed to parse _save line: {stripped!r} error={e}")
        else:
            clean_lines.append(line)

    reply = "\n".join(clean_lines).strip()
    logger.info(f"Final field={field} value={value} reply_len={len(reply)}")

    if not reply:
        reply = "Sorry, something went wrong. Could you say that again?"

    return reply, field, value