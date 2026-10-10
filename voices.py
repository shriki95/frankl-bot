"""Every persona can speak through a Telegram bot of his own, so his messages carry his own name and photo.

Frankl speaks through the main bot (TELEGRAM_TOKEN). It is the only bot that reads the group, and the
place where the user connects the other bots: he forwards BotFather's message (with the new bot's token)
to it. The tokens are kept in the database. Until a persona has a bot of his own, the main bot posts for
him, signed with his name.
"""
import asyncio
import contextlib
import logging
import re

from telegram import Bot, BotCommandScopeAllPrivateChats, Update
from telegram.request import HTTPXRequest

import db
import personas
import prompts

logger = logging.getLogger(__name__)

MAIN = "frankl"   # the persona the main bot belongs to
CHAIR = "chair"   # the parliament's chair: summaries and announcements in the meetings
KEYS = [p.key for p in personas.ALL if p.key != MAIN] + [CHAIR]
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
COMMANDS = [("profile", "מה שאני זוכר עליך"), ("goals", "היעדים שלך"), ("events", "חוטים פתוחים ויומן השיחות"),
            ("reminders", "מתי אני כותב לך ביוזמתי"), ("summary", "סיכום הדרך שעברנו"),
            ("clear", "דף חדש (הזיכרון נשמר)"), ("usage", "כמה טוקנים נוצלו"), ("help", "עזרה")]
CHAIR_COMMANDS = [("usage", "כמה טוקנים נוצלו ומה זה עלה")]
DESCRIBED = 2   # bump when names, descriptions or commands change - the bots are described again at startup

main: Bot | None = None
_bots: dict[str, Bot] = {}
pending: dict[int, tuple[Bot, str]] = {}   # bot id -> (bot, token), while the user says whose bot it is


def make_bot(token):
    return Bot(token, request=HTTPXRequest(connection_pool_size=8))


def describe_error(error, bot):
    """An error, safe to log: a bot's token never goes into the logs."""
    return repr(error).replace(bot.token, "<token>")


def title(key):
    return f"{prompts.CHAIR_EMOJI} {prompts.CHAIR_NAME}" if key == CHAIR else personas.BY_KEY[key].title


def name(key):
    return prompts.CHAIR_NAME if key == CHAIR else personas.BY_KEY[key].name


def bot_for(key):
    """The bot that speaks for a persona or for the chair."""
    return _bots.get(key) or main


def own_bot(key):
    return _bots.get(key)


def has_own(key):
    return key == MAIN or key in _bots


def key_of(bot):
    """Whose bot this is: MAIN for the main bot, None for a bot we don't know."""
    if main is not None and bot.id == main.id:
        return MAIN
    return next((key for key, b in _bots.items() if b.id == bot.id), None)


def missing():
    return [key for key in KEYS if key not in _bots]


def wanted(update: Update):
    """The persona bots don't read the group (the main bot does) - only their private chats,
    the buttons on their own messages, and being added to a group."""
    if update.callback_query or update.my_chat_member:
        return True
    return update.effective_chat is not None and update.effective_chat.type == "private"

# ============================================================
# WHOSE BOT IS IT
# ============================================================


def _normal(text):
    return (text or "").replace("״", '"').replace("׳", "'").replace("’", "'").replace("`", "'").lower()


def _names(key):
    if key == CHAIR:
        return (prompts.CHAIR_NAME, 'יו"ר', "יושב ראש", "chair", "parliament")
    persona = personas.BY_KEY[key]
    return (persona.name, persona.short, *persona.aliases, *_MISSPELLED.get(key, ()))


_MISSPELLED = {"buffett": ("buffet",), "churchill": ("churchil",)}


def guess(bot):
    """The persona (or the chair) a new bot is named after, if exactly one fits."""
    text = _normal(f"{bot.first_name} {bot.username}")
    found = {key for key in KEYS if any(_normal(alias) in text for alias in _names(key))}
    return found.pop() if len(found) == 1 else None

# ============================================================
# CONNECTING
# ============================================================


def _rows():
    return db.fetchall("SELECT key, token, described FROM voices")


def _described(key):
    db.execute("UPDATE voices SET described = %s WHERE key = %s", (DESCRIBED, key))


def _save(key, bot, token):
    def work(cur):
        cur.execute("DELETE FROM voices WHERE bot_id = %s OR key = %s", (bot.id, key))
        cur.execute("INSERT INTO voices (key, token, bot_id, username, described) VALUES (%s, %s, %s, %s, %s)",
                    (key, token, bot.id, bot.username, DESCRIBED))
    db.run(work)


async def connect(token):
    """A ready bot for a token, or None if Telegram rejects it."""
    bot = make_bot(token)
    try:
        await bot.initialize()
        return bot
    except Exception:
        # Never log this exception: its message contains the token.
        with contextlib.suppress(Exception):
            await bot.shutdown()
        return None


async def load(main_bot):
    """At startup: the main bot, and the persona bots saved in the database.
    Returns the keys of bots whose name, description or commands are out of date."""
    global main
    main = main_bot
    rows = await db.call(_rows)

    async def one(row):
        bot = await connect(row["token"])
        if bot is None:
            logger.error(f"The bot of {row['key']} was rejected by Telegram - the main bot speaks for him")
        else:
            _bots[row["key"]] = bot
    await asyncio.gather(*(one(row) for row in rows))
    logger.info(f"Persona bots: {sorted(_bots) or 'none yet'}")
    return [row["key"] for row in rows if row["described"] < DESCRIBED and row["key"] in _bots]


async def describe_again(keys):
    for key in keys:
        await _describe(key, _bots[key])
        await db.call(_described, key)


async def close():
    for bot in list(_bots.values()):
        with contextlib.suppress(Exception):
            await bot.shutdown()


async def set_webhook(key, bot, url, secret):
    await bot.set_webhook(url, secret_token=secret, max_connections=1, allowed_updates=ALLOWED_UPDATES)


async def set_webhooks(url_for, secret):
    async def one(key, bot):
        try:
            await set_webhook(key, bot, url_for(key), secret)
        except Exception as error:
            logger.error(f"Setting the webhook of {key}'s bot failed: {describe_error(error, bot)}")
    await asyncio.gather(*(one(key, bot) for key, bot in list(_bots.items())))


async def _describe(key, bot):
    """His name, a description for the empty chat, and the commands of a private conversation."""
    if key == CHAIR:
        short = description = prompts.CHAIR_DESCRIPTION
        commands = CHAIR_COMMANDS
    else:
        persona = personas.BY_KEY[key]
        short = persona.domain
        description = f"{persona.title}\n{persona.domain}.\n\nשיחה אישית - רק בינינו."
        commands = COMMANDS
    steps = [bot.set_my_short_description(short), bot.set_my_description(description),
             bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())]
    if bot.first_name != name(key):
        steps.append(bot.set_my_name(name(key)))
    for step in steps:
        try:
            await step
        except Exception as error:
            logger.warning(f"Describing {key}'s bot: {describe_error(error, bot)}")


async def assign(key, bot, token, url, secret):
    """Make a bot the voice of a persona (or of the chair), replacing the one he had."""
    await db.call(_save, key, bot, token)
    for other, old in list(_bots.items()):
        if other == key or old.id == bot.id:
            del _bots[other]
            if old.id != bot.id:
                with contextlib.suppress(Exception):
                    await old.delete_webhook()
            with contextlib.suppress(Exception):
                await old.shutdown()
    _bots[key] = bot
    await set_webhook(key, bot, url, secret)
    await _describe(key, bot)
    logger.info(f"{key} now speaks through @{bot.username}")
