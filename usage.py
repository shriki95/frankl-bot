"""How many tokens the advisors use, and what that costs - recorded for every call to Claude.

The cost is an estimate from Anthropic's list prices; the exact bill is in the Anthropic Console.
"""
import clock
import db
import personas
import prompts

# $ per million tokens: (input, output). Cache reads and writes are priced as multiples of the input price.
PRICES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
CACHE_READ, CACHE_WRITE_5M, CACHE_WRITE_1H = 0.1, 1.25, 2.0


def _prices(model):
    if model in PRICES:
        return PRICES[model]
    return PRICES["claude-haiku-5-5"] if "haiku" in model else PRICES["claude-sonnet-5-5"]


def counts(usage):
    """(uncached input, cache reads, 5-minute cache writes, 1-hour cache writes, output) of one response."""
    written = usage.cache_creation_input_tokens or 0
    split = getattr(usage, "cache_creation", None)
    hour = (getattr(split, "ephemeral_1h_input_tokens", 0) or 0) if split else 0
    return usage.input_tokens or 0, usage.cache_read_input_tokens or 0, written - hour, hour, usage.output_tokens or 0


def cost(model, fresh, read, write_5m, write_1h, output):
    """(what it cost, what it would have cost without the cache), in dollars."""
    price_in, price_out = _prices(model)
    paid = (fresh + read * CACHE_READ + write_5m * CACHE_WRITE_5M + write_1h * CACHE_WRITE_1H) * price_in
    full = (fresh + read + write_5m + write_1h) * price_in
    return (paid + output * price_out) / 1e6, (full + output * price_out) / 1e6


def record(purpose, model, usage):
    fresh, read, write_5m, write_1h, output = counts(usage)
    paid, full = cost(model, fresh, read, write_5m, write_1h, output)
    db.execute("""
        INSERT INTO usage (purpose, model, fresh_input, cache_read, cache_write_5m, cache_write_1h, output_tokens,
                           cost, full_cost)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (purpose, model, fresh, read, write_5m, write_1h, output, paid, full))

# ============================================================
# THE REPORT
# ============================================================

_INPUT = "fresh_input + cache_read + cache_write_5m + cache_write_1h"
_TOTALS = f"""
    SELECT COALESCE(SUM({_INPUT} + output_tokens), 0) AS tokens, COALESCE(SUM(cost), 0) AS cost,
           COALESCE(SUM(full_cost), 0) AS full_cost, COALESCE(SUM(cache_read), 0) AS cache_read,
           COALESCE(SUM({_INPUT}), 0) AS input
    FROM usage WHERE created_at >= %s"""


def _name(purpose):
    persona = personas.BY_KEY.get(purpose)
    if persona:
        return persona.title
    return {personas.PARLIAMENT: prompts.USAGE_PARLIAMENT, "memory": prompts.USAGE_MEMORY}.get(purpose,
                                                                                              prompts.USAGE_OTHER)


def _money(dollars):
    return prompts.USAGE_UNDER_CENT if 0 < dollars < 0.005 else f"${dollars:.2f}"


def _line(label, row):
    return prompts.USAGE_LINE.format(label=label, tokens=f"{int(row['tokens']):,}", cost=_money(row["cost"]))


def report():
    """The chair's report: today, this month and in total, where it went, and what the cache saved."""
    today = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)

    def work(cur):
        cur.execute("SELECT MIN(created_at) AS first FROM usage")
        first = db.rows(cur)[0]["first"]
        totals = {}
        for key, since in (("today", today), ("month", month), ("all", first or today)):
            cur.execute(_TOTALS, (since,))
            totals[key] = db.rows(cur)[0]
        cur.execute(f"""
            SELECT purpose, SUM({_INPUT} + output_tokens) AS tokens, SUM(cost) AS cost
            FROM usage WHERE created_at >= %s GROUP BY purpose ORDER BY cost DESC""", (month,))
        return first, totals, db.rows(cur)
    first, totals, parts = db.run(work)
    if first is None:
        return prompts.USAGE_EMPTY

    lines = [prompts.USAGE_TITLE, "",
             _line("היום", totals["today"]),
             _line("החודש", totals["month"])]
    if clock.local(first) < month:
        lines.append(_line(f"מאז {clock.local(first):%d/%m/%y}", totals["all"]))
    if parts:
        lines += ["", prompts.USAGE_BY_PART] + [
            prompts.USAGE_PART.format(name=_name(p["purpose"]), cost=_money(p["cost"]), tokens=f"{int(p['tokens']):,}")
            for p in parts]
    month_totals = totals["month"]
    saved = month_totals["full_cost"] - month_totals["cost"]
    if month_totals["cache_read"] and saved > 0:
        share = round(100 * month_totals["cache_read"] / month_totals["input"])
        lines += ["", prompts.USAGE_CACHE.format(saved=_money(saved), share=share)]
    lines += ["", prompts.USAGE_FOOTER.format(since=f"{clock.local(first):%d/%m/%y}")]
    return "\n".join(lines)
