"""
Database — waitlist entries + conversation sessions.
Uses psycopg3 async directly.
"""
import logging
from typing import Optional

from psycopg_pool import AsyncConnectionPool
from config import config

logger = logging.getLogger(__name__)
_pool: Optional[AsyncConnectionPool] = None


def _pg_url() -> str:
    return config.database_url.replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=_pg_url(),
            min_size=2,
            max_size=20,
            open=False,
        )
        await _pool.open()
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id           SERIAL PRIMARY KEY,
                phone        TEXT UNIQUE NOT NULL,
                handle_type  TEXT,
                name         TEXT,
                email        TEXT,
                phone_number TEXT,
                routes       TEXT,
                state        TEXT NOT NULL DEFAULT 'new',
                signed_up    BOOLEAN NOT NULL DEFAULT FALSE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("SELECT 1")
    logger.info("Waitlist table ready.")


async def get_or_create_session(phone: str, handle_type: str = "phone") -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM waitlist WHERE phone = %s", (phone,))
        row = await cur.fetchone()
        if row is not None:
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))

        # Pre-fill known contact field from handle
        phone_number = phone if handle_type == "phone" else None
        email = phone if handle_type == "email" else None

        await conn.execute(
            """INSERT INTO waitlist (phone, handle_type, phone_number, email, state)
               VALUES (%s, %s, %s, %s, 'new') ON CONFLICT (phone) DO NOTHING""",
            (phone, handle_type, phone_number, email)
        )
        cur2 = await conn.execute("SELECT * FROM waitlist WHERE phone = %s", (phone,))
        row2 = await cur2.fetchone()
        cols2 = [desc[0] for desc in cur2.description]
        return dict(zip(cols2, row2))


async def get_session(phone: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM waitlist WHERE phone = %s", (phone,))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))


async def save_field(phone: str, field: str, value: str):
    allowed = {"name", "email", "phone_number", "home_airport"}
    if field not in allowed:
        raise ValueError(f"Invalid field: {field}")
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            f"UPDATE waitlist SET {field} = %s, updated_at = NOW() WHERE phone = %s",
            (value, phone)
        )


async def mark_complete(phone: str):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE waitlist SET signed_up = TRUE, state = 'complete', updated_at = NOW() WHERE phone = %s",
            (phone,)
        )


async def is_already_signed_up(phone: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT signed_up FROM waitlist WHERE phone = %s", (phone,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])

async def get_total_signups() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM waitlist WHERE signed_up = TRUE")
        row = await cur.fetchone()
        return row[0] if row else 0
