"""Where each conversation lives in Telegram.

The private chat with the bot is Frankl's (as it always was). In the user's group with topics,
every persona has his own topic, and one more topic holds the parliament.
"""
from dataclasses import dataclass

import db


@dataclass(frozen=True)
class Space:
    chat_id: int
    thread_id: int | None   # forum topic; None in the private chat
    persona: str            # persona key, or personas.PARLIAMENT


def persona_of(chat_id, thread_id):
    row = db.fetchone("SELECT persona FROM spaces WHERE chat_id = %s AND thread_id = %s", (chat_id, thread_id))
    return row["persona"] if row else None


def in_chat(chat_id):
    """{persona: thread_id} for one group."""
    return {r["persona"]: r["thread_id"]
            for r in db.fetchall("SELECT persona, thread_id FROM spaces WHERE chat_id = %s", (chat_id,))}


def save(chat_id, thread_id, user_id, persona):
    db.execute("""
        INSERT INTO spaces (chat_id, thread_id, user_id, persona) VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id, thread_id) DO UPDATE SET persona = EXCLUDED.persona, user_id = EXCLUDED.user_id
    """, (chat_id, thread_id, user_id, persona))


def home(user_id, persona):
    """Where a persona writes to the user on his own: his topic, else (Frankl only) the private chat."""
    row = db.fetchone("""
        SELECT chat_id, thread_id FROM spaces WHERE user_id = %s AND persona = %s ORDER BY created_at DESC LIMIT 1
    """, (user_id, persona))
    if row:
        return Space(row["chat_id"], row["thread_id"], persona)
    return Space(user_id, None, persona) if persona == "frankl" else None


def link(chat_id, thread_id):
    """t.me link to a topic of a supergroup (ids look like -100xxxxxxxxxx)."""
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{thread_id}"
