"""Conversation storage and long-term memory - separately for every persona and for the parliament.

What a persona sees on every message:
- a notes document about the user, rewritten by Claude every few exchanges (and after a pause),
- the recent messages of his own conversation, verbatim.
Older details stay in the messages table and are reachable with search().
"""
import asyncio
import logging
import re
import time

import clock
import db
import llm
import personas
import prompts

logger = logging.getLogger(__name__)

CONSOLIDATE_AFTER = 16        # unsummarized messages that trigger a memory update (~8 exchanges)
IDLE_CONSOLIDATE_AFTER = 6    # after a pause, fold in even a short conversation
IDLE_SECONDS = 10 * 60         # before Render's free plan puts the bot to sleep (15 idle minutes)
OVERLAP = 6                   # already-summarized messages still shown verbatim, for continuity
WINDOW_LIMIT = 40             # hard cap on verbatim messages sent to the model
BATCH = 120                   # messages per memory update call
MAX_MEMORY_CHARS = 10000      # above this the memory gets a compression pass
RETRY_AFTER_FAILURE = 15 * 60
MEETING_RECENT = 10           # latest parliament messages a persona carries into his private conversation
MEETING_SNIPPET = 300         # characters kept from each of them

_locks: dict[tuple, asyncio.Lock] = {}
_last_failure: dict[tuple, float] = {}
_background: set[asyncio.Task] = set()


def speaker_name(message, first_name):
    if message["role"] == "user":
        return first_name or "המשתמש"
    persona = personas.BY_KEY.get(message.get("speaker") or message.get("persona"))
    return persona.name if persona else prompts.CHAIR_NAME


# ============================================================
# MESSAGES
# ============================================================

def save_user(user_id, first_name, username):
    db.execute("""
        INSERT INTO users (user_id, first_name, username) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name, username = EXCLUDED.username
    """, (user_id, first_name, username))


def save_message(user_id, persona, role, content, kind="chat", speaker=None):
    def work(cur):
        cur.execute("""
            INSERT INTO messages (user_id, persona, role, content, kind, speaker)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (user_id, persona, role, content, kind, speaker))
        return cur.fetchone()[0]
    return db.run(work)


def latest_user_message_id(user_id, persona):
    row = db.fetchone("SELECT MAX(id) AS id FROM messages WHERE user_id = %s AND persona = %s AND role = 'user'",
                      (user_id, persona))
    return row["id"] if row else None


def window(cur, user_id, persona, cursor):
    """Recent messages: everything not yet in the memory document, plus a few before it.
    A 'topic' marker (new topic in the parliament) cuts off everything before it."""
    cur.execute("""
        SELECT id, role, content, kind, speaker, created_at::timestamptz AS at FROM messages
        WHERE user_id = %s AND persona = %s ORDER BY id DESC LIMIT %s
    """, (user_id, persona, WINDOW_LIMIT))
    recent = list(reversed(db.rows(cur)))
    markers = [i for i, m in enumerate(recent) if m["kind"] == "topic"]
    if markers:
        recent = recent[markers[-1] + 1:]
    older = [m for m in recent if m["id"] <= cursor][-OVERLAP:]
    return older + [m for m in recent if m["id"] > cursor]


def search(user_id, persona, first_name, query, before_id):
    """Keyword search in this persona's older messages. Returns text for the model."""
    words = [w.replace("\\", "\\\\").replace("_", "\\_") for w in re.findall(r"\w{2,}", query)][:6]
    if not words:
        return "לא התקבלו מילות חיפוש."
    match = "content ILIKE %s"
    patterns = [f"%{w}%" for w in words]
    found = db.fetchall(f"""
        SELECT role, content, created_at::timestamptz AS at, {" + ".join(f"({match})::int" for _ in words)} AS score
        FROM messages
        WHERE user_id = %s AND persona = %s AND id < %s AND ({" OR ".join(match for _ in words)})
        ORDER BY score DESC, id DESC LIMIT 8
    """, (*patterns, user_id, persona, before_id, *patterns))
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


def get_memory(user_id, persona):
    row = db.fetchone("SELECT memory FROM memories WHERE user_id = %s AND persona = %s", (user_id, persona))
    return row["memory"] if row else ""


def memory_state(cur, user_id, persona):
    cur.execute("SELECT memory, memory_cursor FROM memories WHERE user_id = %s AND persona = %s",
                (user_id, persona))
    found = db.rows(cur)
    return (found[0]["memory"], found[0]["memory_cursor"]) if found else ("", 0)


def meetings(cur, user_id, persona, first_name):
    """What a persona remembers from the parliament: the meeting notes, and the latest part he was present for.
    Shown in his private conversation (the other direction - private to the meeting - stays closed)."""
    notes, cursor = memory_state(cur, user_id, personas.PARLIAMENT)
    cur.execute("""
        SELECT id, role, content, kind, speaker, created_at::timestamptz AS at FROM messages
        WHERE user_id = %s AND persona = %s AND id > %s ORDER BY id DESC LIMIT %s
    """, (user_id, personas.PARLIAMENT, cursor, MEETING_RECENT * 3))
    recent = list(reversed(db.rows(cur)))
    name = personas.BY_KEY[persona].name
    left, back = prompts.MEMBER_LEFT.format(name=name), prompts.MEMBER_RETURNED.format(name=name)
    cur.execute("SELECT absent FROM parliament WHERE user_id = %s", (user_id,))
    found = cur.fetchone()
    absent_now = bool(found) and persona in found[0].split(",")
    first_event = next((m["content"] for m in recent if m["content"] in (left, back)), None)
    present = first_event == left if first_event else not absent_now
    attended = []
    for m in recent:
        if m["kind"] == "topic":
            present = True
        elif m["content"] == left:
            attended.append(m)
            present = False
        elif m["content"] == back:
            present = True
        elif present:
            attended.append(m)
    attended = [{**m, "content": m["content"] if len(m["content"]) <= MEETING_SNIPPET
                 else m["content"][:MEETING_SNIPPET] + "..."} for m in attended[-MEETING_RECENT:]]
    if not notes and not attended:
        return ""
    parts = [notes] if notes else []
    if attended:
        parts.append("מהישיבה האחרונה:\n" + transcript(attended, first_name))
    return prompts.MEETINGS_BLOCK.format(meetings="\n\n".join(parts))


def _legacy_notes(cur, user_id):
    """Notes saved by the first version of the bot (Frankl only), before the memory document existed."""
    cur.execute("SELECT profile_summary FROM users WHERE user_id = %s", (user_id,))
    found = cur.fetchone()
    parts = [f"פרופיל: {found[0]}"] if found and found[0] else []
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


def _pending(user_id, persona):
    def work(cur):
        cur.execute("SELECT first_name FROM users WHERE user_id = %s", (user_id,))
        found = cur.fetchone()
        if not found:
            return None, "", 0, [], 0, ""
        memory_text, cursor = memory_state(cur, user_id, persona)
        cur.execute("""
            SELECT id, role, content, kind, speaker, created_at::timestamptz AS at FROM messages
            WHERE user_id = %s AND persona = %s AND id > %s ORDER BY id LIMIT %s
        """, (user_id, persona, cursor, BATCH))
        messages = db.rows(cur)
        cur.execute("SELECT COUNT(*) FROM messages WHERE user_id = %s AND persona = %s AND id > %s",
                    (user_id, persona, cursor))
        remaining = cur.fetchone()[0]
        legacy = _legacy_notes(cur, user_id) if persona == "frankl" and not memory_text else ""
        return found[0], memory_text, cursor, messages, remaining, legacy
    return db.run(work)


def _store(user_id, persona, memory_text, cursor):
    def work(cur):
        cur.execute("""
            INSERT INTO memories (user_id, persona, memory, memory_cursor, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, persona) DO UPDATE SET memory = EXCLUDED.memory,
                memory_cursor = GREATEST(memories.memory_cursor, EXCLUDED.memory_cursor), updated_at = NOW()
        """, (user_id, persona, memory_text, cursor))
        cur.execute("INSERT INTO memory_versions (user_id, persona, content) VALUES (%s, %s, %s)",
                    (user_id, persona, memory_text))
        cur.execute("""
            DELETE FROM memory_versions WHERE user_id = %s AND persona = %s AND id NOT IN (
                SELECT id FROM memory_versions WHERE user_id = %s AND persona = %s ORDER BY id DESC LIMIT 30)
        """, (user_id, persona, user_id, persona))
    db.run(work)


def _instructions(persona):
    if persona == personas.PARLIAMENT:
        roster = ", ".join(p.name for p in personas.ALL)
        return prompts.PARLIAMENT_CONSOLIDATION_PROMPT.format(roster=roster), prompts.PARLIAMENT_MEMORY_SECTIONS
    p = personas.BY_KEY[persona]
    return prompts.CONSOLIDATION_PROMPT.format(name=p.name, focus=p.memory_focus), prompts.MEMORY_SECTIONS


async def _ask(system, messages):
    # The whole document is rewritten (Hebrew is token-heavy), plus thinking - leave room for both.
    response = await llm.create(
        model=llm.MEMORY_MODEL, max_tokens=16000, system=system, messages=messages,
        output_config={"effort": llm.MEMORY_EFFORT}, timeout=300)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"memory update stopped: {response.stop_reason}")
    return llm.text_of(response)


def transcript(messages, first_name):
    lines = []
    for m in messages:
        if m["kind"] == "topic":
            lines.append("--- נושא חדש ---")
        else:
            lines.append(f"[{clock.stamp(m['at'])}] {speaker_name(m, first_name)}: {m['content']}")
    return "\n".join(lines)


async def _write(persona, first_name, memory_text, messages, legacy):
    system, _ = _instructions(persona)
    parts = [f"<current_memory>\n{memory_text or '(ריק)'}\n</current_memory>"]
    if legacy:
        parts.append(f"<old_notes>\nרשימות מגרסה קודמת של הבוט - שלב אותן בזיכרון:\n{legacy}\n</old_notes>")
    if messages:
        parts.append(f"<new_conversation>\n{transcript(messages, first_name)}\n</new_conversation>")
    parts.append(f"עכשיו: {clock.describe_now()}")
    request = [{"role": "user", "content": "\n\n".join(parts)}]
    text = await _ask(system, request)
    if len(text) > MAX_MEMORY_CHARS:
        request += [{"role": "assistant", "content": text},
                    {"role": "user", "content": prompts.COMPRESS_REQUEST}]
        text = await _ask(system, request)
    return text


def _looks_valid(new, old, sections):
    headers = sum(f"## {title}" in new for title in sections)
    if headers < len(sections) - 2:
        return False
    # A sudden collapse of a substantial memory means something went wrong - keep the old one.
    return not (len(old) > 1500 and len(new) < len(old) * 0.4)


async def consolidate(user_id, persona) -> bool:
    """Fold every message not yet in the memory document into it. False if an update failed."""
    key = (user_id, persona)
    async with _locks.setdefault(key, asyncio.Lock()):
        while True:
            first_name, memory_text, cursor, messages, remaining, legacy = await db.call(_pending, user_id, persona)
            if not messages and not legacy:
                return True
            try:
                new_memory = await _write(persona, first_name, memory_text, messages, legacy)
            except Exception:
                logger.exception(f"Memory update failed for {persona}/{user_id}")
                _last_failure[key] = time.monotonic()
                return False
            if not _looks_valid(new_memory, memory_text, _instructions(persona)[1]):
                logger.error(f"Memory update for {persona}/{user_id} rejected as invalid ({len(new_memory)} chars)")
                _last_failure[key] = time.monotonic()
                return False
            last_id = messages[-1]["id"] if messages else cursor
            await db.call(_store, user_id, persona, new_memory, last_id)
            logger.info(f"Memory updated for {persona}/{user_id}: {len(messages)} messages, {len(new_memory)} chars")
            if remaining <= len(messages):
                return True


def _unsummarized(user_id):
    """{persona: (unsummarized messages, seconds since the last message)}"""
    rows = db.fetchall("""
        SELECT m.persona, COUNT(*) AS n,
               EXTRACT(EPOCH FROM NOW() - MAX(m.created_at::timestamptz))::float AS idle
        FROM messages m LEFT JOIN memories mm ON mm.user_id = m.user_id AND mm.persona = m.persona
        WHERE m.user_id = %s AND m.id > COALESCE(mm.memory_cursor, 0)
        GROUP BY m.persona
    """, (user_id,))
    return {r["persona"]: (r["n"], r["idle"]) for r in rows}


async def maybe_consolidate(user_id, persona, minimum=CONSOLIDATE_AFTER):
    key = (user_id, persona)
    lock = _locks.get(key)
    if lock and lock.locked():
        return
    if time.monotonic() - _last_failure.get(key, float("-inf")) < RETRY_AFTER_FAILURE:
        return
    count, _ = (await db.call(_unsummarized, user_id)).get(persona, (0, 0))
    if count >= minimum:
        await consolidate(user_id, persona)


def consolidate_soon(user_id, persona):
    """Check in the background whether this memory needs an update."""
    task = asyncio.create_task(maybe_consolidate(user_id, persona))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def consolidate_idle(user_ids):
    """Update the memories of conversations that paused, so the next one starts lean."""
    for user_id in user_ids:
        for persona, (count, idle) in (await db.call(_unsummarized, user_id)).items():
            if idle > IDLE_SECONDS and count >= IDLE_CONSOLIDATE_AFTER:
                await maybe_consolidate(user_id, persona, IDLE_CONSOLIDATE_AFTER)


def _delete_summarized(user_id, persona):
    db.execute("""
        DELETE FROM messages WHERE user_id = %s AND persona = %s
        AND id <= (SELECT memory_cursor FROM memories WHERE user_id = %s AND persona = %s)
    """, (user_id, persona, user_id, persona))


async def clear_history(user_id, persona) -> bool:
    """Delete a conversation after saving everything in it to memory."""
    if not await consolidate(user_id, persona):
        return False
    await db.call(_delete_summarized, user_id, persona)
    return True
