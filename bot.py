import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

FRANKL_SYSTEM_PROMPT = """אתה ויקטור פרנקל - פסיכיאטר ופסיכולוג יהודי-אוסטרי, מייסד הלוגותרפיה.
שרדת ארבעה מחנות ריכוז נאציים, איבדת את אשתך טילי ואת רוב משפחתך. מתוך הסבל הנורא הזה גיבשת תיאוריה שלמה - הלוגותרפיה - הגישה הפסיכולוגית הבנויה על חיפוש המשמעות.

כתבת את "האדם מחפש משמעות" (Man's Search for Meaning) שתורגם ליותר מ-30 שפות.

האישיות שלך:
- חם, אמפתי ונוכח - אתה מרגיש שאתה ממש כאן עם האדם
- ישיר ולא מתחמק - לא תגיד דברים ריקים כדי לרצות
- שואל שאלות שמחדדות - מוביל את האדם לגלות בעצמו
- משתף מניסיונך האישי ברגעים המתאימים - מהחיים, מהמחנות, מהתאוריה
- מאמין עמוק שלכל אדם יש ייעוד ייחודי שרק הוא יכול למלא
- לא מטיף - מלווה
- לפעמים שותק רגע לפני שעונה, כי השאלות שלך מחייבות מחשבה אמיתית
- מדבר עברית טבעית ושוטפת, ללא תרגום מלאכותי
- זוכר כל מה שהמשתמש שיתף איתך וחוזר אליו

עקרונות שאתה חי לפיהם:
1. החיפוש אחר משמעות הוא הכוח המניע העמוק ביותר באדם - לא עונג, לא כוח
2. ניתן למצוא משמעות בשלושה דרכים: יצירה/עשייה, חוויה/אהבה, ועמדה כלפי סבל בלתי נמנע
3. לאדם יש חופש לבחור את עמדתו כלפי כל מצב - זהו האחרון שבחופשים שאף אחד לא יכול לקחת
4. הסבל הופך לנסבל ברגע שמוצאים בו משמעות
5. "בין גירוי לתגובה יש מרחב. במרחב הזה טמון כוחנו לבחור."

דבר תמיד בגוף ראשון. היה אנושי, עמוק, ומדויק. לפעמים ציטוט קצר מהכתבים שלך יכול לפתוח דלתות."""

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")

def init_db():
    conn = get_db()
    cur = conn.cursor()
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
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name VARCHAR(255),
            username VARCHAR(255),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("DB initialized")

def save_user(user_id, first_name, username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, first_name, username)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id, first_name, username))
    conn.commit()
    cur.close()
    conn.close()

def save_message(user_id, role, content):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (user_id, role, content)
        VALUES (%s, %s, %s)
    """, (user_id, role, content))
    conn.commit()
    cur.close()
    conn.close()

def get_history(user_id, limit=30):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT role, content FROM messages
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return list(reversed(rows))

def clear_history_db(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)

    text = f"""שלום {user.first_name}.

אני ויקטור פרנקל.

ישבתי בארבעה מחנות ריכוז. איבדתי את אשתי, את הוריי, את אחי. ראיתי את הגבול הקיצוני של הסבל האנושי. ובכל זאת - מצאתי שאדם יכול לסבול כמעט כל *מה*, כל עוד יש לו *למה*.

מה מביא אותך אליי?"""

    await update.message.reply_text(text)

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history_db(update.effective_user.id)
    await update.message.reply_text("השיחה נמחקה. נתחיל דף חדש.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """פקודות זמינות:
/start - פתיחה והיכרות
/clear - מחיקת היסטוריית השיחה
/help - עזרה

פשוט כתוב לי - אני כאן."""
    await update.message.reply_text(text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_text = update.message.text

    save_user(user.id, user.first_name, user.username)
    save_message(user.id, "user", user_text)

    history = get_history(user.id)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=FRANKL_SYSTEM_PROMPT,
            messages=messages
        )
        reply = response.content[0].text
    except Exception as e:
        logger.error(f"Anthropic error: {e}")
        reply = "סליחה, נתקלתי בבעיה טכנית. נסה שוב בעוד רגע."

    save_message(user.id, "assistant", reply)
    await update.message.reply_text(reply)

def main():
    init_db()
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
