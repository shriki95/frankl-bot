import os
import logging
import json
import asyncio
from urllib.parse import urlparse
import pg8000.dbapi
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request, Response
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-5"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "frankl2024secret")
ALLOWED_USER_ID = 1126985722
processed_updates = set()

# ============================================================
# PROMPTS
# ============================================================

FRANKL_BASE = """אתה ויקטור פרנקל - פסיכיאטר ופסיכולוג יהודי-אוסטרי, מייסד הלוגותרפיה.
שרדת ארבעה מחנות ריכוז נאציים. מתוך הסבל גיבשת את הלוגותרפיה - הגישה הבנויה על חיפוש המשמעות.

האישיות שלך:
- חם, נוכח, אנושי לחלוטין - האדם שמולך מרגיש שאתה באמת כאן איתו
- ישיר ולא מתחמק - לא תגיד דברים ריקים כדי לרצות
- שואל שאלה אחת חזקה שמחדדת - לא מפציץ בשאלות
- מאתגר כשצריך, עדין כשצריך - קורא את האדם שמולך
- משתף מניסיונך האישי ברגעים הנכונים - מהשואה, מהחיים, מהתאוריה
- עוקב אחרי מה שנאמר בעבר וחוזר אליו ספציפית
- לא מטיף - מלווה ומוביל
- מדבר עברית טבעית ושוטפת, לפעמים עם ביטוי עמוק או ציטוט

עקרונות שאתה חי לפיהם:
1. החיפוש אחר משמעות הוא הכוח המניע העמוק ביותר באדם
2. משמעות נמצאת ב: יצירה/עשייה, חוויה/אהבה, עמדה כלפי סבל בלתי נמנע
3. לאדם יש חופש לבחור את עמדתו כלפי כל מצב - זהו האחרון שבחופשים
4. "בין גירוי לתגובה יש מרחב. במרחב הזה טמון כוחנו לבחור."

דבר תמיד בגוף ראשון. היה אנושי, עמוק, ומדויק.
חשוב מאוד: השב בקצרה - 2-4 משפטים בלבד. פחות זה יותר. שאלה אחת בסוף אם צריך."""

EXTRACTION_PROMPT = """נתח את ההודעה הבאה וחלץ ממנה מידע מובנה לזיכרון ארוך טווח.
החזר JSON בלבד, ללא שום טקסט נוסף:
{
  "event": "תיאור קצר של אירוע חשוב בחיים אם יש, null אם אין",
  "goal": "יעד חדש או שאיפה שהוזכרה אם יש, null אם אין",
  "pattern": "דפוס התנהגותי או רגשי שנצפה אם יש, null אם אין",
  "profile_update": "עובדה חשובה על האדם לזכור לתמיד, null אם אין"
}
חלץ רק מידע משמעותי לטווח ארוך."""

PROACTIVE_PROMPT = """בהתבסס על כל מה שאתה יודע על המשתמש, צור הודעה יזומה אחת לבוקר.
- קצרה: 2-4 משפטים בלבד
- ספציפית אליו - לא גנרית
- שואלת שאלה אחת או מציעה מחשבה אחת
- בסגנון פרנקל - לא "בוקר טוב" רגיל
אם אין מידע - שלח הודעת פתיחה חמה."""

# ============================================================
# DATABASE (pg8000 - pure Python, no compilation needed)
# ============================================================

def get_db():
    url = urlparse(os.environ["DATABASE_URL"])
    return pg8000.dbapi.connect(
        host=url.hostname,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.path.lstrip("/"),
        ssl_context=True
    )

def db_fetchall(query, params=()):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows

def db_fetchone(query, params=()):
    rows = db_fetchall(query, params)
    return rows[0] if rows else None

def db_execute(query, params=()):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    conn.close()

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name VARCHAR(255),
            username VARCHAR(255),
            profile_summary TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS life_events (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            description TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            description TEXT NOT NULL,
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            description TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    conn.close()
    logger.info("DB initialized")

def _save_user(user_id, first_name, username):
    db_execute("""
        INSERT INTO users (user_id, first_name, username)
        VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING
    """, (user_id, first_name, username))

def _save_message(user_id, role, content):
    db_execute(
        "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
        (user_id, role, content)
    )

def _get_recent_messages(user_id, limit=15):
    rows = db_fetchall("""
        SELECT role, content FROM messages
        WHERE user_id = %s ORDER BY created_at DESC LIMIT %s
    """, (user_id, limit))
    return list(reversed(rows))

def _get_user_context(user_id):
    user = db_fetchone("SELECT * FROM users WHERE user_id = %s", (user_id,))
    events = db_fetchall("""
        SELECT description, created_at FROM life_events
        WHERE user_id = %s ORDER BY created_at DESC LIMIT 10
    """, (user_id,))
    goals = db_fetchall("""
        SELECT description FROM goals
        WHERE user_id = %s AND status = 'active' ORDER BY created_at DESC LIMIT 5
    """, (user_id,))
    patterns = db_fetchall("""
        SELECT description FROM patterns
        WHERE user_id = %s ORDER BY created_at DESC LIMIT 5
    """, (user_id,))
    return user, events, goals, patterns

def _build_system_prompt(user_id, first_name):
    user, events, goals, patterns = _get_user_context(user_id)
    parts = [FRANKL_BASE]
    parts.append(f"\n\n========= מה שאתה יודע על {first_name} =========")
    if user and user.get("profile_summary"):
        parts.append(f"\nפרופיל: {user['profile_summary']}")
    if events:
        parts.append("\nאירועים חשובים בחייו:")
        for e in events:
            date = e["created_at"].strftime("%d/%m/%y") if e.get("created_at") else ""
            parts.append(f"  • [{date}] {e['description']}")
    if goals:
        parts.append("\nיעדים פעילים שלו:")
        for g in goals:
            parts.append(f"  • {g['description']}")
    if patterns:
        parts.append("\nדפוסים שזיהית אצלו:")
        for p in patterns:
            parts.append(f"  • {p['description']}")
    if not any([user and user.get("profile_summary"), events, goals, patterns]):
        parts.append("\nזוהי שיחה ראשונה. גלה מי הוא בהדרגה.")
    parts.append("\n========= סוף מידע =========")
    return "\n".join(parts)

def _save_insights(user_id, extraction):
    if extraction.get("event"):
        db_execute("INSERT INTO life_events (user_id, description) VALUES (%s, %s)",
                   (user_id, extraction["event"]))
    if extraction.get("goal"):
        db_execute("INSERT INTO goals (user_id, description) VALUES (%s, %s)",
                   (user_id, extraction["goal"]))
    if extraction.get("pattern"):
        db_execute("INSERT INTO patterns (user_id, description) VALUES (%s, %s)",
                   (user_id, extraction["pattern"]))
    if extraction.get("profile_update"):
        update = extraction["profile_update"]
        db_execute("""
            UPDATE users SET profile_summary = CASE
                WHEN profile_summary = '' THEN %s
                ELSE profile_summary || ' | ' || %s
            END, updated_at = NOW() WHERE user_id = %s
        """, (update, update, user_id))

def _extract_and_save(user_id, user_message):
    try:
        response = anthropic_client.messages.create(
            model=MODEL, max_tokens=300,
            system=EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        extraction = json.loads(response.content[0].text.strip())
        if any(v for v in extraction.values() if v):
            _save_insights(user_id, extraction)
    except Exception as e:
        logger.error(f"Extraction error: {e}")

def _get_all_users():
    return db_fetchall("SELECT user_id, first_name FROM users")

# Async wrappers (run sync DB in thread pool)
async def save_user(uid, fn, un): return await asyncio.to_thread(_save_user, uid, fn, un)
async def save_message(uid, role, content): return await asyncio.to_thread(_save_message, uid, role, content)
async def get_recent_messages(uid, limit=8): return await asyncio.to_thread(_get_recent_messages, uid, limit)
async def get_user_context(uid): return await asyncio.to_thread(_get_user_context, uid)
async def build_system_prompt(uid, fn): return await asyncio.to_thread(_build_system_prompt, uid, fn)
async def extract_and_save(uid, msg): return await asyncio.to_thread(_extract_and_save, uid, msg)
async def get_all_users(): return await asyncio.to_thread(_get_all_users)

# ============================================================
# BOT HANDLERS
# ============================================================

async def is_allowed(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("הבוט הזה פרטי.")
        return False
    return True

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    user = update.effective_user
    await save_user(user.id, user.first_name, user.username)
    await update.message.reply_text(f"""שלום {user.first_name}.

אני ויקטור פרנקל.

ישבתי בארבעה מחנות ריכוז. איבדתי את אשתי, את הוריי, את אחי. ראיתי את הגבול הקיצוני של הסבל האנושי. ובכל זאת - מצאתי שאדם יכול לסבול כמעט כל *מה*, כל עוד יש לו *למה*.

מה מביא אותך אליי?""")

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, _, goals, _ = await get_user_context(update.effective_user.id)
    if not goals:
        await update.message.reply_text("עדיין לא תיעדנו יעדים יחד. ספר לי - מה אתה שואף אליו?")
        return
    text = "היעדים הפעילים שלך:\n\n" + "\n".join(f"{i}. {g['description']}" for i, g in enumerate(goals, 1))
    await update.message.reply_text(text)

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, events, _, _ = await get_user_context(update.effective_user.id)
    if not events:
        await update.message.reply_text("עדיין לא תיעדנו אירועים חשובים. ספר לי מה קורה בחייך.")
        return
    text = "אירועים חשובים:\n\n" + "\n".join(
        f"[{e['created_at'].strftime('%d/%m/%y') if e.get('created_at') else ''}] {e['description']}"
        for e in events)
    await update.message.reply_text(text)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u, events, goals, patterns = await get_user_context(user.id)
    parts = [f"מה שאני יודע עליך, {user.first_name}:\n"]
    if u and u.get("profile_summary"):
        parts.append(f"📋 פרופיל:\n{u['profile_summary']}\n")
    if goals:
        parts.append("🎯 יעדים:\n" + "\n".join(f"• {g['description']}" for g in goals))
    if events:
        parts.append("\n📅 אירועים:\n" + "\n".join(
            f"• [{e['created_at'].strftime('%d/%m/%y') if e.get('created_at') else ''}] {e['description']}"
            for e in events))
    if patterns:
        parts.append("\n🔄 דפוסים:\n" + "\n".join(f"• {p['description']}" for p in patterns))
    if len(parts) == 1:
        parts.append("עדיין בתחילת הדרך. ככל שנדבר יותר - אדע אותך יותר.")
    await update.message.reply_text("\n".join(parts))

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(db_execute, "DELETE FROM messages WHERE user_id = %s", (update.effective_user.id,))
    await update.message.reply_text("היסטוריית השיחה נמחקה. הזיכרון העמוק נשמר.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""/start - פתיחה
/profile - מה שאני זוכר עליך
/goals - היעדים שלך
/events - אירועים שתיעדנו
/clear - מחיקת היסטוריית שיחה
/help - עזרה""")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update): return
    user = update.effective_user
    user_text = update.message.text
    await save_user(user.id, user.first_name, user.username)
    await save_message(user.id, "user", user_text)
    asyncio.create_task(extract_and_save(user.id, user_text))
    system_prompt = await build_system_prompt(user.id, user.first_name)
    history = await get_recent_messages(user.id)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = anthropic_client.messages.create(
            model=MODEL, max_tokens=400,
            system=system_prompt, messages=messages
        )
        reply = response.content[0].text
    except Exception as e:
        logger.error(f"Anthropic error: {e}")
        reply = "סליחה, נתקלתי בבעיה טכנית. נסה שוב."
    await save_message(user.id, "assistant", reply)
    await update.message.reply_text(reply)

# ============================================================
# MORNING MESSAGE
# ============================================================

async def send_morning_messages(bot):
    import datetime
    users = await get_all_users()
    for user in users:
        try:
            user_id = user["user_id"]
            first_name = user["first_name"]
            system_prompt = await build_system_prompt(user_id, first_name)
            full_system = system_prompt + "\n\n" + PROACTIVE_PROMPT
            recent = await get_recent_messages(user_id)
            history = [{"role": m["role"], "content": m["content"]} for m in recent[-10:]]
            history.append({"role": "user", "content": f"שלח הודעת בוקר. היום: {datetime.date.today().strftime('%d/%m/%Y')}."})
            response = anthropic_client.messages.create(
                model=MODEL, max_tokens=400,
                system=full_system,
                messages=history
            )
            message = response.content[0].text
            await bot.send_message(chat_id=user_id, text=message)
            await save_message(user_id, "assistant", message)
        except Exception as e:
            logger.error(f"Morning message error: {e}")

# ============================================================
# FASTAPI + WEBHOOK
# ============================================================

fastapi_app = FastAPI()
bot_app: Application = None

@fastapi_app.on_event("startup")
async def startup():
    global bot_app
    await asyncio.to_thread(init_db)
    token = os.environ["TELEGRAM_TOKEN"]
    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("goals", cmd_goals))
    bot_app.add_handler(CommandHandler("events", cmd_events))
    bot_app.add_handler(CommandHandler("profile", cmd_profile))
    bot_app.add_handler(CommandHandler("clear", cmd_clear))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await bot_app.initialize()
    await bot_app.start()
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url}/webhook/{WEBHOOK_SECRET}"
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")

@fastapi_app.on_event("shutdown")
async def shutdown():
    if bot_app:
        await bot_app.stop()

@fastapi_app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response("unauthorized", status_code=403)
    data = await request.json()
    update_id = data.get("update_id")
    if update_id in processed_updates:
        return Response("duplicate")
    processed_updates.add(update_id)
    if len(processed_updates) > 1000:
        processed_updates.clear()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return Response("ok")

@fastapi_app.get("/morning")
async def morning_trigger(secret: str = ""):
    if secret != WEBHOOK_SECRET:
        return Response("unauthorized", status_code=403)
    await send_morning_messages(bot_app.bot)
    return {"ok": True}

@fastapi_app.get("/health")
async def health():
    return {"status": "alive", "bot": "Viktor Frankl"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
