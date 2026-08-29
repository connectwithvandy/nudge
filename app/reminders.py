"""reminders.py — proactive nudges, on each user's own clock.

A single-user app can hang one cron job off each reminder. With many users in many timezones
that breaks down immediately, so instead ONE job runs every minute and asks, for each connected
user: what time is it where you are, and is anything due?

That inversion is what makes per-user times and timezones possible at all, and it means adding
a new kind of reminder is a few lines here rather than a new scheduler entry.

Three guards apply to every nudge, in this order:
  1. a break ("go quiet till monday") silences everything;
  2. a mute silences one kind, indefinitely;
  3. a per-user gap stops two nudges landing in the same minute.
Only the first two are the user's choice, and they are honoured before an AI call is even
made — composing a message nobody will receive wastes the shared free-tier budget.
"""
import traceback
from . import accounts, bodystats, coach, db, messenger, nutrition

DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Every reminder kind, and the profile key holding its time(s). Kinds a user hasn't switched
# on in `reminders_on` never fire.
ALL_KINDS = ("gym", "water", "meals", "supplements", "steps", "winddown", "summary", "weekly")


def enabled_kinds(prof: dict) -> set[str]:
    raw = prof.get("reminders_on")
    if raw is None:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def _parse_hhmm(value: str):
    try:
        h, _, m = (value or "").strip().partition(":")
        h, m = int(h), int(m or 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return None


def _times(value: str) -> list[tuple[int, int, str]]:
    """Parse "13:00=lunch, 20:00=dinner" or "13:15,19:15" into [(h, m, label)]."""
    out = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        clock, _, label = entry.partition("=")
        hm = _parse_hhmm(clock)
        if hm:
            out.append((hm[0], hm[1], label.strip()))
    return out


def dow_matches(spec: str, when) -> bool:
    """Does `when` fall on a day in "mon,wed" or "mon-fri"? Empty spec means every day."""
    spec = (spec or "").strip().lower()
    if not spec or spec in ("all", "daily", "everyday", "every day"):
        return True
    k = when.weekday()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a in DOW and b in DOW:
                i, j = DOW.index(a), DOW.index(b)
                if (i <= k <= j) if i <= j else (k >= i or k <= j):  # wraps, e.g. sat-sun
                    return True
        elif part in DOW and DOW.index(part) == k:
            return True
    return False


def _already_fired(user: str, tag: str, day: str) -> bool:
    """Persisted, not in-memory: a redeploy in the middle of the day would otherwise re-fire
    every nudge already sent, which is exactly the kind of burst that gets an app muted."""
    return db.get_profile(user).get(f"fired_{tag}") == day


def _mark_fired(user: str, tag: str, day: str):
    db.set_profile(user, f"fired_{tag}", day)


def _due(user: str, now, tag: str, hour: int, minute: int, grace: int = 3) -> bool:
    """True once per day, at or just after the target minute.

    The grace window matters: the sweep can be delayed by a slow tick or a redeploy, and an
    exact-minute match would silently drop that day's nudge with nothing to show why.
    """
    day = now.strftime("%Y-%m-%d")
    if _already_fired(user, tag, day):
        return False
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (now - target).total_seconds()
    if 0 <= delta <= grace * 60:
        _mark_fired(user, tag, day)
        return True
    return False


def _say(user: str, situation: str, fallback: str) -> str:
    try:
        return coach.say_in_voice(user, situation)
    except Exception:
        return fallback


# ---------- the individual nudges ----------

def _gym(user: str, prof: dict, now):
    cond = coach.active_condition(user)
    if cond and cond["kind"] in ("illness", "injury"):
        situation = (f"It's their usual training slot, but right now they are {cond['note']}. "
                     "DO NOT push training or hype them up. Warmly check in on how they're "
                     "feeling and reassure them it's completely fine to rest and recover today "
                     "— the muscle isn't going anywhere. One short, caring line.")
        fallback = ("morning 💙 how are you feeling today? no pressure on training — rest up "
                    "and recover, okay?")
    elif cond and cond["kind"] == "period":
        situation = (f"It's their training slot and right now they are {cond['note']}. Check in "
                     "KINDLY, no hype and no pressure — a lighter session or a rest day is "
                     "completely fine this week and lower energy is normal, not a setback. One "
                     "short, caring line.")
        fallback = ("morning 💙 train only if you feel up to it — a lighter day is totally fine "
                    "this week. listen to your body 🤍")
    else:
        situation = ("Training check-in — their session is coming up shortly. Ask them, warmly "
                     "and playfully, if they're heading in today. One short line; make them "
                     "want to say yes. 🏋️")
        fallback = "training o'clock soon 🏋️ you heading in today? go get it 💪"
    messenger.send_reminder(user, _say(user, situation, fallback), kind="gym")


def _water(user: str, prof: dict, now):
    goal = accounts.water_goal(user)
    snap = nutrition.health_snapshot(user)
    ml = int(snap["water_ml"]) if "water_ml" in snap else None
    if ml is not None and ml >= goal:
        return  # already there — don't nag
    situation = (f"Quick water nudge. Their goal is {goal}ml. "
                 + (f"They've had {ml}ml so far. " if ml is not None else "")
                 + "One short, warm one-liner telling them to drink some water now. 💧")
    messenger.send_reminder(
        user, _say(user, situation, "water break! 💧 grab a glass, you'll thank yourself later"),
        kind="water")


def _steps(user: str, prof: dict, now):
    goal = accounts.step_goal(user)
    snap = nutrition.health_snapshot(user)
    if "steps" not in snap:
        return  # no step data — a nudge about a number we don't have is just noise
    steps = int(snap["steps"])
    if steps >= goal:
        return
    situation = (f"Evening step nudge. They're at {steps}/{goal} steps today. Encourage a short "
                 "walk to close the gap — warm, never shame, one or two lines. 🚶")
    messenger.send_reminder(
        user, _say(user, situation, f"you're at {steps}/{goal} steps 🚶 a quick walk would feel "
                                    "good right now"), kind="steps")


def _meal(user: str, prof: dict, now, label: str):
    skip_hours = db.setting_int(prof, "meal_skip_hours", 2)
    if nutrition.logged_meal_recently(user, skip_hours):
        return  # they're already on it
    meal = label or "your meal"
    situation = (f"It's about {meal} time. Remind them — warmly and playfully — to snap a quick "
                 f"photo of {meal} and send it so you can log the macros. One short line, make "
                 "it feel easy, never naggy. 📸")
    messenger.send_reminder(
        user, _say(user, situation, f"pic time! 📸 send me a snap of {meal} and i'll do the "
                                    "macros 🍽️"), kind="meals")


def _supplement(user: str, prof: dict, now, what: str):
    situation = (f"Reminder for: {what}. Nudge them warmly and a little playfully to take it "
                 "now — short, friendly, a tiny caring poke, never naggy. Name it. 💊")
    messenger.send_reminder(user, _say(user, situation, f"💊 {what} — time for it"),
                            kind="supplements")


def _winddown(user: str, prof: dict, now):
    situation = ("It's their wind-down time and they're aiming to be asleep soon. One short, "
                 "cozy line nudging them to start winding down — screens off soon, tomorrow "
                 "comes early. Warm, zero guilt. 🌙")
    messenger.send_reminder(
        user, _say(user, situation, "🌙 start winding down? good sleep makes tomorrow feel "
                                    "easy 💙"), kind="winddown")


def _summary(user: str, prof: dict, now):
    try:
        messenger.send_reminder(user, coach.summary_line(user), kind="summary")
    except Exception:
        traceback.print_exc()


def _weekly(user: str, prof: dict, now):
    try:
        messenger.send_reminder(user, coach.weekly_summary_line(user), kind="weekly")
    except Exception:
        traceback.print_exc()


# ---------- the sweep ----------

def sweep():
    """Run every minute. Each user is handled inside its own try, so one bad timezone or one
    unreachable chat can never stop everybody else's reminders."""
    for acct in accounts.active_users():
        try:
            _sweep_user(acct["id"])
        except Exception:
            traceback.print_exc()


def _sweep_user(user: str):
    prof = db.get_profile(user)
    on = enabled_kinds(prof)
    if not on:
        return
    if coach.quiet_until(user):
        return  # on a break: don't even compose a message they won't receive
    now = accounts.now_local(user)
    muted = coach.muted_kinds(user)

    def live(kind: str) -> bool:
        return kind in on and kind not in muted

    if live("gym") and dow_matches(prof.get("gym_days", ""), now):
        hm = _parse_hhmm(prof.get("gym_time", ""))
        if hm and _due(user, now, "gym", *hm):
            _gym(user, prof, now)

    if live("water"):
        for i, (h, m, _) in enumerate(_times(prof.get("water_times", ""))):
            if _due(user, now, f"water{i}", h, m):
                _water(user, prof, now)

    if live("meals"):
        for i, (h, m, label) in enumerate(_times(prof.get("meal_times", ""))):
            if _due(user, now, f"meal{i}", h, m):
                _meal(user, prof, now, label)

    if live("supplements"):
        for i, (name, hm, days) in enumerate(parse_supplements(prof.get("supplements", ""))):
            if dow_matches(days, now) and _due(user, now, f"supp{i}", *hm):
                _supplement(user, prof, now, name)

    if live("steps"):
        hm = _parse_hhmm(prof.get("step_time", "18:30"))
        if hm and _due(user, now, "steps", *hm):
            _steps(user, prof, now)

    if live("winddown"):
        hm = _parse_hhmm(prof.get("winddown_time", ""))
        if hm and _due(user, now, "winddown", *hm):
            _winddown(user, prof, now)

    if live("summary"):
        hm = _parse_hhmm(prof.get("summary_time", ""))
        if hm and _due(user, now, "summary", *hm):
            _summary(user, prof, now)

    if live("weekly") and dow_matches(prof.get("weekly_day", "mon"), now):
        hm = _parse_hhmm(prof.get("weekly_time", ""))
        if hm and _due(user, now, "weekly", *hm):
            _weekly(user, prof, now)


def parse_supplements(raw: str) -> list[tuple[str, tuple[int, int], str]]:
    """One supplement per line: "Vitamin D 1000IU | 09:00 | mon,thu".

    Free text on purpose. Everyone's stack is different, and a fixed list of vitamins with
    fixed times is exactly the kind of thing that only ever fits the person who wrote it.
    Days may be omitted, which means daily. A malformed line is skipped, never guessed at.
    """
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        hm = _parse_hhmm(parts[1])
        if not hm:
            continue
        days = parts[2] if len(parts) > 2 else ""
        out.append((parts[0], hm, days))
    return out


def clear_old_fired_flags():
    """Housekeeping: a fired_* marker only means anything for the day it names, and left alone
    they accumulate one row per reminder per user forever."""
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM profile WHERE key LIKE 'fired_%' AND value < date('now','-2 day')")
