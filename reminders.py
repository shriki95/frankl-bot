"""Messages Frankl sends on his own: a daily check-in and one-off follow-ups.

Rows in the reminders table:
- kind 'daily': at most one active per user (status 'pending'); after each run due_at moves to the next day.
- kind 'once': status pending -> sending -> sent / skipped / missed / failed, or cancelled.
"""
import datetime
import os
import re

import clock
import db

DEFAULT_DAILY_TIME = os.environ.get("DEFAULT_DAILY_TIME", "08:00")
DAILY_GRACE = datetime.timedelta(hours=2)     # a daily message later than this is skipped, not sent late
ONCE_GRACE = datetime.timedelta(hours=12)
ACTIVE_WINDOW = datetime.timedelta(minutes=20)  # he wrote this recently - don't interrupt with a scheduled message
POSTPONE = datetime.timedelta(minutes=30)
MAX_UNANSWERED = 2                            # unanswered proactive messages before Frankl backs off
MAX_PENDING_FOLLOWUPS = 3
MAX_AHEAD = datetime.timedelta(days=60)
QUIET_START, QUIET_END = datetime.time(22, 30), datetime.time(7, 30)


class Rejected(Exception):
    """A tool request that breaks a rule. The message goes back to the model."""


def _normalize_time(text):
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        raise Rejected("שעה לא תקינה - צריך HH:MM (למשל 08:30), או off לביטול.")
    return f"{int(found[1]):02d}:{found[2]}"


def _quiet(due):
    t = clock.local(due).time()
    return t >= QUIET_START or t < QUIET_END


# ============================================================
# SETTINGS
# ============================================================

def seed_defaults(user_id):
    """Give a user who never had proactive settings the default daily message."""
    db.execute("""
        INSERT INTO reminders (user_id, kind, due_at, daily_time)
        SELECT u.user_id, 'daily', %s, %s FROM users u
        WHERE u.user_id = %s AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.user_id = u.user_id)
    """, (clock.next_daily(DEFAULT_DAILY_TIME, clock.now()), DEFAULT_DAILY_TIME, user_id))


def settings(cur, user_id):
    cur.execute("SELECT followups_enabled, checkin_asked FROM users WHERE user_id = %s", (user_id,))
    found = db.rows(cur)
    user = found[0] if found else {"followups_enabled": True, "checkin_asked": False}
    cur.execute("""
        SELECT id, kind, due_at, daily_time, purpose, requested_by FROM reminders
        WHERE user_id = %s AND status = 'pending' ORDER BY due_at
    """, (user_id,))
    pending = db.rows(cur)
    daily = next((r for r in pending if r["kind"] == "daily"), None)
    return {
        "daily_time": daily["daily_time"] if daily else None,
        "followups": user["followups_enabled"],
        "asked": user["checkin_asked"],
        "scheduled": [r for r in pending if r["kind"] == "once"],
    }


def get_settings(user_id):
    return db.run(lambda cur: settings(cur, user_id))


# ============================================================
# TOOLS (called by Frankl during a conversation)
# ============================================================

def set_preferences(user_id, daily_time, followups):
    off = daily_time.strip().lower() in ("off", "none", "")
    if not off:
        daily_time = _normalize_time(daily_time)

    def work(cur):
        cur.execute("UPDATE reminders SET status = 'cancelled' "
                    "WHERE user_id = %s AND kind = 'daily' AND status = 'pending'", (user_id,))
        if not off:
            cur.execute("INSERT INTO reminders (user_id, kind, due_at, daily_time) VALUES (%s, 'daily', %s, %s)",
                        (user_id, clock.next_daily(daily_time, clock.now()), daily_time))
        if not followups:
            cur.execute("UPDATE reminders SET status = 'cancelled' WHERE user_id = %s AND kind = 'once' "
                        "AND status = 'pending' AND requested_by = 'frankl'", (user_id,))
        cur.execute("UPDATE users SET followups_enabled = %s, checkin_asked = TRUE WHERE user_id = %s",
                    (followups, user_id))
    db.run(work)
    daily = "בוטלה" if off else f"כל יום ב-{daily_time}"
    return f"עודכן. הודעה יומית: {daily}. פניות מזדמנות: {'מופעלות' if followups else 'כבויות'}."


def schedule(user_id, when, purpose, requested_by_user):
    try:
        due = clock.parse_local(when)
    except ValueError:
        raise Rejected("פורמט זמן לא תקין - צריך YYYY-MM-DD HH:MM בשעון ישראל.") from None
    now = clock.now()
    if due <= now + datetime.timedelta(minutes=1):
        raise Rejected(f"הזמן הזה כבר עבר. עכשיו {now:%Y-%m-%d %H:%M} - בחר זמן עתידי.")
    if due > now + MAX_AHEAD:
        raise Rejected("אפשר לתזמן עד 60 יום קדימה.")
    purpose = purpose.strip()[:500]
    if not purpose:
        raise Rejected("חסרה מטרה - על מה לכתוב לו?")

    def work(cur):
        if not requested_by_user:
            cur.execute("SELECT followups_enabled FROM users WHERE user_id = %s", (user_id,))
            if not cur.fetchone()[0]:
                raise Rejected("הוא ביקש בלי פניות מזדמנות - אל תתזמן פנייה ביוזמתך.")
            if _quiet(due):
                raise Rejected("זו שעת לילה (22:30-07:30). בחר שעה אחרת - אלא אם הוא ביקש את השעה הזו במפורש.")
            cur.execute("SELECT COUNT(*) FROM reminders WHERE user_id = %s AND kind = 'once' "
                        "AND status = 'pending' AND requested_by = 'frankl'", (user_id,))
            if cur.fetchone()[0] >= MAX_PENDING_FOLLOWUPS:
                raise Rejected("כבר יש 3 פניות מתוכננות ביוזמתך. בטל אחת או ותר על זו.")
        cur.execute("""
            INSERT INTO reminders (user_id, kind, due_at, purpose, requested_by)
            VALUES (%s, 'once', %s, %s, %s) RETURNING id
        """, (user_id, due, purpose, "user" if requested_by_user else "frankl"))
        return cur.fetchone()[0]
    reminder_id = db.run(work)
    return f"נקבע (#{reminder_id}) ל{clock.short(due)}."


def cancel(user_id, reminder_id):
    def work(cur):
        cur.execute("""
            UPDATE reminders SET status = 'cancelled'
            WHERE id = %s AND user_id = %s AND kind = 'once' AND status = 'pending' RETURNING id
        """, (reminder_id, user_id))
        return cur.fetchone()
    if not db.run(work):
        raise Rejected("לא מצאתי פנייה מתוכננת עם המספר הזה.")
    return f"פנייה #{reminder_id} בוטלה."


# ============================================================
# DELIVERY
# ============================================================

def claim_due():
    """Lock the reminders that are due and advance them, so no other run sends them again."""
    def work(cur):
        cur.execute("""
            SELECT r.id, r.user_id, r.kind, r.due_at, r.daily_time, r.purpose, r.requested_by, u.first_name
            FROM reminders r JOIN users u ON u.user_id = r.user_id
            WHERE r.status = 'pending' AND r.due_at <= NOW()
            ORDER BY r.due_at
            FOR UPDATE OF r SKIP LOCKED
        """)
        due = db.rows(cur)
        now = clock.now()
        for r in due:
            if r["kind"] == "daily":
                cur.execute("UPDATE reminders SET due_at = %s WHERE id = %s",
                            (clock.next_daily(r["daily_time"], now), r["id"]))
            else:
                cur.execute("UPDATE reminders SET status = 'sending' WHERE id = %s", (r["id"],))
        return due
    return db.run(work)


def activity(user_id):
    """(seconds since his last message or None, proactive messages he hasn't answered)."""
    recent = db.fetchall("""
        SELECT role, kind, EXTRACT(EPOCH FROM NOW() - created_at::timestamptz)::float AS age
        FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10
    """, (user_id,))
    last_user_age = next((m["age"] for m in recent if m["role"] == "user"), None)
    unanswered = 0
    for m in recent:
        if m["role"] == "user":
            break
        unanswered += m["kind"] == "proactive"
    return last_user_age, unanswered


def decide(reminder, now, last_user_age, unanswered):
    """'send', 'skip', 'postpone' or 'missed'."""
    late = now - reminder["due_at"]
    talking = last_user_age is not None and last_user_age < ACTIVE_WINDOW.total_seconds()
    if reminder["kind"] == "daily":
        if late > DAILY_GRACE or talking or unanswered >= MAX_UNANSWERED:
            return "skip"
        return "send"
    if late > ONCE_GRACE:
        return "missed"
    if reminder["requested_by"] == "user":
        return "send"
    if talking:
        return "postpone"
    if unanswered >= MAX_UNANSWERED:
        return "skip"
    return "send"


def due_soon(within):
    """Whether a scheduled message is due within `within` (or already due)."""
    row = db.fetchone("SELECT EXISTS (SELECT 1 FROM reminders WHERE status = 'pending' "
                      "AND due_at <= NOW() + %s) AS soon", (within,))
    return row["soon"]


def finish(reminder_id, status):
    db.execute("""
        UPDATE reminders SET status = %s, sent_at = CASE WHEN %s = 'sent' THEN NOW() END
        WHERE id = %s AND kind = 'once'
    """, (status, status, reminder_id))


def postpone(reminder_id, due):
    db.execute("UPDATE reminders SET status = 'pending', due_at = %s WHERE id = %s", (due, reminder_id))


def recover():
    """Reminders caught mid-send by a restart are marked failed rather than risk a double send."""
    db.execute("UPDATE reminders SET status = 'failed' WHERE status = 'sending'")
