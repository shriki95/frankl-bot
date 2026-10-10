"""Where each conversation lives in Telegram.

In the user's group with topics, every persona has his own topic, and one more topic holds the
parliament. Private chats: the main bot's is Frankl's (as it always was), and each persona's own bot
has a private chat of its own with him.
"""
from dataclasses import dataclass

import db


@dataclass(frozen=True)
class Space:
    chat_id: int
    thread_id: int | None   # forum topic; None in a private chat
    persona: str            # persona key, or personas.PARLIAMENT

    @property
    def private(self):
        return self.chat_id > 0   # groups have negative ids


def persona_of(chat_id, thread_id):
    row = db.fetchone("SELECT persona FROM spaces WHERE chat_id = %s AND thread_id = %s", (chat_id, thread_id))
    return row["persona"] if row else None


def in_chat(chat_id):
    """{persona: thread_id} for one group."""
    return {r["persona"]: r["thread_id"]
            for r in db.fetchall("SELECT persona, thread_id FROM spaces WHERE chat_id = %s", (chat_id,))}


def save(chat_id, thread_id, user_id, persona, voiced=False):
    db.execute("""
        INSERT INTO spaces (chat_id, thread_id, user_id, persona, voiced) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (chat_id, thread_id) DO UPDATE
        SET persona = EXCLUDED.persona, user_id = EXCLUDED.user_id, voiced = EXCLUDED.voiced
    """, (chat_id, thread_id, user_id, persona, voiced))


def remove(chat_id, thread_id):
    db.execute("DELETE FROM spaces WHERE chat_id = %s AND thread_id = %s", (chat_id, thread_id))


def is_voiced(chat_id, thread_id):
    row = db.fetchone("SELECT voiced FROM spaces WHERE chat_id = %s AND thread_id = %s", (chat_id, thread_id))
    return bool(row and row["voiced"])


def set_voiced(chat_id, thread_id):
    db.execute("UPDATE spaces SET voiced = TRUE WHERE chat_id = %s AND thread_id = %s", (chat_id, thread_id))


def group_of(user_id):
    """His group with the topics (the newest, if there are several)."""
    row = db.fetchone("SELECT chat_id FROM spaces WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
    return row["chat_id"] if row else None


def home(user_id, persona):
    """Where a persona writes to the user on his own: his topic, else (Frankl only) the private chat."""
    row = db.fetchone("""
        SELECT chat_id, thread_id FROM spaces WHERE user_id = %s AND persona = %s ORDER BY created_at DESC LIMIT 1
    """, (user_id, persona))
    if row:
        return Space(row["chat_id"], row["thread_id"], persona)
    return Space(user_id, None, persona) if persona == "frankl" else None


def guide(chat_id):
    return db.fetchone("SELECT guide_message_id, guide_by FROM forums WHERE chat_id = %s", (chat_id,))


def save_guide(chat_id, message_id, by):
    db.execute("""
        INSERT INTO forums (chat_id, guide_message_id, guide_by) VALUES (%s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET guide_message_id = EXCLUDED.guide_message_id, guide_by = EXCLUDED.guide_by
    """, (chat_id, message_id, by))


def link(chat_id, thread_id):
    """t.me link to a topic of a supergroup (ids look like -100xxxxxxxxxx)."""
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{thread_id}"
