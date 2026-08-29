"""bodystats.py — body-composition trends, compared scan to scan.

The rule this file exists to enforce: COMPARE SCAN TO SCAN, NEVER OVER A FIXED WINDOW.

People weigh in whenever they get round to it — real gaps run 1, 5, 6, 8, 13 days. A fixed
7-day window fails in both directions. On a short gap the comparison silently vanishes, and a
missing change looks exactly like no change. On a long gap it reaches PAST the reading it
should have used and reports a change against something three weeks old, under a heading that
says "your week".

So everything here anchors on the days that carry a metric only a scale screenshot can
produce, and every comparison prints the gap it measured — a change means nothing until you
know whether it took 6 days or 13.
"""
from datetime import date, timedelta

from . import accounts, db, nutrition
from .db import BODY_STATS, HEALTH_KEYS

# Metrics that can ONLY come from a scale screenshot. weight_kg and body_fat are deliberately
# excluded: a phone or band syncs those on its own, so a day carrying only those two is not a
# weigh-in the person chose to do — counting it as one compares a new screenshot against a day
# they never stepped on the scale, and reports a change they didn't make.
SCAN_ONLY_KEYS = tuple(k for k, _, _, _ in BODY_STATS if k not in HEALTH_KEYS)

# The three numbers most goals actually turn on. Everything else is saved but not compared, so
# the reply stays readable on a phone. (key, label, unit, does "held" matter?)
HEADLINE = [("weight_kg", "⚖️ Weight", "kg", False),
            ("body_fat", "\U0001f525 Body fat", "%", False),
            ("muscle_mass_kg", "\U0001f4aa Muscle", "kg", True)]


def scan_days(user: str, limit: int = 2) -> list[str]:
    """The most recent days they actually sent a scale screenshot, newest first."""
    marks = ",".join("?" * len(SCAN_ONLY_KEYS))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT day FROM health_metrics WHERE user=? AND metric IN ({marks}) "
            "ORDER BY day DESC LIMIT ?", (user, *SCAN_ONLY_KEYS, limit)).fetchall()
    return [r[0] for r in rows]


def value_on(user: str, metric: str, day: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM health_metrics WHERE user=? AND day=? AND metric=? "
            "ORDER BY ts DESC LIMIT 1", (user, day, metric)).fetchone()
    return row[0] if row else None


def scan_trend(user: str, metric: str):
    """(latest, change_since_the_scan_before_it, latest_day, gap_days) for one body metric.

    Restricted to scan days on purpose — a weight synced from a phone is not a weigh-in they
    chose, and must never become the baseline their screenshot is judged against.
    """
    days = set(scan_days(user, limit=40))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, value FROM health_metrics WHERE user=? AND metric=? "
            "ORDER BY day DESC, ts DESC", (user, metric)).fetchall()
    series, seen = [], set()
    for day, val in rows:
        if day in seen or day not in days:
            continue
        seen.add(day)
        series.append((day, val))
    if not series:
        return None, None, None, None
    latest_day, latest = series[0]
    if len(series) < 2:
        return latest, None, latest_day, None
    prev_day, prev = series[1]
    try:
        gap = (date.fromisoformat(latest_day) - date.fromisoformat(prev_day)).days
    except ValueError:
        gap = None
    return latest, round(latest - prev, 1), latest_day, gap


def compare_block(user: str) -> str:
    """"This scan vs the one before it" — sent the MOMENT a screenshot arrives, rather than
    making them wait until the weekly report to find out whether anything moved."""
    days = scan_days(user, limit=2)
    if not days:
        return ""
    latest = days[0]
    prev = days[1] if len(days) > 1 else None
    snap = nutrition.health_snapshot(user, day=latest, defs=BODY_STATS)
    if not snap:
        return ""

    if prev is None:
        # First scan on file: say so plainly rather than printing a comparison block with
        # every change blank, which just reads like a bug.
        return ("\U0001f4ca saved your body stats: "
                + nutrition.health_line(snap, defs=BODY_STATS)
                + "\nfirst scan on file — i'll compare the next one against this \U0001f4c8")

    try:
        gap = (date.fromisoformat(latest) - date.fromisoformat(prev)).days
    except ValueError:
        gap = None
    head = f"\U0001f4ca vs your last scan ({f'{gap} days ago' if gap else 'your last scan'})"

    rows, shown = [], set()
    for key, label, unit, hold in HEADLINE:
        now_v = snap.get(key)
        if now_v is None:
            continue
        shown.add(key)
        was = value_on(user, key, prev)
        line = f"{label}  {now_v:.1f}{unit}"
        if was is None:
            line += "  — (new)"
        else:
            d = round(now_v - was, 1)
            if hold and abs(d) < 0.3:
                line += "  held \U0001f3af"
            elif d == 0:
                line += "  — same"
            else:
                arrow = "↓" if d < 0 else "↑"
                line += f"  {arrow} {abs(d):.1f}{unit}"
        rows.append(line)
    if not rows:
        return ""

    rest = {k: v for k, v in snap.items() if k not in shown}
    tail = ("\n\nalso saved: " + nutrition.health_line(rest, defs=BODY_STATS)) if rest else ""
    return head + "\n\n" + "\n".join(rows) + tail


def week_meal_stats(user: str):
    """Average calories and protein per logged day over the last 7 days, or None."""
    since = (accounts.now_local(user).date() - timedelta(days=6)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, COALESCE(SUM(calories),0), COALESCE(SUM(protein),0) "
            "FROM meals WHERE user=? AND day >= ? GROUP BY day", (user, since)).fetchall()
    if not rows:
        return None
    goal = accounts.protein_goal(user) or 0
    n = len(rows)
    return {"days": n,
            "avg_cal": round(sum(r[1] for r in rows) / n),
            "avg_pro": round(sum(r[2] for r in rows) / n),
            "hit": sum(1 for r in rows if goal and r[2] >= goal)}


def week_metric_avg(user: str, metric: str):
    since = (accounts.now_local(user).date() - timedelta(days=6)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, value FROM health_metrics WHERE user=? AND metric=? AND day >= ? "
            "ORDER BY day, ts", (user, metric, since)).fetchall()
    by_day = {day: val for day, val in rows}  # last reading of each day wins
    return round(sum(by_day.values()) / len(by_day)) if by_day else None


def week_lines(user: str) -> tuple[list[str], list[str]]:
    """(body lines, food/activity lines) for the weekly report. Only lines we have data for,
    so a thin week degrades gracefully instead of printing blanks."""
    today = accounts.now_local(user)
    start = today - timedelta(days=6)
    on_plan = bool(db.get_profile(user).get("current_plan"))

    def age_note(day: str | None) -> str:
        """" · from 18 Aug" when the reading predates this report's week, so a number that
        stopped updating is never presented as if it were measured this week."""
        if not day:
            return ""
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return ""
        return "" if d >= start.date() else f"  · from {d:%d %b}"

    def body_line(metric, label, unit, hold):
        val, change, day, gap = scan_trend(user, metric)
        if val is None:
            return None
        line = f"{label}: {val:.1f}{unit}"
        if hold and change is not None and abs(change) < 0.3:
            line += "  (held 🎯"
        elif change:
            arrow = "📉" if change < 0 else "📈"
            line += f"  ({'down' if change < 0 else 'up'} {abs(change):.1f}{unit} {arrow}"
        else:
            return line + age_note(day)   # nothing to compare against yet
        # Always print the gap. A change is meaningless until you know how long it took.
        line += f" · vs {gap}d before)" if gap else ")"
        return line + age_note(day)

    body = [ln for ln in (body_line(*h) for h in HEADLINE) if ln]

    food = []
    meal = week_meal_stats(user)
    if meal:
        pg = accounts.protein_goal(user)
        # On someone's own structured plan, protein is a number they're TRACKING, not a target
        # they're failing. Scoring "hit 110g on 0/3 days" is exactly the pressure the plan is
        # meant to remove.
        if on_plan or not pg:
            food.append(f"💪 Protein: {meal['avg_pro']}g/day avg logged")
        else:
            food.append(f"💪 Protein: {meal['avg_pro']}g/day avg · hit {pg}g on "
                        f"{meal['hit']}/{meal['days']} days")
        cg = accounts.calorie_goal(user)
        food.append(f"🍽️ Calories: {meal['avg_cal']}/day avg" + (f" (target {cg})" if cg else ""))
    steps = week_metric_avg(user, "steps")
    if steps:
        food.append(f"🏃 Steps: {steps}/day avg")
    sleep = week_metric_avg(user, "sleep_hours")
    if sleep:
        food.append(f"😴 Sleep: ~{sleep}h/night avg")
    return body, food
