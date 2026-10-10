"""Conversation storage and long-term memory.

What Frankl sees on every message:
- a notes document about the user, rewritten by Claude every few exchanges (and after a pause),
- the recent messages verbatim.
Older details stay in the messages table and are reachable with search().
"""
import asyncio
import logging
import re
import time

import clock
import db
import llm
import prompts

logger = logging.getLogger(__name__)

CONSOLIDATE_AFTER = 12        # unsummarized messages that trigger a memory update (~6 exchanges)
IDLE_CONSOLIDATE_AFTER = 4    # after a pause, fold in even a short conversation
IDLE_SECONDS = 30 * 60
OVERLAP = 6                   # already-summarized messages still shown verbatim, for continuity
WINDOW_LIMIT = 40             # hard cap on verbatim messages sent to the model
BATCH = 120                   # messages per memory update call
MAX_MEMORY_CHARS = 12000      # above this the memory gets a compression pass
RETRY_AFTER_FAILURE = 15 * 60

_locks: dict[int, asyncio.Lock] = {}
_last_failure: dict[int, float] = {}
_background: set[asyncio.Task] = set()


# ============================================================
# MESSAGES
# ============================================================

def save_user(user_id, first_name, username):
    db.execute("""
        INSERT INTO users (user_id, first_name, username) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name, username = EXCLUDED.username
    """, (user_id, first_name, username))


def save_message(user_id, role, content, kind="chat"):
    def work(cur):
        cur.execute(
            "INSERT INTO messages (user_id, role, content, kind) VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, role, content, kind))
        return cur.fetchone()[0]
    return db.run(work)


def latest_user_message_id(user_id):
    row = db.fetchone("SELECT MAX(id) AS id FROM messages WHERE user_id = %s AND role = 'user'", (user_id,))
    return row["id"] if row else None


def window(cur, user_id, cursor):
    """Recent messages: everything not yet in the memory document, plus a few before it."""
    cur.execute("""
        SELECT id, role, content, kind, created_at::timestamptz AS at FROM messages
        WHERE user_id = %s ORDER BY id DESC LIMIT %s
    """, (user_id, WINDOW_LIMIT))
    recent = list(reversed(db.rows(cur)))
    older = [m for m in recent if m["id"] <= cursor][-OVERLAP:]
    return older + [m for m in recent if m["id"] > cursor]


def search(user_id, first_name, query, before_id):
    """Keyword search in messages older than the current window. Returns text for the model."""
    words = [w.replace("\\", "\\\\").replace("_", "\\_") for w in re.findall(r"\w{2,}", query)][:6]
    if not words:
        return "לא התקבלו מילות חיפוש."
    match = "content ILIKE %s"
    patterns = [f"%{w}%" for w in words]
    found = db.fetchall(f"""
        SELECT role, content, created_at::timestamptz AS at, {" + ".join(f"({match})::int" for _ in words)} AS score
        FROM messages
        WHERE user_id = %s AND id < %s AND ({" OR ".join(match for _ in words)})
        ORDER BY score DESC, id DESC LIMIT 8
    """, (*patterns, user_id, before_id, *patterns))
    if not found:
        return "לא נמצא דבר בשיחות הקודמות."
    lines = []
    for m in sorted(found, key=lambda m: m["at"]):
        speaker = first_name if m["role"] == "user" else "אתה"
        text = m["content"] if len(m["content"]) <= 400 else m["content"][:400] + "..."
        lines.append(f"[{clock.stamp(m['at'])}] {speaker}: {text}")
    return "\n".join(lines)


# ============================================================
# MEMORY DOCUMENT
# ============================================================

def section(memory_text, title):
    """One '## title' section of the memory document."""
    found = re.search(rf"^## {re.escape(title)}[ \t]*\n(.*?)(?=^## |\Z)", memory_text, re.M | re.S)
    return found.group(1).strip() if found else ""


def _legacy_notes(cur, user_id, profile_summary):
    """Notes saved by the previous version of the bot, before the memory document existed."""
    parts = [f"פרופיל: {profile_summary}"] if profile_summary else []
    for table, title in (("life_events", "אירועים"), ("goals", "יעדים"), ("patterns", "דפוסים")):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        if not cur.fetchone()[0]:
            continue
        cur.execute(f"SELECT description, created_at FROM {table} WHERE user_id = %s ORDER BY id", (user_id,))
        items = cur.fetchall()
        if items:
            parts.append(f"{title}:\n" + "\n".join(
                f"• [{created:%d/%m/%y}] {description}" if created else f"• {description}"
                for description, created in items))
    return "\n\n".join(parts)


def _pending(user_id):
    def work(cur):
        cur.execute("SELECT first_name, memory, memory_cursor, profile_summary FROM users WHERE user_id = %s",
                    (user_id,))
        found = db.rows(cur)
        if not found:
            return None, [], 0, ""
        user = found[0]
        cur.execute("""
            SELECT id, role, content, created_at::timestamptz AS at FROM messages
            WHERE user_id = %s AND id > %s ORDER BY id LIMIT %s
        """, (user_id, user["memory_cursor"], BATCH))
        messages = db.rows(cur)
        cur.execute("SELECT COUNT(*) FROM messages WHERE user_id = %s AND id > %s",
                    (user_id, user["memory_cursor"]))
        remaining = cur.fetchone()[0]
        legacy = "" if user["memory"] else _legacy_notes(cur, user_id, user["profile_summary"])
        return user, messages, remaining, legacy
    return db.run(work)


def _store(user_id, memory_text, cursor):
    def work(cur):
        cur.execute("""
            UPDATE users SET memory = %s, memory_cursor = GREATEST(memory_cursor, %s), memory_updated_at = NOW()
            WHERE user_id = %s
        """, (memory_text, cursor, user_id))
        cur.execute("INSERT INTO memory_versions (user_id, content) VALUES (%s, %s)", (user_id, memory_text))
        cur.execute("""
            DELETE FROM memory_versions WHERE user_id = %s AND id NOT IN (
                SELECT id FROM memory_versions WHERE user_id = %s ORDER BY id DESC LIMIT 30)
        """, (user_id, user_id))
    db.run(work)


async def _ask(messages):
    response = await llm.create(
        model=llm.MEMORY_MODEL, max_tokens=8000,
        system=prompts.CONSOLIDATION_PROMPT, messages=messages,
        output_config={"effort": "medium"})
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"memory update stopped: {response.stop_reason}")
    return llm.text_of(response)


async def _write(user, messages, legacy):
    name = user["first_name"] or "המשתמש"
    parts = [f"<current_memory>\n{user['memory'] or '(ריק)'}\n</current_memory>"]
    if legacy:
        parts.append(f"<old_notes>\nרשימות מגרסה קודמת של הבוט - שלב אותן בזיכרון:\n{legacy}\n</old_notes>")
    if messages:
        lines = [f"[{clock.stamp(m['at'])}] {name if m['role'] == 'user' else 'פרנקל'}: {m['content']}"
                 for m in messages]
        parts.append("<new_conversation>\n" + "\n".join(lines) + "\n</new_conversation>")
    parts.append(f"עכשיו: {clock.describe_now()}")
    request = [{"role": "user", "content": "\n\n".join(parts)}]
    text = await _ask(request)
    if len(text) > MAX_MEMORY_CHARS:
        request += [{"role": "assistant", "content": text},
                    {"role": "user", "content": prompts.COMPRESS_REQUEST}]
        text = await _ask(request)
    return text


def _looks_valid(new, old):
    headers = sum(f"## {title}" in new for title in prompts.MEMORY_SECTIONS)
    if headers < len(prompts.MEMORY_SECTIONS) - 2:
        return False
    # A sudden collapse of a substantial memory means something went wrong - keep the old one.
    return not (len(old) > 1500 and len(new) < len(old) * 0.4)


async def consolidate(user_id) -> bool:
    """Fold every message not yet in the memory document into it. False if an update failed."""
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        while True:
            user, messages, remaining, legacy = await db.call(_pending, user_id)
            if not messages and not legacy:
                return True
            try:
                new_memory = await _write(user, messages, legacy)
            except Exception:
                logger.exception(f"Memory update failed for user {user_id}")
                _last_failure[user_id] = time.monotonic()
                return False
            if not _looks_valid(new_memory, user["memory"]):
                logger.error(f"Memory update for user {user_id} rejected as invalid ({len(new_memory)} chars)")
                _last_failure[user_id] = time.monotonic()
                return False
            last_id = messages[-1]["id"] if messages else user["memory_cursor"]
            await db.call(_store, user_id, new_memory, last_id)
            logger.info(f"Memory updated for user {user_id}: {len(messages)} messages, {len(new_memory)} chars")
            if remaining <= len(messages):
                return True


def _unsummarized_count(user_id):
    row = db.fetchone("""
        SELECT COUNT(*) AS n FROM messages m JOIN users u ON u.user_id = m.user_id
        WHERE m.user_id = %s AND m.id > u.memory_cursor
    """, (user_id,))
    return row["n"] if row else 0


async def maybe_consolidate(user_id, minimum=CONSOLIDATE_AFTER):
    lock = _locks.get(user_id)
    if lock and lock.locked():
        return
    if time.monotonic() - _last_failure.get(user_id, float("-inf")) < RETRY_AFTER_FAILURE:
        return
    if await db.call(_unsummarized_count, user_id) >= minimum:
        await consolidate(user_id)


def consolidate_soon(user_id):
    """Check in the background whether the memory needs an update."""
    task = asyncio.create_task(maybe_consolidate(user_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


def _is_idle(user_id):
    row = db.fetchone("""
        SELECT MAX(m.created_at::timestamptz) < NOW() - make_interval(secs => %s) AS idle
        FROM messages m WHERE m.user_id = %s
    """, (IDLE_SECONDS, user_id))
    return bool(row and row["idle"])


async def consolidate_idle(user_ids):
    """Update the memory of users who paused, so the next conversation starts lean."""
    for user_id in user_ids:
        if await db.call(_is_idle, user_id):
            await maybe_consolidate(user_id, IDLE_CONSOLIDATE_AFTER)


def _delete_summarized(user_id):
    db.execute("""
        DELETE FROM messages WHERE user_id = %s
        AND id <= (SELECT memory_cursor FROM users WHERE user_id = %s)
    """, (user_id, user_id))


async def clear_history(user_id) -> bool:
    """Delete the conversation after saving everything in it to memory."""
    if not await consolidate(user_id):
        return False
    await db.call(_delete_summarized, user_id)
    return True
