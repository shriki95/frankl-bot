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
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import brain
import clock
import db
import memory
import prompts
import reminders

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


async def send_text(bot, chat_id, text, parse_mode=None):
    for chunk in split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)


@contextlib.asynccontextmanager
async def typing(bot, chat_id):
    """Show 'typing...' until the block ends (Telegram clears it after ~5 seconds)."""
    async def keep_typing():
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4.5)
    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


def memory_html(text):
    """The memory document for Telegram: section titles in bold."""
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            lines.append(f"\n<b>{html.escape(line[3:].strip())}</b>")
        else:
            lines.append(html.escape(line))
    return "\n".join(lines).strip()


async def get_memory(user_id):
    row = await db.call(db.fetchone, "SELECT memory FROM users WHERE user_id = %s", (user_id,))
    return row["memory"] if row else ""


def has_content(section_text):
    return bool(section_text) and section_text != "אין עדיין"

# ============================================================
# BOT HANDLERS
# ============================================================


async def is_allowed(update: Update) -> bool:
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER_ID:
        if update.effective_message:
            await update.effective_message.reply_text(prompts.PRIVATE_BOT)
        return False
    return True


async def register(user):
    await db.call(memory.save_user, user.id, user.first_name, user.username)
    await db.call(reminders.seed_defaults, user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await register(update.effective_user)
    await update.effective_message.reply_text(prompts.OPENING_MESSAGE)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    text = await get_memory(update.effective_user.id)
    if not text:
        await update.effective_message.reply_text("עדיין בתחילת הדרך. ככל שנדבר יותר - אדע אותך יותר.")
        return
    await send_text(context.bot, update.effective_chat.id,
                    "<b>מה שאני יודע עליך:</b>\n" + memory_html(text), ParseMode.HTML)


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    goals = memory.section(await get_memory(update.effective_user.id), "יעדים ושאיפות")
    if not has_content(goals):
        await update.effective_message.reply_text("עדיין לא תיעדנו יעדים יחד. ספר לי - מה אתה שואף אליו?")
        return
    await send_text(context.bot, update.effective_chat.id,
                    "<b>היעדים שלך:</b>\n\n" + html.escape(goals), ParseMode.HTML)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    text = await get_memory(update.effective_user.id)
    parts = [(title, memory.section(text, title)) for title in ("חוטים פתוחים", "יומן השיחות")]
    parts = [(title, body) for title, body in parts if has_content(body)]
    if not parts:
        await update.effective_message.reply_text("עדיין לא תיעדנו אירועים חשובים. ספר לי מה קורה בחייך.")
        return
    await send_text(context.bot, update.effective_chat.id, "\n\n".join(
        f"<b>{title}:</b>\n{html.escape(body)}" for title, body in parts), ParseMode.HTML)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    settings = await db.call(reminders.get_settings, update.effective_user.id)
    lines = ["<b>מתי אני כותב לך ביוזמתי:</b>",
             f"• הודעה יומית: כל יום ב-{settings['daily_time']}" if settings["daily_time"] else "• הודעה יומית: כבויה",
             f"• פניות מזדמנות לפי מה שעולה בשיחה: {'מופעלות' if settings['followups'] else 'כבויות'}"]
    if settings["scheduled"]:
        lines += ["", "<b>מתוכנן:</b>"] + [
            f"• {clock.short(r['due_at'])} - {html.escape(r['purpose'])}" for r in settings["scheduled"]]
    lines += ["", 'רוצה לשנות? פשוט תגיד לי - למשל "תכתוב לי כל ערב ב-21:00" או "בלי הודעות יומיות".']
    await send_text(context.bot, update.effective_chat.id, "\n".join(lines), ParseMode.HTML)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    user = update.effective_user
    await update.effective_message.reply_text("רגע, אני מסכם את הדרך שעברנו...")
    async with typing(context.bot, update.effective_chat.id), brain.conversation_lock(user.id):
        try:
            text = await brain.summarize(user.id, user.first_name)
        except Exception:
            logger.exception("Summary error")
            text = "סליחה, לא הצלחתי לייצר סיכום כרגע."
    await send_text(context.bot, update.effective_chat.id, text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    user_id = update.effective_user.id
    async with typing(context.bot, update.effective_chat.id), brain.conversation_lock(user_id):
        cleared = await memory.clear_history(user_id)
    if cleared:
        await update.effective_message.reply_text("היסטוריית השיחה נמחקה. הזיכרון העמוק נשמר.")
    else:
        await update.effective_message.reply_text(
            "לא הצלחתי לשמור את הזיכרון כרגע, אז לא מחקתי כלום. נסה שוב עוד מעט.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await update.effective_message.reply_text(prompts.HELP_TEXT)


_arrival_locks: dict[int, asyncio.Lock] = {}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    user = update.effective_user
    chat_id = update.effective_chat.id
    # Updates are handled concurrently; this first wait keeps his messages in the order they arrived.
    async with _arrival_locks.setdefault(user.id, asyncio.Lock()):
        await register(user)
        message_id = await db.call(memory.save_message, user.id, "user", update.effective_message.text)
    async with typing(context.bot, chat_id):
        await asyncio.sleep(DEBOUNCE_SECONDS)
        async with brain.conversation_lock(user.id):
            if await db.call(memory.latest_user_message_id, user.id) != message_id:
                return  # a newer message arrived - its handler answers them all together
            try:
                reply = await brain.reply(user.id, user.first_name)
            except Exception:
                logger.exception("Reply error")
                reply = prompts.ERROR_TEXT
            if reply not in (prompts.ERROR_TEXT, prompts.REFUSAL_TEXT):
                await db.call(memory.save_message, user.id, "assistant", reply)
            await send_text(context.bot, chat_id, reply)
    memory.consolidate_soon(user.id)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    await update.effective_message.reply_text(prompts.TEXT_ONLY)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error", exc_info=context.error)

# ============================================================
# MESSAGES FRANKL SENDS ON HIS OWN
# ============================================================

_due_lock = asyncio.Lock()


async def deliver(reminder):
    user_id = reminder["user_id"]
    if user_id != ALLOWED_USER_ID:
        await db.call(reminders.finish, reminder["id"], "cancelled")
        return
    last_user_age, unanswered = await db.call(reminders.activity, user_id)
    action = reminders.decide(reminder, clock.now(), last_user_age, unanswered)
    if action == "postpone":
        await db.call(reminders.postpone, reminder["id"], clock.now() + reminders.POSTPONE)
        return
    if action != "send":
        logger.info(f"Reminder {reminder['id']} ({reminder['kind']}): {action}")
        await db.call(reminders.finish, reminder["id"], "skipped" if action == "skip" else action)
        return
    async with brain.conversation_lock(user_id):
        text = await brain.compose_proactive(user_id, reminder["first_name"], reminder)
        if not text:
            raise RuntimeError("empty proactive message")
        await send_text(bot_app.bot, user_id, text)
        await db.call(memory.save_message, user_id, "assistant", text, "proactive")
    await db.call(reminders.finish, reminder["id"], "sent")
    logger.info(f"Reminder {reminder['id']} ({reminder['kind']}) sent")


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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    await db.call(db.init_db)
    await db.call(reminders.recover)
    await db.call(reminders.seed_defaults, ALLOWED_USER_ID)
    bot_app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).updater(None).concurrent_updates(True).build()
    for name, handler in (("start", cmd_start), ("profile", cmd_profile), ("goals", cmd_goals),
                          ("events", cmd_events), ("reminders", cmd_reminders), ("summary", cmd_summary),
                          ("clear", cmd_clear), ("help", cmd_help)):
        bot_app.add_handler(CommandHandler(name, handler))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, handle_other))
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
