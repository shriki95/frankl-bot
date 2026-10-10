"""Frankl's conversation engine: builds what Claude sees and runs the tool loop."""
import asyncio
import logging

import clock
import db
import llm
import memory
import prompts
import reminders

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
CACHE = {"type": "ephemeral"}

_conversation_locks: dict[int, asyncio.Lock] = {}


def conversation_lock(user_id) -> asyncio.Lock:
    """One reply (or proactive message) at a time per user."""
    return _conversation_locks.setdefault(user_id, asyncio.Lock())


TOOLS = [
    {
        "name": "cancel_scheduled_message",
        "description": "ביטול פנייה מתוכננת לפי המספר שלה (מופיע ברשימת הפניות המתוכננות בהקשר).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "מספר הפנייה"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "schedule_message",
        "description": ("תזמון פנייה אחת ביוזמתך בזמן מסוים - מעקב אחרי משהו שעלה בשיחה, או תזכורת שהוא ביקש. "
                        "בזמן שנקבע תכתוב לו הודעה לפי המטרה."),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "תאריך ושעה בשעון ישראל, בפורמט YYYY-MM-DD HH:MM"},
                "purpose": {"type": "string",
                            "description": "לעצמך: על מה לכתוב לו ולמה. למשל: לשאול איך היה הראיון בחברת X"},
                "requested_by_user": {"type": "boolean",
                                      "description": "true אם הוא ביקש את התזכורת במפורש, false אם זו יוזמה שלך"},
            },
            "required": ["when", "purpose", "requested_by_user"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "search_past_conversations",
        "description": ("חיפוש בשיחות קודמות איתו - מעבר לזיכרון ולשיחה האחרונה. השתמש כשהוא מזכיר משהו מהעבר "
                        "ואתה לא בטוח בפרטים (שם, תאריך, מה בדיוק קרה). כתוב כמה מילות מפתח בצורת הבסיס שלהן, "
                        "בלי ה/ו/ב/ל/ש בתחילת המילה."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "מילות מפתח מופרדות ברווח"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "set_checkin_preferences",
        "description": "עדכון ההעדפות שלו לפניות ביוזמתך. קרא רק אחרי שהוא אמר מה הוא מעדיף.",
        "input_schema": {
            "type": "object",
            "properties": {
                "daily_time": {"type": "string",
                               "description": "שעת ההודעה היומית בשעון ישראל (HH:MM), או off כדי לבטל אותה"},
                "followups": {"type": "boolean",
                              "description": "האם מותר לך לפנות מדי פעם, בזמנים שונים, לפי מה שעולה בשיחה"},
            },
            "required": ["daily_time", "followups"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


# ============================================================
# CONTEXT
# ============================================================

def _load(user_id):
    def work(cur):
        cur.execute("SELECT memory, memory_cursor FROM users WHERE user_id = %s", (user_id,))
        found = db.rows(cur)
        user = found[0] if found else {"memory": "", "memory_cursor": 0}
        return user["memory"], memory.window(cur, user_id, user["memory_cursor"]), reminders.settings(cur, user_id)
    return db.run(work)


def _history(window):
    """API messages from stored ones: starts with the user, consecutive turns of one role merged."""
    messages = []
    for m in window:
        if not messages and m["role"] != "user":
            continue
        if messages and messages[-1]["role"] == m["role"]:
            messages[-1]["content"] += "\n\n" + m["content"]
        else:
            messages.append({"role": m["role"], "content": m["content"]})
    if messages:
        # Cache the conversation so far; the next message (or tool round) reads it back cheaply.
        messages[-1]["content"] = [{"type": "text", "text": messages[-1]["content"], "cache_control": CACHE}]
    return messages


def _settings_lines(settings):
    lines = ["פניות ביוזמתך:"]
    lines.append(f"• הודעה יומית: כל יום ב-{settings['daily_time']}" if settings["daily_time"]
                 else "• הודעה יומית: כבויה")
    lines.append(f"• פניות מזדמנות לפי מה שעולה בשיחה: {'מופעלות' if settings['followups'] else 'כבויות'}")
    for r in settings["scheduled"]:
        who = " (הוא ביקש)" if r["requested_by"] == "user" else ""
        lines.append(f"• מתוכנן #{r['id']}: {clock.short(r['due_at'])} - {r['purpose']}{who}")
    if not settings["scheduled"]:
        lines.append("• אין פניות מתוכננות")
    if not settings["asked"]:
        lines.append("עוד לא שאלת אותו איך הוא מעדיף שתפנה אליו ביוזמתך (ההגדרות למעלה הן ברירת מחדל). "
                     "כשיגיע רגע רגוע וטבעי - שאל.")
    return lines


def _note(window, settings, replying):
    """Fresh context for this moment - sent after the conversation, so the cached prefix stays intact."""
    now = clock.now()
    lines = ["[הקשר עדכני מהמערכת - לא הודעה ממנו]", f"עכשיו: {clock.describe_now(now)}"]
    earlier = list(window)
    if replying:
        while earlier and earlier[-1]["role"] == "user":  # his current message(s)
            earlier.pop()
    if earlier:
        last = earlier[-1]
        gap = (now - last["at"]).total_seconds()
        if replying and last["kind"] == "proactive":
            lines.append(f"הוא עונה עכשיו להודעה ששלחת לו ביוזמתך ({clock.ago(gap)}).")
        elif gap >= 3 * 3600 or not replying:
            lines.append(f"ההודעה הקודמת ביניכם: {clock.ago(gap)}.")
        unanswered = 0
        for m in reversed(earlier):
            if m["role"] == "user":
                break
            unanswered += m["kind"] == "proactive"
        if unanswered >= reminders.MAX_UNANSWERED:
            lines.append(f"{unanswered} ההודעות האחרונות ששלחת לו ביוזמתך נשארו בלי מענה. "
                         "אם זה מתאים - בדוק בעדינות אם הקצב מתאים לו.")
    lines += _settings_lines(settings)
    return "\n".join(lines)


async def _context(user_id, first_name):
    memory_text, window, settings = await db.call(_load, user_id)
    if memory_text:
        known = memory_text
    else:
        known = prompts.MEMORY_EMPTY_EARLY if len(window) > 1 else prompts.MEMORY_EMPTY_FIRST
    system = [
        {"type": "text", "text": prompts.FRANKL_BASE, "cache_control": CACHE},
        {"type": "text", "text": prompts.MEMORY_BLOCK.format(first_name=first_name, memory=known),
         "cache_control": CACHE},
    ]
    window_start = window[0]["id"] if window else 2**62
    return system, window, settings, window_start


# ============================================================
# TOOLS
# ============================================================

async def _run_tool(user_id, first_name, window_start, block):
    args = block.input
    name = block.name.lower()
    try:
        if name == "search_past_conversations":
            result = await db.call(memory.search, user_id, first_name, args["query"], window_start)
        elif name == "schedule_message":
            result = await db.call(reminders.schedule, user_id, args["when"], args["purpose"],
                                   args["requested_by_user"])
        elif name == "cancel_scheduled_message":
            result = await db.call(reminders.cancel, user_id, args["id"])
        elif name == "set_checkin_preferences":
            result = await db.call(reminders.set_preferences, user_id, args["daily_time"], args["followups"])
        else:
            raise reminders.Rejected(f"אין כלי בשם {block.name}.")
        logger.info(f"Tool {name} for user {user_id}: {result[:200]}")
        return {"type": "tool_result", "tool_use_id": block.id, "content": result}
    except reminders.Rejected as e:
        return {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
    except Exception:
        logger.exception(f"Tool {name} failed")
        return {"type": "tool_result", "tool_use_id": block.id, "content": "שגיאה פנימית בכלי.", "is_error": True}


# ============================================================
# CONVERSATION
# ============================================================

async def reply(user_id, first_name) -> str:
    """Frankl's answer to the latest message(s), with tools available."""
    system, window, settings, window_start = await _context(user_id, first_name)
    messages = _history(window)
    if not messages or messages[-1]["role"] != "user":
        # A proactive message went out between his message and this reply.
        messages.append({"role": "user", "content": prompts.REPLY_AFTER_PROACTIVE})
    messages.append({"role": "system", "content": _note(window, settings, replying=True)})
    texts = []
    for round_number in range(MAX_TOOL_ROUNDS):
        last_round = round_number == MAX_TOOL_ROUNDS - 1
        response = await llm.create(
            model=llm.CHAT_MODEL, max_tokens=4000, system=system, tools=TOOLS,
            tool_choice={"type": "none" if last_round else "auto"},
            messages=messages, output_config={"effort": llm.CHAT_EFFORT})
        if response.stop_reason == "refusal":
            logger.warning(f"Reply declined: {response.stop_details}")
            return prompts.REFUSAL_TEXT
        texts.append(llm.text_of(response))
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": [
            await _run_tool(user_id, first_name, window_start, block) for block in tool_calls]})
    return "\n\n".join(text for text in texts if text) or prompts.ERROR_TEXT


async def compose_proactive(user_id, first_name, reminder) -> str:
    """A message Frankl sends on his own. Empty string if nothing could be written."""
    if reminder["kind"] == "daily":
        instruction = prompts.PROACTIVE_DAILY
    elif reminder["requested_by"] == "user":
        instruction = prompts.PROACTIVE_USER_REMINDER.format(purpose=reminder["purpose"])
    else:
        instruction = prompts.PROACTIVE_FOLLOWUP.format(purpose=reminder["purpose"])
    system, window, settings, _ = await _context(user_id, first_name)
    messages = _history(window)
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": prompts.PROACTIVE_NO_NEW_MESSAGE})
    messages.append({"role": "system", "content": _note(window, settings, replying=False) + "\n\n" + instruction})
    response = await llm.create(
        model=llm.CHAT_MODEL, max_tokens=2000, system=system, tools=TOOLS, tool_choice={"type": "none"},
        messages=messages, output_config={"effort": llm.CHAT_EFFORT})
    if response.stop_reason == "refusal":
        logger.warning(f"Proactive message declined: {response.stop_details}")
        return ""
    return llm.text_of(response)


async def summarize(user_id, first_name) -> str:
    system, window, settings, _ = await _context(user_id, first_name)
    messages = _history(window) + [
        {"role": "user", "content": prompts.SUMMARY_REQUEST},
        {"role": "system", "content": _note(window, settings, replying=False)},
    ]
    response = await llm.create(
        model=llm.CHAT_MODEL, max_tokens=6000, system=system, tools=TOOLS, tool_choice={"type": "none"},
        messages=messages, output_config={"effort": "medium"})
    if response.stop_reason == "refusal":
        return prompts.REFUSAL_TEXT
    return llm.text_of(response) or prompts.ERROR_TEXT
