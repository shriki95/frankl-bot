"""The parliament: one conversation where the user consults all the personas together.

How the meeting runs is his call and it sticks until he changes it (the "mode"):
- views:      everyone answers, each separately from his own angle (in parallel - fast)
- one_by_one: everyone answers in sequence, each reacting to the one before
- rotation:   one persona answers per message, taking turns
- debate:     they discuss among themselves until he stops them (in bounded bursts, to save tokens)
- silent:     nobody answers - he is thinking out loud
A cheap model reads each message to decide the mode, who speaks and what about.
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import brain
import clock
import db
import llm
import memory
import personas
import prompts

logger = logging.getLogger(__name__)

PARLIAMENT = personas.PARLIAMENT
MODES = ("views", "one_by_one", "rotation", "debate", "silent")
DEBATE_BURST = 6          # statements per burst before pausing for him
TRANSCRIPT_LIMIT = 24     # meeting messages a speaker sees verbatim (older ones live in the meeting notes)
KEYS = [p.key for p in personas.ALL]


@dataclass
class Plan:
    action: str = "respond"          # respond / brief / summary / usage / stop / new_topic / none / more
    mode_change: str = "none"
    speakers: list = field(default_factory=list)
    recall: list = field(default_factory=list)
    focus: str = ""


# ============================================================
# STATE
# ============================================================

def get_state(user_id):
    row = db.fetchone("SELECT mode, turn, absent FROM parliament WHERE user_id = %s", (user_id,))
    if not row:
        return {"mode": "views", "turn": 0, "absent": []}
    return {"mode": row["mode"], "turn": row["turn"], "absent": [k for k in row["absent"].split(",") if k]}


def save_state(user_id, state):
    db.execute("""
        INSERT INTO parliament (user_id, mode, turn, absent) VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET mode = EXCLUDED.mode, turn = EXCLUDED.turn, absent = EXCLUDED.absent
    """, (user_id, state["mode"], state["turn"], ",".join(state["absent"])))


def present(state):
    return [k for k in KEYS if k not in state["absent"]]


def load(user_id, member=None):
    """Meeting notes, the meeting so far, and (for a speaker) his private memory of the user."""
    def work(cur):
        notes, cursor = memory.memory_state(cur, user_id, PARLIAMENT)
        private = memory.memory_state(cur, user_id, member)[0] if member else ""
        return notes, memory.window(cur, user_id, PARLIAMENT, cursor), private
    return db.run(work)


def _last_speaker(window):
    return next((m["speaker"] for m in reversed(window)
                 if m["role"] == "assistant" and m["speaker"] in KEYS), None)


def _meeting_text(notes, window, first_name):
    recent = [m for m in window if m["kind"] != "topic"][-TRANSCRIPT_LIMIT:]
    return (f"<meeting_notes>\n{notes or 'אין עדיין'}\n</meeting_notes>\n\n"
            f"<meeting>\n{memory.transcript(recent, first_name) or '(הישיבה רק התחילה)'}\n</meeting>")


def roster(keys=None):
    return ", ".join(personas.BY_KEY[k].name for k in (keys or KEYS))


# ============================================================
# ROUTING
# ============================================================

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "mode_change": {"type": "string", "enum": ["none", *MODES]},
        "action": {"type": "string", "enum": ["respond", "brief", "summary", "usage", "stop", "new_topic", "none"]},
        "speakers": {"type": "array", "items": {"type": "string", "enum": KEYS}},
        "recall": {"type": "array", "items": {"type": "string", "enum": KEYS}},
        "focus": {"type": "string"},
    },
    "required": ["mode_change", "action", "speakers", "recall", "focus"],
    "additionalProperties": False,
}

_ALIASES = {alias: p.key for p in personas.ALL for alias in (p.short, *p.aliases)}


def mentioned(text, exclude=None):
    """Personas named in a text, in order of appearance."""
    found = []
    for alias, key in sorted(_ALIASES.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(re.escape(alias), text):
            found.append((match.start(), key))
    order = []
    for _, key in sorted(found):
        if key not in order and key != exclude:
            order.append(key)
    return order


def fallback_plan(text):
    """Keyword routing, used when the router model is unavailable."""
    plan = Plan(speakers=mentioned(text))
    rules = [("summary", r"סכמ|סכם|סיכום|מסקנ"), ("usage", r"טוקנ|כמה (זה )?עלה|עלויות"),
             ("new_topic", r"נושא חדש|דף חדש"), ("stop", r"^(די|עצרו|מספיק)")]
    for action, pattern in rules:
        if re.search(pattern, text):
            plan.action = action
    modes = [("silent", r"אף אחד לא עונה|רק תקשיבו|אל תענו"), ("one_by_one", r"אחד אחד"),
             ("rotation", r"בתורו|כל אחד בתור"), ("debate", r"תדונו|תתווכחו|ביניכם"),
             ("views", r"כל אחד מנקודת|כולם תענו")]
    for mode, pattern in modes:
        if re.search(pattern, text):
            plan.mode_change = mode
            break
    return plan


async def route(text, state, window, first_name) -> Plan:
    aliases = "; ".join(f"{p.key}: {', '.join((p.short, *p.aliases))}" for p in personas.ALL)
    system = prompts.ROUTER_PROMPT.format(
        roster=", ".join(f"{p.key} ({p.name})" for p in personas.ALL), aliases=aliases,
        mode=state["mode"], absent=", ".join(state["absent"]) or "none")
    recent = memory.transcript([m for m in window if m["kind"] != "topic"][-6:], first_name)
    try:
        response = await llm.create(
            purpose=PARLIAMENT, model=llm.ROUTER_MODEL, max_tokens=2000, system=system,
            messages=[{"role": "user", "content": f"Recent:\n{recent}\n\nLatest message from the user:\n{text}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ROUTER_SCHEMA}})
        data = json.loads(llm.text_of(response))
        return Plan(action=data["action"], mode_change=data["mode_change"], speakers=data["speakers"],
                    recall=data["recall"], focus=data["focus"])
    except Exception:
        logger.exception("Parliament router failed - using keywords")
        return fallback_plan(text)


# ============================================================
# SPEAKING
# ============================================================

async def statement(user_id, first_name, key, turn, focus=""):
    """One persona's contribution to the meeting. Returns (text, left_the_meeting)."""
    notes, window, private_memory = await db.call(load, user_id, key)
    system = brain.persona_system(key, first_name, private_memory, prompts.MEMORY_EMPTY_FIRST)
    others = [k for k in KEYS if k != key]
    instructions = prompts.PARLIAMENT_MEMBER.format(
        first_name=first_name, roster=roster(others), leave_mark=prompts.LEAVE_MARK,
        turn=turn + (prompts.TURN_FOCUS.format(focus=focus) if focus else ""))
    messages = [
        {"role": "user", "content": _meeting_text(notes, window, first_name)},
        {"role": "system", "content": "\n".join(brain.time_lines([], replying=False)) + "\n\n" + instructions},
    ]
    # Same tools as in his private conversation, so the cached prompt prefix is shared with it.
    response = await llm.create(
        purpose=PARLIAMENT, model=llm.CHAT_MODEL, max_tokens=3000, system=system, tools=brain.TOOLS, tool_choice={"type": "none"},
        messages=messages, output_config={"effort": llm.CHAT_EFFORT})
    if response.stop_reason == "refusal":
        return "", False
    text = llm.text_of(response)
    left = prompts.LEAVE_MARK in text
    if left:
        logger.info(f"{key} walked out of the meeting")
    return text.replace(prompts.LEAVE_MARK, "").strip(), left


async def chair_summary(user_id, first_name):
    notes, window, _ = await db.call(load, user_id)
    system = prompts.CHAIR_PROMPT.format(chair=prompts.CHAIR_NAME, first_name=first_name, roster=roster())
    messages = [
        {"role": "user", "content": _meeting_text(notes, window, first_name)},
        {"role": "system", "content": f"עכשיו: {clock.describe_now()}\n{prompts.CHAIR_REQUEST}"},
    ]
    response = await llm.create(purpose=PARLIAMENT, model=llm.CHAT_MODEL, max_tokens=6000, system=system,
                                messages=messages, output_config={"effort": "medium"})
    return llm.text_of(response) if response.stop_reason != "refusal" else ""


# ============================================================
# A ROUND
# ============================================================

def _no_show(key, writing):
    pass


async def _write(user_id, first_name, key, turn, focus, show):
    """A statement, with 'typing...' shown for him while he writes it (emit clears it when it is sent)."""
    show(key, True)
    try:
        text, left = await statement(user_id, first_name, key, turn, focus)
    except BaseException:
        show(key, False)
        raise
    if not text:
        show(key, False)
    return text, left


async def _speak(user_id, first_name, state, key, turn, focus, emit, show):
    text, left = await _write(user_id, first_name, key, turn, focus, show)
    if text:
        await emit(key, text)
    if left:
        state["absent"].append(key)
        await db.call(save_state, user_id, state)
        await emit("chair", prompts.MEMBER_LEFT.format(name=personas.BY_KEY[key].name))


def _next_in_rotation(state, after=None, exclude=()):
    keys = [k for k in present(state) if k not in exclude] or present(state)
    if not keys:
        return None
    if after in KEYS:
        start = (KEYS.index(after) + 1) % len(KEYS)
        ordered = KEYS[start:] + KEYS[:start]
        return next(k for k in ordered if k in keys)
    return keys[state["turn"] % len(keys)]


async def debate(user_id, first_name, state, emit, focus="", first=None, last_speaker=None, show=_no_show):
    """A burst of discussion between the personas. Returns True if it stopped only because of the budget."""
    speaker = first if first in present(state) else _next_in_rotation(state, after=last_speaker)
    for _ in range(DEBATE_BURST):
        if speaker is None:
            return False
        text, left = await _write(user_id, first_name, speaker, prompts.TURN_DEBATE, focus, show)
        focus = ""  # the focus frames the first statement; after that they follow each other
        if text:
            await emit(speaker, text)
        if left:
            state["absent"].append(speaker)
            await db.call(save_state, user_id, state)
            await emit("chair", prompts.MEMBER_LEFT.format(name=personas.BY_KEY[speaker].name))
        if len(present(state)) < 2:
            return False
        # Whoever was just addressed by name answers next; otherwise the next one around the table.
        addressed = [k for k in mentioned(text, exclude=speaker) if k in present(state)]
        speaker = addressed[0] if addressed else _next_in_rotation(state, after=speaker, exclude=(speaker,))
    return True


async def respond(user_id, first_name, plan, emit, show=_no_show):
    """Carry out a plan. emit(speaker, text) sends and saves one message; show(speaker, True/False)
    marks who is writing right now.
    Returns 'round' (show the meeting buttons), 'paused' (debate burst used up), 'silent', 'ack', 'usage'
    (he asked what the meeting costs - the chair reports, outside the meeting record) or 'none'."""
    state = await db.call(get_state, user_id)
    changed = False
    if plan.mode_change in MODES and plan.mode_change != state["mode"]:
        state["mode"] = plan.mode_change
        changed = True
    for key in plan.recall:
        if key in state["absent"]:
            state["absent"].remove(key)
            changed = True
            await emit("chair", prompts.MEMBER_RETURNED.format(name=personas.BY_KEY[key].name))
    if changed:
        await db.call(save_state, user_id, state)

    if plan.action == "usage":
        return "usage"
    if plan.action == "new_topic":
        await memory.consolidate(user_id, PARLIAMENT)
        await db.call(memory.save_message, user_id, PARLIAMENT, "assistant", "", "topic", "chair")
        state["absent"] = []
        await db.call(save_state, user_id, state)
        await emit("chair", prompts.NEW_TOPIC)
        return "none"
    if plan.action == "summary":
        show("chair", True)
        try:
            text = await chair_summary(user_id, first_name)
        finally:
            show("chair", False)
        if text:
            await emit("chair", text)
        return "round"
    if plan.action == "stop":
        return "ack"
    if plan.action == "more":
        _, window, _ = await db.call(load, user_id)
        return "paused" if await debate(user_id, first_name, state, emit, last_speaker=_last_speaker(window),
                                        show=show) else "round"

    speakers = [k for k in plan.speakers if k in KEYS]
    for key in speakers:  # addressing someone who left brings him back
        if key in state["absent"]:
            state["absent"].remove(key)
            await db.call(save_state, user_id, state)
            await emit("chair", prompts.MEMBER_RETURNED.format(name=personas.BY_KEY[key].name))
    if plan.action == "none" or (state["mode"] == "silent" and not speakers):
        return "ack" if changed else "silent"
    if not present(state):
        return "none"

    mode = state["mode"]
    if plan.action == "brief":  # small talk: one of them answers, in turn - no round, no debate
        key = speakers[0] if speakers else _next_in_rotation(state)
        state["turn"] += 1
        await db.call(save_state, user_id, state)
        await _speak(user_id, first_name, state, key, prompts.TURN_BRIEF, plan.focus, emit, show)
        return "round"
    if mode == "debate":
        return "paused" if await debate(user_id, first_name, state, emit, plan.focus,
                                        first=speakers[0] if speakers else None, show=show) else "round"
    if speakers:
        for key in speakers:
            await _speak(user_id, first_name, state, key, prompts.TURN_DIRECT, plan.focus, emit, show)
        return "round"
    if mode == "rotation":
        key = _next_in_rotation(state)
        state["turn"] += 1
        await db.call(save_state, user_id, state)
        await _speak(user_id, first_name, state, key, prompts.TURN_ROTATION, plan.focus, emit, show)
        return "round"
    if mode == "one_by_one":
        for key in present(state):
            if key in present(state):  # someone may have walked out mid-round
                await _speak(user_id, first_name, state, key, prompts.TURN_ONE_BY_ONE, plan.focus, emit, show)
        return "round"
    # views: independent answers, written in parallel and sent in order
    keys = present(state)
    results = await asyncio.gather(*(_write(user_id, first_name, k, prompts.TURN_VIEWS, plan.focus, show)
                                     for k in keys), return_exceptions=True)
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            logger.error(f"Parliament statement by {key} failed: {result!r}")
            continue
        text, left = result
        if text:
            await emit(key, text)
        if left:
            state["absent"].append(key)
            await db.call(save_state, user_id, state)
            await emit("chair", prompts.MEMBER_LEFT.format(name=personas.BY_KEY[key].name))
    return "round"
