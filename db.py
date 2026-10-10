"""PostgreSQL access (pg8000 - pure Python, no compilation needed).

Each worker thread keeps its own connection instead of opening a new SSL
connection for every query.
"""
import asyncio
import logging
import os
import threading
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.dbapi
import pg8000.exceptions

logger = logging.getLogger(__name__)
_local = threading.local()
_CONNECTION_ERRORS = (pg8000.exceptions.InterfaceError, OSError)


def _connect():
    url = urlparse(os.environ["DATABASE_URL"])
    sslmode = parse_qs(url.query).get("sslmode", [""])[0]
    return pg8000.dbapi.connect(
        host=url.hostname,
        port=url.port or 5432,
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        database=url.path.lstrip("/"),
        ssl_context=None if sslmode == "disable" else True,
    )


def _discard():
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def run(work):
    """Run work(cursor) in one transaction and return its result.

    Reconnects and retries once if the connection was dropped (servers close idle connections).
    """
    for attempt in range(2):
        if getattr(_local, "conn", None) is None:
            _local.conn = _connect()
        conn = _local.conn
        try:
            result = work(conn.cursor())
            conn.commit()
            return result
        except _CONNECTION_ERRORS:
            _discard()
            if attempt:
                raise
            logger.warning("DB connection lost, reconnecting")
        except Exception:
            try:
                conn.rollback()
            except Exception:
                _discard()
            raise


def rows(cur):
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetchall(query, params=()):
    def work(cur):
        cur.execute(query, params)
        return rows(cur)
    return run(work)


def fetchone(query, params=()):
    result = fetchall(query, params)
    return result[0] if result else None


def execute(query, params=()):
    run(lambda cur: cur.execute(query, params))


async def call(fn, *args):
    """Run a blocking DB function in the thread pool."""
    return await asyncio.to_thread(fn, *args)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        first_name VARCHAR(255),
        username VARCHAR(255),
        profile_summary TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS memory TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_cursor BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_updated_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS followups_enabled BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS checkin_asked BOOLEAN NOT NULL DEFAULT FALSE",
    """CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        role VARCHAR(20) NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'chat'",
    "CREATE INDEX IF NOT EXISTS messages_user_id_id_idx ON messages (user_id, id)",
    """CREATE TABLE IF NOT EXISTS memory_versions (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS reminders (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        kind VARCHAR(10) NOT NULL,
        due_at TIMESTAMPTZ NOT NULL,
        daily_time VARCHAR(5),
        purpose TEXT NOT NULL DEFAULT '',
        requested_by VARCHAR(10) NOT NULL DEFAULT 'frankl',
        status VARCHAR(12) NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        sent_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS reminders_status_due_idx ON reminders (status, due_at)",
    # --- several advisors: every conversation, memory and reminder belongs to one persona ---
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS persona VARCHAR(20) NOT NULL DEFAULT 'frankl'",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS speaker VARCHAR(20)",
    "CREATE INDEX IF NOT EXISTS messages_user_persona_id_idx ON messages (user_id, persona, id)",
    "ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS persona VARCHAR(20) NOT NULL DEFAULT 'frankl'",
    "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS persona VARCHAR(20) NOT NULL DEFAULT 'frankl'",
    """CREATE TABLE IF NOT EXISTS memories (
        user_id BIGINT NOT NULL,
        persona VARCHAR(20) NOT NULL,
        memory TEXT NOT NULL DEFAULT '',
        memory_cursor BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ,
        PRIMARY KEY (user_id, persona)
    )""",
    # Frankl's memory used to live in the users table.
    """INSERT INTO memories (user_id, persona, memory, memory_cursor, updated_at)
       SELECT user_id, 'frankl', memory, memory_cursor, memory_updated_at FROM users
       WHERE memory <> '' OR memory_cursor > 0
       ON CONFLICT DO NOTHING""",
    """CREATE TABLE IF NOT EXISTS persona_settings (
        user_id BIGINT NOT NULL,
        persona VARCHAR(20) NOT NULL,
        followups_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        checkin_asked BOOLEAN NOT NULL DEFAULT TRUE,
        PRIMARY KEY (user_id, persona)
    )""",
    """INSERT INTO persona_settings (user_id, persona, followups_enabled, checkin_asked)
       SELECT user_id, 'frankl', followups_enabled, checkin_asked FROM users
       ON CONFLICT DO NOTHING""",
    # Forum topics in the user's group: which topic belongs to which persona (or the parliament).
    """CREATE TABLE IF NOT EXISTS spaces (
        chat_id BIGINT NOT NULL,
        thread_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        persona VARCHAR(20) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (chat_id, thread_id)
    )""",
    # requested_by: 'user' (he asked for it) or 'advisor' (the persona's own initiative; was 'frankl').
    "UPDATE reminders SET requested_by = 'advisor' WHERE requested_by = 'frankl'",
    "ALTER TABLE reminders ALTER COLUMN requested_by SET DEFAULT 'advisor'",
    """CREATE TABLE IF NOT EXISTS parliament (
        user_id BIGINT PRIMARY KEY,
        mode VARCHAR(20) NOT NULL DEFAULT 'views',
        turn INT NOT NULL DEFAULT 0,
        absent TEXT NOT NULL DEFAULT ''
    )""",
    # --- every persona speaks through a bot of his own ---
    """CREATE TABLE IF NOT EXISTS voices (
        key VARCHAR(20) PRIMARY KEY,
        token TEXT NOT NULL,
        bot_id BIGINT NOT NULL,
        username VARCHAR(64),
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    # voiced: the topic was opened with the persona's own bot (an older one was opened by the main bot).
    "ALTER TABLE spaces ADD COLUMN IF NOT EXISTS voiced BOOLEAN NOT NULL DEFAULT FALSE",
    "UPDATE spaces SET voiced = TRUE WHERE persona = 'frankl' AND NOT voiced",
    # The pinned guide in each group, so it can be kept up to date.
    """CREATE TABLE IF NOT EXISTS forums (
        chat_id BIGINT PRIMARY KEY,
        guide_message_id BIGINT NOT NULL,
        guide_by VARCHAR(20) NOT NULL
    )""",
]


def init_db():
    def work(cur):
        for statement in SCHEMA:
            cur.execute(statement)
    run(work)
    logger.info("DB initialized")
