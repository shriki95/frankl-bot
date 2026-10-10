"""Real time in Israel, phrased in Hebrew for the model and for the user."""
import datetime
from zoneinfo import ZoneInfo

from pyluach import dates as hebrew_dates

TZ = ZoneInfo("Asia/Jerusalem")
DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
          "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def now() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def local(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(TZ)


def describe_now(t: datetime.datetime | None = None) -> str:
    """'יום שישי, 10 באוקטובר 2026 (2026-10-10), 14:32, שעון ישראל. תאריך עברי: ...'"""
    t = local(t or now())
    text = (f"יום {DAYS[t.weekday()]}, {t.day} ב{MONTHS[t.month - 1]} {t.year} "
            f"({t:%Y-%m-%d}), {t:%H:%M}, שעון ישראל.")
    hebrew = hebrew_dates.HebrewDate.from_pydate(t.date())
    text += f" תאריך עברי: {hebrew.hebrew_date_string()}."
    holiday = hebrew.holiday(israel=True, hebrew=True)
    if holiday:
        text += f" היום: {holiday}."
    return text


def short(dt: datetime.datetime) -> str:
    """'יום חמישי 15/10 בשעה 18:00'"""
    t = local(dt)
    return f"יום {DAYS[t.weekday()]} {t:%d/%m} בשעה {t:%H:%M}"


def stamp(dt: datetime.datetime) -> str:
    """'15/10/26 18:00' - for transcripts the memory model reads."""
    return f"{local(dt):%d/%m/%y %H:%M}"


def ago(seconds: float) -> str:
    """Elapsed time in natural Hebrew: 'לפני שעתיים', 'לפני 3 ימים'."""
    minutes = int(seconds // 60)
    hours = minutes // 60
    days = hours // 24
    if minutes < 2:
        return "ממש עכשיו"
    if hours < 1:
        return f"לפני {minutes} דקות"
    if days < 1:
        return {1: "לפני שעה", 2: "לפני שעתיים"}.get(hours, f"לפני {hours} שעות")
    if days < 7:
        return {1: "אתמול", 2: "לפני יומיים"}.get(days, f"לפני {days} ימים")
    if days < 30:
        weeks = days // 7
        return {1: "לפני שבוע", 2: "לפני שבועיים"}.get(weeks, f"לפני {weeks} שבועות")
    months = days // 30
    return {1: "לפני חודש", 2: "לפני חודשיים"}.get(months, f"לפני {months} חודשים")


def parse_local(text: str) -> datetime.datetime:
    """'2026-10-15 18:00' (Israel time) -> aware datetime. Raises ValueError on bad input."""
    naive = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TZ)


def next_daily(hhmm: str, after: datetime.datetime) -> datetime.datetime:
    """The first occurrence of HH:MM (Israel time) strictly after `after`."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    day = local(after).date()
    candidate = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=TZ)
    if candidate <= after:
        candidate = datetime.datetime.combine(day + datetime.timedelta(days=1),
                                              datetime.time(hour, minute), tzinfo=TZ)
    return candidate
