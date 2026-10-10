import asyncio
import collections
import contextlib
import datetime
import hashlib
import hmac
import html
import logging
import os
import secrets
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import brain
import clock
import db
import memory
import parliament
import personas
import prompts
import reminders
import spaces
from spaces import Space

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO - and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "1126985722"))
DEBOUNCE_SECONDS = 2.0       # wait for a burst of short messages before answering them together
TELEGRAM_LIMIT = 4000
KEEP_AWAKE_SECONDS = 10 * 60
WAKE_LOOKAHEAD = datetime.timedelta(minutes=90)  # longer than the gap between hourly wake-up pings
LEAKED_SECRETS = {"frankl2024secret"}  # the old default, visible in the public repo
PARLIAMENT = personas.PARLIAMENT


def _load_secret():
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret in LEAKED_SECRETS:
        logger.error("WEBHOOK_SECRET is the old default that is public on GitHub - ignoring it. Set a new one.")
        secret = ""
    if not secret:
        logger.warning("WEBHOOK_SECRET is not set - using a random one for this run (/tick can't be called from outside).")
        secret = secrets.token_urlsafe(32)
    return secret


WEBHOOK_SECRET = _load_secret()
# Telegram echoes this in a header on every update. Derived, so it only uses characters Telegram allows.
TELEGRAM_SECRET_TOKEN = hashlib.sha256(f"telegram:{WEBHOOK_SECRET}".encode()).hexdigest()
public_url = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
              or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
              ).rstrip("/")
# Render's free plan sleeps after 15 idle minutes. A GitHub Actions ping wakes the bot every hour
# (.github/workflows/wake.yml); when a scheduled message is due soon, the bot pings its own address
# to stay awake until it is sent. Staying awake all the time would use up the free plan's monthly
# hours, which are shared with the account's other free services.
KEEP_AWAKE = os.environ.get("KEEP_AWAKE", "1" if os.environ.get("RENDER_EXTERNAL_URL") else "0") == "1"

bot_app: Application = None

# ============================================================
# HELPERS
# ============================================================


def split_text(text, limit=TELEGRAM_LIMIT):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return [chunk for chunk in chunks if chunk]


async def send_text(bot, space, text, parse_mode=None, reply_markup=None):
    """Send to a conversation (private chat or topic). Returns the last message sent."""
    chunks = split_text(text)
    sent = None
    for i, chunk in enumerate(chunks):
        sent = await bot.send_message(chat_id=space.chat_id, text=chunk, parse_mode=parse_mode,
                                      message_thread_id=space.thread_id,
                                      reply_markup=reply_markup if i == len(chunks) - 1 else None)
    return sent


@contextlib.asynccontextmanager
async def typing(bot, space):
    """Show 'typing...' until the block ends (Telegram clears it after ~5 seconds)."""
    async def keep_typing():
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=space.chat_id, action=ChatAction.TYPING,
                                           message_thread_id=space.thread_id)
            await asyncio.sleep(4.5)
    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


async def react(bot, message, emoji):
    with contextlib.suppress(Exception):
        await bot.set_message_reaction(chat_id=message.chat_id, message_id=message.message_id, reaction=emoji)


def memory_html(text):
    """The memory document for Telegram: section titles in bold."""
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            lines.append(f"\n<b>{html.escape(line[3:].strip(), quote=False)}</b>")
        else:
            lines.append(html.escape(line, quote=False))
    return "\n".join(lines).strip()


def has_content(section_text):
    return bool(section_text) and section_text != "אין עדיין"


def speaker_title(speaker):
    persona = personas.BY_KEY.get(speaker)
    return persona.title if persona else f"{prompts.CHAIR_EMOJI} {prompts.CHAIR_NAME}"

# ============================================================
# WHERE A MESSAGE BELONGS
# ============================================================


async def is_allowed(update: Update) -> bool:
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER_ID:
        # Answer strangers in a private chat; stay quiet in groups.
        if update.effective_message and update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text(prompts.PRIVATE_BOT)
        return False
    return True


async def register(user):
    await db.call(memory.save_user, user.id, user.first_name, user.username)
    await db.call(reminders.seed_defaults, user.id)


async def resolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The conversation a message belongs to, or None (after pointing him to the right place)."""
    chat = update.effective_chat
    if chat.type == "private":
        return Space(chat.id, None, "frankl")
    message = update.effective_message
    thread_id = message.message_thread_id if message.is_topic_message else None
    if thread_id is not None:
        persona = await db.call(spaces.persona_of, chat.id, thread_id)
        if persona:
            return Space(chat.id, thread_id, persona)
    # The group's general topic, a topic we didn't open, or a group without topics.
    if not await db.call(spaces.in_chat, chat.id):
        await setup(context.bot, chat, update.effective_user.id)
    else:
        await send_guide(context.bot, chat.id, thread_id)
    return None


async def send_guide(bot, chat_id, thread_id=None):
    threads = await db.call(spaces.in_chat, chat_id)
    lines = [f"{personas.BY_KEY[p.key].title} - {p.domain}: {spaces.link(chat_id, threads[p.key])}"
             for p in personas.ALL if p.key in threads]
    if PARLIAMENT in threads:
        lines.append(f"{prompts.CHAIR_EMOJI} ישיבת פרלמנט: {spaces.link(chat_id, threads[PARLIAMENT])}")
    text = prompts.GROUP_GUIDE.format(emoji=prompts.CHAIR_EMOJI, links="\n".join(lines))
    return await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id,
                                  disable_web_page_preview=True)


async def setup(bot, chat, user_id):
    """Open a topic for every persona and one for the parliament, in a group with topics enabled."""
    if chat.type == "private":
        await bot.send_message(chat_id=chat.id, text=prompts.SETUP_NOT_FORUM)
        return
    if not chat.is_forum:
        await bot.send_message(chat_id=chat.id, text=prompts.SETUP_NOT_FORUM)
        return
    me = await bot.get_chat_member(chat.id, bot.id)
    if not getattr(me, "can_manage_topics", False):
        await bot.send_message(chat_id=chat.id, text=prompts.SETUP_NO_RIGHTS)
        return
    existing = await db.call(spaces.in_chat, chat.id)
    roster = "\n".join(f"{p.title} - {p.domain}" for p in personas.ALL)
    for key in [p.key for p in personas.ALL] + [PARLIAMENT]:
        if key in existing:
            continue
        persona = personas.BY_KEY.get(key)
        topic = await bot.create_forum_topic(chat.id, persona.title if persona else
                                             f"{prompts.CHAIR_EMOJI} ישיבת פרלמנט")
        await db.call(spaces.save, chat.id, topic.message_thread_id, user_id, key)
        if persona:
            known = await db.call(memory.latest_user_message_id, user_id, key)
            intro = f"{persona.short} כאן. ממשיכים מאיפה שעצרנו." if known else persona.opening
        else:
            intro = prompts.PARLIAMENT_INTRO.format(emoji=prompts.CHAIR_EMOJI, roster=roster)
        await bot.send_message(chat_id=chat.id, text=intro, message_thread_id=topic.message_thread_id)
    guide = await send_guide(bot, chat.id)
    with contextlib.suppress(Exception):
        await bot.pin_chat_message(chat_id=chat.id, message_id=guide.message_id, disable_notification=True)
    await bot.send_message(chat_id=chat.id, text=prompts.SETUP_DONE)

# ============================================================
# COMMANDS
# ============================================================


async def command_space(update, context):
    """The conversation a command was sent in, or None."""
    if not await is_allowed(update):
        return None
    return await resolve(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await register(update.effective_user)
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(personas.BY_KEY["frankl"].opening)
    else:
        await resolve(update, context)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await register(update.effective_user)
    await setup(context.bot, update.effective_chat, update.effective_user.id)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    text = await db.call(memory.get_memory, update.effective_user.id, space.persona)
    if not text:
        await send_text(context.bot, space, "עדיין בתחילת הדרך. ככל שנדבר יותר - אדע אותך יותר.")
        return
    title = "מה שנאמר בישיבות עד עכשיו:" if space.persona == PARLIAMENT else "מה שאני יודע עליך:"
    await send_text(context.bot, space, f"<b>{title}</b>\n" + memory_html(text), ParseMode.HTML)


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    title = "מסקנות והחלטות" if space.persona == PARLIAMENT else "יעדים ושאיפות"
    body = memory.section(await db.call(memory.get_memory, update.effective_user.id, space.persona), title)
    if not has_content(body):
        await send_text(context.bot, space, "עדיין לא תיעדנו כאן יעדים. ספר לי - מה אתה שואף אליו?")
        return
    await send_text(context.bot, space, f"<b>{title}:</b>\n\n" + html.escape(body, quote=False), ParseMode.HTML)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    text = await db.call(memory.get_memory, update.effective_user.id, space.persona)
    titles = ("שאלות פתוחות", "יומן הישיבות") if space.persona == PARLIAMENT else ("חוטים פתוחים", "יומן השיחות")
    parts = [(title, memory.section(text, title)) for title in titles]
    parts = [(title, body) for title, body in parts if has_content(body)]
    if not parts:
        await send_text(context.bot, space, "עדיין לא תיעדנו כאן אירועים. ספר לי מה קורה.")
        return
    await send_text(context.bot, space, "\n\n".join(
        f"<b>{title}:</b>\n{html.escape(body, quote=False)}" for title, body in parts), ParseMode.HTML)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    if space.persona == PARLIAMENT:
        await send_text(context.bot, space, "בפרלמנט אין פניות יזומות - כל אחד כותב לך ביוזמתו בשיחה האישית שלו.")
        return
    settings = await db.call(reminders.get_settings, update.effective_user.id, space.persona)
    lines = ["<b>מתי אני כותב לך ביוזמתי:</b>",
             f"• הודעה יומית: כל יום ב-{settings['daily_time']}" if settings["daily_time"] else "• הודעה יומית: כבויה",
             f"• פניות מזדמנות לפי מה שעולה בשיחה: {'מופעלות' if settings['followups'] else 'כבויות'}"]
    if settings["scheduled"]:
        lines += ["", "<b>מתוכנן:</b>"] + [
            f"• {clock.short(r['due_at'])} - {html.escape(r['purpose'], quote=False)}"
            for r in settings["scheduled"]]
    lines += ["", 'רוצה לשנות? פשוט תגיד לי - למשל "תכתוב לי כל ערב ב-21:00" או "אל תכתוב לי ביוזמתך".']
    await send_text(context.bot, space, "\n".join(lines), ParseMode.HTML)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    user = update.effective_user
    if space.persona == PARLIAMENT:
        await run_parliament(context.bot, space, user, parliament.Plan(action="summary"), update.effective_message)
        return
    await send_text(context.bot, space, "רגע, אני מסכם את הדרך שעברנו...")
    async with typing(context.bot, space), brain.conversation_lock(user.id, space.persona):
        try:
            text = await brain.summarize(user.id, user.first_name, space.persona)
        except Exception:
            logger.exception("Summary error")
            text = "סליחה, לא הצלחתי לייצר סיכום כרגע."
    await send_text(context.bot, space, text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    user = update.effective_user
    if space.persona == PARLIAMENT:
        await run_parliament(context.bot, space, user, parliament.Plan(action="new_topic"), update.effective_message)
        return
    async with typing(context.bot, space), brain.conversation_lock(user.id, space.persona):
        cleared = await memory.clear_history(user.id, space.persona)
    await send_text(context.bot, space, "היסטוריית השיחה נמחקה. הזיכרון העמוק נשמר." if cleared else
                    "לא הצלחתי לשמור את הזיכרון כרגע, אז לא מחקתי כלום. נסה שוב עוד מעט.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await update.effective_message.reply_text(prompts.HELP_TEXT)

# ============================================================
# CONVERSATIONS
# ============================================================

_arrival_locks: dict[int, asyncio.Lock] = {}


async def save_incoming(user, persona, text, kind="chat"):
    # Updates are handled concurrently; this first wait keeps his messages in the order they arrived.
    async with _arrival_locks.setdefault(user.id, asyncio.Lock()):
        await register(user)
        return await db.call(memory.save_message, user.id, persona, "user", text, kind)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    space = await resolve(update, context)
    if not space: return
    if space.persona == PARLIAMENT:
        await handle_parliament(update, context, space)
        return
    user = update.effective_user
    message_id = await save_incoming(user, space.persona, update.effective_message.text)
    async with typing(context.bot, space):
        await asyncio.sleep(DEBOUNCE_SECONDS)
        async with brain.conversation_lock(user.id, space.persona):
            if await db.call(memory.latest_user_message_id, user.id, space.persona) != message_id:
                return  # a newer message arrived - its handler answers them all together
            try:
                reply = await brain.reply(user.id, user.first_name, space.persona)
            except Exception:
                logger.exception("Reply error")
                reply = prompts.ERROR_TEXT
            if reply not in (prompts.ERROR_TEXT, prompts.REFUSAL_TEXT):
                await db.call(memory.save_message, user.id, space.persona, "assistant", reply)
            await send_text(context.bot, space, reply)
            logger.info(f"Reply sent by {space.persona} ({len(reply)} chars)")
    memory.consolidate_soon(user.id, space.persona)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await update.effective_message.reply_text(prompts.TEXT_ONLY)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error", exc_info=context.error)

# ============================================================
# THE PARLIAMENT
# ============================================================

_rounds: dict[int, asyncio.Task] = {}
_keyboards: dict[int, tuple] = {}   # user -> (chat_id, message_id) of the message carrying the buttons

ROUND_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🗣️ תדונו ביניכם", callback_data="parl:debate"),
     InlineKeyboardButton("🧭 סכמו", callback_data="parl:summary")],
    [InlineKeyboardButton(p.emoji, callback_data=f"parl:ask:{p.key}") for p in personas.ALL],
    [InlineKeyboardButton("🆕 נושא חדש", callback_data="parl:new")],
])
PAUSE_BUTTONS = InlineKeyboardMarkup([[
    InlineKeyboardButton("▶️ המשיכו", callback_data="parl:more"),
    InlineKeyboardButton("🧭 סכמו", callback_data="parl:summary"),
]])
BUTTON_PLANS = {
    "debate": ("[ביקשת: תדונו ביניכם]", lambda: parliament.Plan(mode_change="debate")),
    "summary": ("[ביקשת: סכמו]", lambda: parliament.Plan(action="summary")),
    "new": ("[ביקשת: נושא חדש]", lambda: parliament.Plan(action="new_topic")),
    "more": ("[ביקשת: המשיכו]", lambda: parliament.Plan(action="more")),
}


async def _move_buttons(bot, user_id, message, markup):
    """Only the latest round carries the buttons."""
    previous = _keyboards.pop(user_id, None)
    if previous:
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(chat_id=previous[0], message_id=previous[1], reply_markup=None)
    if message is not None and markup is not None:
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(chat_id=message.chat_id, message_id=message.message_id,
                                                reply_markup=markup)
            _keyboards[user_id] = (message.chat_id, message.message_id)


def interrupt(user_id):
    """A new message from him stops a discussion that is still going."""
    task = _rounds.get(user_id)
    if task and not task.done():
        task.cancel()


async def run_parliament(bot, space, user, plan, trigger_message):
    """Carry out a plan in the parliament topic, sending each speaker's words as they come."""
    last = None

    async def emit(speaker, text):
        nonlocal last
        header = html.escape(speaker_title(speaker), quote=False)
        last = await send_text(bot, space, f"<b>{header}</b>\n{html.escape(text, quote=False)}", ParseMode.HTML)
        await db.call(memory.save_message, user.id, PARLIAMENT, "assistant", text, "chat", speaker)

    interrupt(user.id)
    async with brain.conversation_lock(user.id, PARLIAMENT):
        task = asyncio.create_task(parliament.respond(user.id, user.first_name, plan, emit))
        _rounds[user.id] = task
        try:
            async with typing(bot, space):
                outcome = await task
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                return  # interrupted by a newer message
            raise
        except Exception:
            logger.exception("Parliament round failed")
            await send_text(bot, space, prompts.ERROR_TEXT)
            return
        finally:
            if _rounds.get(user.id) is task:
                del _rounds[user.id]
    if outcome == "round":
        await _move_buttons(bot, user.id, last, ROUND_BUTTONS)
    elif outcome == "paused":
        await _move_buttons(bot, user.id, await send_text(bot, space, prompts.DEBATE_PAUSED), PAUSE_BUTTONS)
    elif outcome == "silent":
        await react(bot, trigger_message, "✍")
    elif outcome == "ack":
        await react(bot, trigger_message, "👌")
    memory.consolidate_soon(user.id, PARLIAMENT)


async def handle_parliament(update, context, space):
    user = update.effective_user
    text = update.effective_message.text
    interrupt(user.id)
    message_id = await save_incoming(user, PARLIAMENT, text)
    await asyncio.sleep(DEBOUNCE_SECONDS)
    if await db.call(memory.latest_user_message_id, user.id, PARLIAMENT) != message_id:
        return  # a newer message arrived - its handler answers them all together
    state = await db.call(parliament.get_state, user.id)
    _, window, _ = await db.call(parliament.load, user.id)
    plan = await parliament.route(text, state, window, user.first_name)
    logger.info(f"Parliament plan: {plan}")
    await run_parliament(context.bot, space, user, plan, update.effective_message)


async def on_parliament_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID or query.message is None:
        await query.answer()
        return
    await query.answer()
    chat_id, thread_id = query.message.chat_id, query.message.message_thread_id
    if await db.call(spaces.persona_of, chat_id, thread_id) != PARLIAMENT:
        return
    space = Space(chat_id, thread_id, PARLIAMENT)
    action = query.data.split(":")[1:]
    if action[0] == "ask" and action[1] in personas.BY_KEY:
        label = f"[ביקשת לשמוע עוד מ{personas.BY_KEY[action[1]].short}]"
        plan = parliament.Plan(speakers=[action[1]], focus="הוא ביקש לשמוע ממך עוד - הרחב או הגב למה שנאמר")
    elif action[0] in BUTTON_PLANS:
        label, make = BUTTON_PLANS[action[0]]
        plan = make()
    else:
        return
    await save_incoming(query.from_user, PARLIAMENT, label, "action")
    await run_parliament(context.bot, space, query.from_user, plan, query.message)

# ============================================================
# MESSAGES THE PERSONAS SEND ON THEIR OWN
# ============================================================

_due_lock = asyncio.Lock()


async def deliver(reminder):
    user_id, persona = reminder["user_id"], reminder["persona"]
    space = await db.call(spaces.home, user_id, persona) if user_id == ALLOWED_USER_ID else None
    if space is None or persona not in personas.BY_KEY:
        await db.call(reminders.finish, reminder["id"], "cancelled")
        return
    last_user_age, unanswered = await db.call(reminders.activity, user_id, persona)
    action = reminders.decide(reminder, clock.now(), last_user_age, unanswered)
    if action == "postpone":
        await db.call(reminders.postpone, reminder["id"], clock.now() + reminders.POSTPONE)
        return
    if action != "send":
        logger.info(f"Reminder {reminder['id']} ({persona}, {reminder['kind']}): {action}")
        await db.call(reminders.finish, reminder["id"], "skipped" if action == "skip" else action)
        return
    async with brain.conversation_lock(user_id, persona):
        text = await brain.compose_proactive(user_id, reminder["first_name"], reminder)
        if not text:
            raise RuntimeError("empty proactive message")
        await send_text(bot_app.bot, space, text)
        await db.call(memory.save_message, user_id, persona, "assistant", text, "proactive")
    await db.call(reminders.finish, reminder["id"], "sent")
    logger.info(f"Reminder {reminder['id']} ({persona}, {reminder['kind']}) sent")


async def run_due_reminders():
    if _due_lock.locked():
        return
    async with _due_lock:
        for reminder in await db.call(reminders.claim_due):
            try:
                await deliver(reminder)
            except Exception:
                logger.exception(f"Reminder {reminder['id']} failed")
                await db.call(reminders.finish, reminder["id"], "failed")


async def reminders_loop():
    last_ping = float("-inf")
    while True:
        try:
            await run_due_reminders()
            if (KEEP_AWAKE and public_url and time.monotonic() - last_ping > KEEP_AWAKE_SECONDS
                    and await db.call(reminders.due_soon, WAKE_LOOKAHEAD)):
                last_ping = time.monotonic()
                async with httpx.AsyncClient(timeout=30) as client:
                    await client.get(f"{public_url}/health")
        except Exception:
            logger.exception("Reminders loop error")
        await asyncio.sleep(60)


async def memory_loop():
    while True:
        try:
            await memory.consolidate_idle([ALLOWED_USER_ID])
        except Exception:
            logger.exception("Memory loop error")
        await asyncio.sleep(60)

# ============================================================
# FASTAPI + WEBHOOK
# ============================================================


async def set_webhook():
    global public_url
    if not public_url:
        # Keep the host Telegram already uses (set by an earlier version of the bot).
        current = (await bot_app.bot.get_webhook_info()).url
        public_url = current.split("/webhook")[0] if current else ""
    if not public_url:
        logger.error("No public URL known - set PUBLIC_URL so Telegram can reach the bot")
        return
    # One connection at a time, so Telegram delivers his messages in order.
    await bot_app.bot.set_webhook(f"{public_url}/webhook", secret_token=TELEGRAM_SECRET_TOKEN, max_connections=1)
    logger.info(f"Webhook set: {public_url}/webhook")


# Media he might send; service messages (topic created, message pinned...) are ignored.
MEDIA = (filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE
         | filters.Document.ALL | filters.Sticker.ALL | filters.ANIMATION)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    await db.call(db.init_db)
    await db.call(reminders.recover)
    await db.call(reminders.seed_defaults, ALLOWED_USER_ID)
    bot_app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).updater(None).concurrent_updates(True).build()
    for name, handler in (("start", cmd_start), ("setup", cmd_setup), ("profile", cmd_profile), ("goals", cmd_goals),
                          ("events", cmd_events), ("reminders", cmd_reminders), ("summary", cmd_summary),
                          ("clear", cmd_clear), ("help", cmd_help)):
        bot_app.add_handler(CommandHandler(name, handler))
    bot_app.add_handler(CallbackQueryHandler(on_parliament_button, pattern=r"^parl:"))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & MEDIA, handle_other))
    bot_app.add_error_handler(on_error)
    await bot_app.initialize()
    await bot_app.start()
    try:
        await set_webhook()
    except Exception:
        logger.exception("Setting the webhook failed")
    loops = [asyncio.create_task(reminders_loop()), asyncio.create_task(memory_loop())]
    yield
    for task in loops:
        task.cancel()
    await bot_app.stop()
    await bot_app.shutdown()


fastapi_app = FastAPI(lifespan=lifespan)
_seen_updates = collections.deque(maxlen=1000)
_tick_tasks: set[asyncio.Task] = set()


def _matches(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


@fastapi_app.post("/webhook")
async def webhook(request: Request):
    if not _matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_SECRET_TOKEN):
        return Response("unauthorized", status_code=403)
    data = await request.json()
    update_id = data.get("update_id")
    if update_id in _seen_updates:
        return Response("duplicate")
    _seen_updates.append(update_id)
    await bot_app.update_queue.put(Update.de_json(data, bot_app.bot))
    return Response("ok")


@fastapi_app.api_route("/tick", methods=["GET", "POST"])
async def tick(request: Request, secret: str = ""):
    """Optional external trigger for scheduled messages (the bot also checks every minute on its own)."""
    if not _matches(secret or request.headers.get("X-Cron-Secret", ""), WEBHOOK_SECRET):
        return Response("unauthorized", status_code=403)
    task = asyncio.create_task(run_due_reminders())
    _tick_tasks.add(task)
    task.add_done_callback(_tick_tasks.discard)
    return Response("ok")


@fastapi_app.get("/morning")
async def morning_trigger():
    """Kept so old cron jobs don't fail. It only wakes the service - the bot schedules its messages itself."""
    return Response("ok")


@fastapi_app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "alive", "bot": "Viktor Frankl"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
