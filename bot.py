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
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import brain
import clock
import db
import memory
import parliament
import personas
import prompts
import reminders
import spaces
import voices
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


async def send_text(bot, space, text, parse_mode=None, reply_markup=None, **options):
    """Send to a conversation (private chat or topic). Returns the last message sent."""
    chunks = split_text(text)
    sent = None
    for i, chunk in enumerate(chunks):
        sent = await bot.send_message(chat_id=space.chat_id, text=chunk, parse_mode=parse_mode,
                                      message_thread_id=space.thread_id,
                                      reply_markup=reply_markup if i == len(chunks) - 1 else None, **options)
    return sent


def speaker_of(space):
    """Who speaks in a conversation: its persona, or the chair in the parliament."""
    return voices.CHAIR if space.persona == PARLIAMENT else space.persona


async def post(space, speaker, text, parse_mode=None, reply_markup=None, sign=True, **options):
    """Send as a persona (or the chair), through his own bot. In the group, when his bot can't post there
    (not connected yet, or not added to the group), the main bot posts for him - signed with his name."""
    bot = voices.bot_for(speaker)
    if bot is not bot_app.bot:
        try:
            return await send_text(bot, space, text, parse_mode, reply_markup, **options)
        except Forbidden:
            if space.private:
                raise  # a private chat belongs to one bot
            logger.warning(f"{speaker}'s bot can't post in the group - the main bot posts for him")
    if sign and speaker != voices.MAIN:
        body = text if parse_mode == ParseMode.HTML else html.escape(text, quote=False)
        text, parse_mode = f"<b>{html.escape(speaker_title(speaker), quote=False)}</b>\n{body}", ParseMode.HTML
    return await send_text(bot_app.bot, space, text, parse_mode, reply_markup, **options)


async def answer(space, text, parse_mode=None):
    """A reply in a conversation, from whoever speaks there."""
    return await post(space, speaker_of(space), text, parse_mode)


@contextlib.asynccontextmanager
async def typing(space, writing):
    """Show 'typing...' until the block ends, for each speaker writing() names, through his own bot.
    (Telegram clears it after ~5 seconds, or when he sends.)"""
    async def show(key):
        action = dict(chat_id=space.chat_id, action=ChatAction.TYPING, message_thread_id=space.thread_id)
        try:
            await voices.bot_for(key).send_chat_action(**action)
        except Forbidden:
            if not space.private:  # his bot isn't in the group - the main bot posts for him
                with contextlib.suppress(Exception):
                    await bot_app.bot.send_chat_action(**action)
        except Exception:
            pass

    async def keep_typing():
        shown = {}
        while True:
            now = set(writing())
            shown = {key: at for key, at in shown.items() if key in now}
            for key in now:
                if time.monotonic() - shown.get(key, float("-inf")) > 4.5:
                    shown[key] = time.monotonic()
                    await show(key)
            await asyncio.sleep(0.1)
    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


async def react(message, emoji):
    with contextlib.suppress(Exception):
        await bot_app.bot.set_message_reaction(chat_id=message.chat_id, message_id=message.message_id,
                                               reaction=emoji)


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
        key = voices.key_of(update.get_bot())   # the main bot's private chat is Frankl's
        if key == voices.CHAIR:
            await update.effective_message.reply_text(prompts.CHAIR_PRIVATE)
            return None
        return Space(chat.id, None, key) if key else None
    message = update.effective_message
    thread_id = message.message_thread_id if message.is_topic_message else None
    if thread_id is not None:
        persona = await db.call(spaces.persona_of, chat.id, thread_id)
        if persona:
            return Space(chat.id, thread_id, persona)
    # The group's general topic, a topic we didn't open, or a group without topics.
    if not await db.call(spaces.in_chat, chat.id):
        await setup(chat, update.effective_user.id)
    else:
        await post(Space(chat.id, thread_id, PARLIAMENT), voices.CHAIR, await guide_text(chat.id), sign=False,
                   disable_web_page_preview=True)
    return None


async def guide_text(chat_id):
    threads = await db.call(spaces.in_chat, chat_id)
    lines = [f"{p.title} - {p.domain}: {spaces.link(chat_id, threads[p.key])}"
             for p in personas.ALL if p.key in threads]
    if PARLIAMENT in threads:
        lines.append(f"{prompts.CHAIR_EMOJI} ישיבת פרלמנט: {spaces.link(chat_id, threads[PARLIAMENT])}")
    return prompts.GROUP_GUIDE.format(emoji=prompts.CHAIR_EMOJI, links="\n".join(lines))


async def refresh_guide(chat_id):
    """Keep one pinned guide in the group's general topic, linking to the current topics."""
    text = await guide_text(chat_id)
    old = await db.call(spaces.guide, chat_id)
    by = voices.CHAIR if voices.has_own(voices.CHAIR) else voices.MAIN
    if old and old["guide_by"] == by:
        try:
            await voices.bot_for(by).edit_message_text(text, chat_id=chat_id, message_id=old["guide_message_id"],
                                                       disable_web_page_preview=True)
            return
        except BadRequest as error:
            if "not modified" in str(error):
                return
            logger.warning(f"Editing the guide failed, sending a new one: {error}")
        except Exception as error:
            logger.warning(f"Editing the guide failed, sending a new one: {error!r}")
    guide = await post(Space(chat_id, None, PARLIAMENT), voices.CHAIR, text, sign=False, disable_web_page_preview=True)
    main = bot_app.bot
    with contextlib.suppress(Exception):
        if old:
            await main.delete_message(chat_id, old["guide_message_id"])
        else:  # a guide from before they were kept track of
            await main.unpin_all_general_forum_topic_messages(chat_id)
    with contextlib.suppress(Exception):
        await main.pin_chat_message(chat_id=chat_id, message_id=guide.message_id, disable_notification=True)
    await db.call(spaces.save_guide, chat_id, guide.message_id, voices.key_of(guide.get_bot()))


async def intro_text(user_id, key):
    """How a persona (or the chair, for the parliament) opens a conversation."""
    persona = personas.BY_KEY.get(key)
    if persona is None:
        roster = "\n".join(f"{p.title} - {p.domain}" for p in personas.ALL)
        return prompts.PARLIAMENT_INTRO.format(emoji=prompts.CHAIR_EMOJI, roster=roster)
    known = await db.call(memory.latest_user_message_id, user_id, key)
    return f"{persona.short} כאן. ממשיכים מאיפה שעצרנו." if known else persona.opening


_layout_lock = asyncio.Lock()   # setup and reopening topics change the same topics and guide


async def open_topic(chat_id, user_id, key):
    """A topic for a persona (or the parliament), opened with a word from him (or from the chair)."""
    persona = personas.BY_KEY.get(key)
    topic = await bot_app.bot.create_forum_topic(chat_id, persona.title if persona else
                                                 f"{prompts.CHAIR_EMOJI} ישיבת פרלמנט")
    space = Space(chat_id, topic.message_thread_id, key)
    await db.call(spaces.save, chat_id, space.thread_id, user_id, key)
    speaker = speaker_of(space)
    intro = await post(space, speaker, await intro_text(user_id, key))
    if voices.key_of(intro.get_bot()) == speaker:
        await db.call(spaces.set_voiced, chat_id, space.thread_id)


async def setup(chat, user_id):
    """Open a topic for every persona and one for the parliament, in a group with topics enabled."""
    main = bot_app.bot
    if not chat.is_forum:
        await main.send_message(chat_id=chat.id, text=prompts.SETUP_NOT_FORUM)
        return
    me = await main.get_chat_member(chat.id, main.id)
    if not getattr(me, "can_manage_topics", False):
        await main.send_message(chat_id=chat.id, text=prompts.SETUP_NO_RIGHTS)
        return
    async with _layout_lock:
        existing = await db.call(spaces.in_chat, chat.id)
        for key in [p.key for p in personas.ALL] + [PARLIAMENT]:
            if key not in existing:
                await open_topic(chat.id, user_id, key)
        await refresh_guide(chat.id)
    await post(Space(chat.id, None, PARLIAMENT), voices.CHAIR, prompts.SETUP_DONE, sign=False)

# ============================================================
# A BOT OF HIS OWN FOR EVERY PERSONA
# ============================================================


def voice_url(key):
    return f"{public_url}/webhook/{key}"


async def in_group(bot, chat_id):
    try:
        member = await bot_app.bot.get_chat_member(chat_id, bot.id)
    except Exception:
        return False
    return member.status in ("member", "administrator", "restricted")


async def voice_joined(key, chat_id, user_id):
    """A persona's own bot is in the group: his topic is opened again, this time by him."""
    target = PARLIAMENT if key == voices.CHAIR else key
    async with _layout_lock:
        thread_id = (await db.call(spaces.in_chat, chat_id)).get(target)
        if thread_id is None or await db.call(spaces.is_voiced, chat_id, thread_id):
            return
        reopened = False
        # Only a topic with nothing from him in it yet - a conversation is never deleted.
        if await db.call(memory.latest_user_message_id, user_id, target) is None:
            try:
                await bot_app.bot.delete_forum_topic(chat_id, thread_id)
                reopened = True
            except Exception as error:
                logger.warning(f"Couldn't reopen the topic of {target}: {error!r}")
        if reopened:
            await db.call(spaces.remove, chat_id, thread_id)
            await open_topic(chat_id, user_id, target)
        else:
            await db.call(spaces.set_voiced, chat_id, thread_id)
        if reopened or key == voices.CHAIR:  # new links, or the chair takes over the guide
            await refresh_guide(chat_id)


async def add_voice(update):
    """He forwarded BotFather's message: the new bot becomes the voice of a persona (or of the chair)."""
    message = update.effective_message
    token = voices.TOKEN_RE.search(message.text).group(0)
    with contextlib.suppress(Exception):
        await message.delete()   # the token shouldn't stay in the chat
    bot = await voices.connect(token)
    if bot is None:
        await message.chat.send_message(prompts.VOICE_BAD_TOKEN)
        return
    if bot.id == bot_app.bot.id:
        await bot.shutdown()
        await message.chat.send_message(prompts.VOICE_IS_MAIN)
        return
    key = voices.guess(bot)
    if key is None:
        voices.pending[bot.id] = (bot, token)
        buttons = [[InlineKeyboardButton(voices.title(k), callback_data=f"voice:{bot.id}:{k}")] for k in voices.KEYS]
        await message.chat.send_message(prompts.VOICE_WHO.format(username=bot.username),
                                        reply_markup=InlineKeyboardMarkup(buttons))
        return
    await connect_voice(message.chat, update.effective_user.id, key, bot, token)


async def connect_voice(chat, user_id, key, bot, token):
    try:
        await voices.assign(key, bot, token, voice_url(key), TELEGRAM_SECRET_TOKEN)
    except Exception as error:
        logger.error(f"Connecting {key}'s bot failed: {voices.describe_error(error, bot)}")
        await chat.send_message(prompts.ERROR_TEXT)
        return
    lines = [prompts.VOICE_CONNECTED.format(title=voices.title(key), username=bot.username)]
    group = await db.call(spaces.group_of, user_id)
    if group is not None and await in_group(bot, group):
        await voice_joined(key, group, user_id)
        lines.append(prompts.VOICE_IN_GROUP)
    else:
        lines.append(prompts.VOICE_ADD_TO_GROUP.format(username=bot.username))
    missing = voices.missing()
    lines.append(prompts.VOICE_REMAINING.format(names=", ".join(voices.name(k) for k in missing)) if missing
                 else prompts.VOICE_ALL_DONE)
    await chat.send_message("\n".join(lines))


async def on_voice_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID or query.message is None:
        return
    _, bot_id, key = query.data.split(":")
    found = voices.pending.pop(int(bot_id), None)
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    if found is None or key not in voices.KEYS:
        await query.message.chat.send_message(prompts.VOICE_EXPIRED)
        return
    await connect_voice(query.message.chat, query.from_user.id, key, *found)


async def on_my_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One of the bots was added to a group: by him - welcome; by anyone else - it leaves at once."""
    change = update.my_chat_member
    if change.chat.type == "private" or change.new_chat_member.status not in ("member", "administrator"):
        return
    bot = update.get_bot()
    if change.from_user.id != ALLOWED_USER_ID:
        logger.warning(f"Someone else added a bot to chat {change.chat.id} - leaving it")
        with contextlib.suppress(Exception):
            await bot.leave_chat(change.chat.id)
        return
    key = voices.key_of(bot)
    if key not in (None, voices.MAIN) and await db.call(spaces.in_chat, change.chat.id):
        await voice_joined(key, change.chat.id, change.from_user.id)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """His group is his alone: anyone else who joins is removed (bots he adds may stay)."""
    message = update.effective_message
    if not await db.call(spaces.in_chat, message.chat_id):
        return
    for member in message.new_chat_members:
        if member.is_bot or member.id == ALLOWED_USER_ID:
            continue
        try:
            await bot_app.bot.ban_chat_member(message.chat_id, member.id)
            await bot_app.bot.unban_chat_member(message.chat_id, member.id)  # removed, not banned for good
        except Exception as error:
            logger.error(f"Couldn't remove a stranger from the group: {error!r}")
            continue
        logger.warning(f"Removed a stranger ({member.id}) from the group")
        await post(Space(message.chat_id, None, PARLIAMENT), voices.CHAIR,
                   prompts.STRANGER_REMOVED.format(name=member.full_name), sign=False)

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
    space = await resolve(update, context)
    if space:
        await answer(space, await intro_text(update.effective_user.id, space.persona))


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await register(update.effective_user)
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(prompts.SETUP_NOT_FORUM)
        return
    await setup(update.effective_chat, update.effective_user.id)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    text = await db.call(memory.get_memory, update.effective_user.id, space.persona)
    if not text:
        await answer(space, "עדיין בתחילת הדרך. ככל שנדבר יותר - אדע אותך יותר.")
        return
    title = "מה שנאמר בישיבות עד עכשיו:" if space.persona == PARLIAMENT else "מה שאני יודע עליך:"
    await answer(space, f"<b>{title}</b>\n" + memory_html(text), ParseMode.HTML)


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    title = "מסקנות והחלטות" if space.persona == PARLIAMENT else "יעדים ושאיפות"
    body = memory.section(await db.call(memory.get_memory, update.effective_user.id, space.persona), title)
    if not has_content(body):
        await answer(space, "עדיין לא תיעדנו כאן יעדים. ספר לי - מה אתה שואף אליו?")
        return
    await answer(space, f"<b>{title}:</b>\n\n" + html.escape(body, quote=False), ParseMode.HTML)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    text = await db.call(memory.get_memory, update.effective_user.id, space.persona)
    titles = ("שאלות פתוחות", "יומן הישיבות") if space.persona == PARLIAMENT else ("חוטים פתוחים", "יומן השיחות")
    parts = [(title, memory.section(text, title)) for title in titles]
    parts = [(title, body) for title, body in parts if has_content(body)]
    if not parts:
        await answer(space, "עדיין לא תיעדנו כאן אירועים. ספר לי מה קורה.")
        return
    await answer(space, "\n\n".join(
        f"<b>{title}:</b>\n{html.escape(body, quote=False)}" for title, body in parts), ParseMode.HTML)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    if space.persona == PARLIAMENT:
        await answer(space, "בפרלמנט אין פניות יזומות - כל אחד כותב לך ביוזמתו בשיחה האישית שלו.")
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
    await answer(space, "\n".join(lines), ParseMode.HTML)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    user = update.effective_user
    if space.persona == PARLIAMENT:
        await run_parliament(space, user, parliament.Plan(action="summary"), update.effective_message)
        return
    await answer(space, "רגע, אני מסכם את הדרך שעברנו...")
    async with typing(space, lambda: [space.persona]), brain.conversation_lock(user.id, space.persona):
        try:
            text = await brain.summarize(user.id, user.first_name, space.persona)
        except Exception:
            logger.exception("Summary error")
            text = "סליחה, לא הצלחתי לייצר סיכום כרגע."
    await answer(space, text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    space = await command_space(update, context)
    if not space: return
    user = update.effective_user
    if space.persona == PARLIAMENT:
        await run_parliament(space, user, parliament.Plan(action="new_topic"), update.effective_message)
        return
    async with typing(space, lambda: [space.persona]), brain.conversation_lock(user.id, space.persona):
        cleared = await memory.clear_history(user.id, space.persona)
    await answer(space, "היסטוריית השיחה נמחקה. הזיכרון העמוק נשמר." if cleared else
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
    if voices.TOKEN_RE.search(update.effective_message.text):
        await add_voice(update)   # never kept in a conversation
        return
    space = await resolve(update, context)
    if not space: return
    if space.persona == PARLIAMENT:
        await handle_parliament(update, context, space)
        return
    user = update.effective_user
    message_id = await save_incoming(user, space.persona, update.effective_message.text)
    async with typing(space, lambda: [space.persona]):
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
            await answer(space, reply)
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
_keyboards: dict[int, object] = {}  # user -> the message carrying the buttons

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


async def _move_buttons(user_id, message, markup):
    """Only the latest round carries the buttons. (Each message is edited by the bot that sent it.)"""
    previous = _keyboards.pop(user_id, None)
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous.edit_reply_markup(reply_markup=None)
    if message is not None and markup is not None:
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=markup)
            _keyboards[user_id] = message


def interrupt(user_id):
    """A new message from him stops a discussion that is still going."""
    task = _rounds.get(user_id)
    if task and not task.done():
        task.cancel()


async def run_parliament(space, user, plan, trigger_message):
    """Carry out a plan in the parliament topic, each speaker sending his words through his own bot."""
    last = None
    writing = set()

    def show(speaker, on):
        (writing.add if on else writing.discard)(speaker)

    async def emit(speaker, text):
        nonlocal last
        writing.discard(speaker)
        last = await post(space, speaker, text)
        await db.call(memory.save_message, user.id, PARLIAMENT, "assistant", text, "chat", speaker)

    interrupt(user.id)
    async with brain.conversation_lock(user.id, PARLIAMENT):
        task = asyncio.create_task(parliament.respond(user.id, user.first_name, plan, emit, show))
        _rounds[user.id] = task
        try:
            async with typing(space, lambda: writing):
                outcome = await task
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                return  # interrupted by a newer message
            raise
        except Exception:
            logger.exception("Parliament round failed")
            await post(space, voices.CHAIR, prompts.ERROR_TEXT)
            return
        finally:
            if _rounds.get(user.id) is task:
                del _rounds[user.id]
    if outcome == "round":
        await _move_buttons(user.id, last, ROUND_BUTTONS)
    elif outcome == "paused":
        await _move_buttons(user.id, await post(space, voices.CHAIR, prompts.DEBATE_PAUSED), PAUSE_BUTTONS)
    elif outcome == "silent":
        await react(trigger_message, "✍")
    elif outcome == "ack":
        await react(trigger_message, "👌")
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
    await run_parliament(space, user, plan, update.effective_message)


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
    await run_parliament(space, query.from_user, plan, query.message)

# ============================================================
# MESSAGES THE PERSONAS SEND ON THEIR OWN
# ============================================================

_due_lock = asyncio.Lock()


async def deliver(reminder):
    user_id, persona = reminder["user_id"], reminder["persona"]
    space = await db.call(spaces.home, user_id, persona) if user_id == ALLOWED_USER_ID else None
    if space is None and user_id == ALLOWED_USER_ID and voices.own_bot(persona):
        space = Space(user_id, None, persona)   # no topic for him: his own bot's private chat
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
        await post(space, persona, text)
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
    await bot_app.bot.set_webhook(f"{public_url}/webhook", secret_token=TELEGRAM_SECRET_TOKEN, max_connections=1,
                                  allowed_updates=voices.ALLOWED_UPDATES)
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
    bot_app.add_handler(CallbackQueryHandler(on_voice_button, pattern=r"^voice:"))
    bot_app.add_handler(ChatMemberHandler(on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & MEDIA, handle_other))
    bot_app.add_error_handler(on_error)
    await bot_app.initialize()
    await voices.load(bot_app.bot)
    await bot_app.start()
    try:
        await set_webhook()
    except Exception:
        logger.exception("Setting the webhook failed")
    loops = [asyncio.create_task(reminders_loop()), asyncio.create_task(memory_loop())]
    if public_url:
        loops.append(asyncio.create_task(voices.set_webhooks(voice_url, TELEGRAM_SECRET_TOKEN)))
    yield
    for task in loops:
        task.cancel()
    await bot_app.stop()
    await bot_app.shutdown()
    await voices.close()


fastapi_app = FastAPI(lifespan=lifespan)
_seen_updates = collections.deque(maxlen=1000)
_tick_tasks: set[asyncio.Task] = set()


def _matches(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


def _authorized(request):
    return _matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_SECRET_TOKEN)


async def _receive(request, key, bot):
    data = await request.json()
    seen = (key, data.get("update_id"))   # every bot numbers its updates separately
    if seen in _seen_updates:
        return Response("duplicate")
    _seen_updates.append(seen)
    update = Update.de_json(data, bot)
    if key == voices.MAIN or voices.wanted(update):
        await bot_app.update_queue.put(update)
    return Response("ok")


@fastapi_app.post("/webhook")
async def webhook(request: Request):
    if not _authorized(request):
        return Response("unauthorized", status_code=403)
    return await _receive(request, voices.MAIN, bot_app.bot)


@fastapi_app.post("/webhook/{key}")
async def voice_webhook(key: str, request: Request):
    """Updates for a persona's own bot."""
    if not _authorized(request):
        return Response("unauthorized", status_code=403)
    bot = voices.own_bot(key)
    if bot is None:  # not connected (yet) - Telegram will try again later
        return Response("unknown bot", status_code=503 if key in voices.KEYS else 404)
    return await _receive(request, key, bot)


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
