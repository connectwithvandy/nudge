"""nutrition.py — meals, macros and health metrics: the numbers, with no personality attached.

The rule that governs this whole file: the vision model reads the photo and the totals live in
SQLite, so the chat model only ever does the talking. It cannot invent anyone's numbers.

The second rule: a failed estimate must NEVER become a number. Anything that can't be parsed
returns None so the caller can ask, because a 0-kcal meal is invisible in the moment and then
quietly corrupts that day's totals and every weekly average after it.
"""
import re
from datetime import datetime, timedelta

from . import accounts, ai, config, db
from .db import BODY_STATS, HEALTH, MACROS, MICROS, NUTRIENTS

# The meal we last logged per user, so a follow-up message can correct it. In memory only —
# corrections are expected within minutes of a photo — with a DB fallback in recent_meal().
LAST_MEAL: dict[str, dict] = {}


# ---------- storing ----------

def log_meal(user: str, items: str, macros: dict, photo: str | None) -> int:
    now = accounts.now_local(user)
    keys = [k for k, _, _ in NUTRIENTS]
    cols = ", ".join(keys)
    marks = ", ".join("?" for _ in keys)
    vals = [int(macros.get(k, 0) or 0) for k in keys]
    with db.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO meals (user, ts, day, items, photo, {cols}) VALUES (?,?,?,?,?,{marks})",
            (user, now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
             items, photo, *vals))
        return cur.lastrowid


def remember_meal(user: str, meal_id: int, items: str, macros: dict):
    LAST_MEAL[user] = {"id": meal_id, "items": items, "macros": macros,
                       "ts": accounts.now_local(user)}


def recent_meal(user: str) -> dict | None:
    """The meal just logged, if it's still inside the correction window.

    The DB fallback matters: LAST_MEAL lives in RAM and every redeploy wipes it. Without it a
    correction sent after a restart isn't even recognised as a correction — it gets routed as
    a brand-new meal and double-logged, quietly inflating the day.
    """
    now = accounts.now_local(user)
    last = LAST_MEAL.get(user)
    if last and (now - last["ts"]).total_seconds() <= config.CORRECTION_WINDOW_MIN * 60:
        return last
    keys = [k for k, _, _ in NUTRIENTS]
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT id, ts, items, {', '.join(keys)} FROM meals WHERE user=? "
            "ORDER BY id DESC LIMIT 1", (user,)).fetchone()
    if not row:
        return None
    try:
        ts = datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    # A row written before the user had a timezone (or by an older build) has no offset, and
    # subtracting it from an aware "now" raises — which would take down the whole correction
    # path for that person with a TypeError, not a missing meal. Assume their own zone.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    if (now - ts).total_seconds() > config.CORRECTION_WINDOW_MIN * 60:
        return None
    found = {"id": row[0], "items": row[2], "ts": ts,
             "macros": {k: row[3 + i] or 0 for i, k in enumerate(keys)}}
    LAST_MEAL[user] = found
    return found


def update_meal(meal_id: int, items: str, macros: dict):
    keys = [k for k, _, _ in NUTRIENTS]
    sets = "items=?, " + ", ".join(f"{k}=?" for k in keys)
    vals = [items] + [int(macros.get(k, 0) or 0) for k in keys]
    with db.connect() as conn:
        conn.execute(f"UPDATE meals SET {sets} WHERE id=?", (*vals, meal_id))


def day_totals(user: str, day: str | None = None) -> tuple[int, dict]:
    day = day or accounts.today(user)
    cols = ", ".join(f"COALESCE(SUM({k}),0)" for k, _, _ in NUTRIENTS)
    with db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*), {cols} FROM meals WHERE user=? AND day=?",
                           (user, day)).fetchone()
    return row[0], {k: row[i + 1] for i, (k, _, _) in enumerate(NUTRIENTS)}


def logged_meal_recently(user: str, hours: int) -> bool:
    cutoff = (accounts.now_local(user) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM meals WHERE user=? AND ts>=? LIMIT 1",
                           (user, cutoff)).fetchone()
    return row is not None


# ---------- formatting ----------

def macro_line(macros: dict) -> str:
    parts = []
    for k, label, unit in MACROS:
        v = macros.get(k, 0)
        parts.append(f"{v} kcal" if k == "calories" else f"{v}{unit} {label}")
    return ", ".join(parts)


def micro_line(macros: dict) -> str:
    """Always prefixed with "~" — these are photo estimates, not lab values."""
    return ", ".join(f"~{macros.get(k, 0)}{unit} {label}" for k, label, unit in MICROS)


def meal_macros(macros: dict) -> str:
    return (f"{macros.get('calories', 0)} kcal · {macros.get('protein', 0)}g protein"
            f" · {macros.get('carbs', 0)}g carbs · {macros.get('fat', 0)}g fat")


def calorie_line(goal: int | None, have: int) -> str:
    if not goal:
        return f"🔥  {have} kcal"
    if have <= goal:
        return f"🔥  {have} / {goal} kcal  ·  {goal - have} left"
    return f"🔥  {have} / {goal} kcal  ·  {have - goal} over"


def protein_line(goal: int | None, have: int) -> str:
    if not goal:
        return f"💪  {have}g protein"
    tail = f"{goal - have}g to go" if have < goal else "goal smashed 🎯"
    return f"💪  {have} / {goal}g protein  ·  {tail}"


# ---------- health metrics ----------

def record_health(user: str, metrics: dict):
    now = accounts.now_local(user)
    known = {k for k, _, _, _ in db.ALL_METRICS}
    with db.connect() as conn:
        for metric, value in metrics.items():
            if metric not in known or value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            conn.execute(
                "INSERT INTO health_metrics (user, ts, day, metric, value) VALUES (?,?,?,?,?)",
                (user, now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                 metric, value))


def health_snapshot(user: str, day: str | None = None, defs=HEALTH) -> dict:
    day = day or accounts.today(user)
    out = {}
    with db.connect() as conn:
        for k, _, _, _ in defs:
            row = conn.execute(
                "SELECT value FROM health_metrics WHERE user=? AND day=? AND metric=? "
                "ORDER BY ts DESC LIMIT 1", (user, day, k)).fetchone()
            if row is not None:
                out[k] = row[0]
    return out


def health_line(snap: dict, defs=HEALTH) -> str:
    parts = []
    for k, label, unit, is_int in defs:
        if k not in snap:
            continue
        v = int(snap[k]) if is_int else round(snap[k], 1)
        parts.append(f"{v}{unit} {label}" if unit else f"{v} {label}")
    return ", ".join(parts) if parts else "no health data synced yet"


def latest_metric(user: str, metric: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM health_metrics WHERE user=? AND metric=? ORDER BY ts DESC LIMIT 1",
            (user, metric)).fetchone()
    return row[0] if row else None


def current_weight(user: str) -> float | None:
    """The best-known weight: a synced reading if there is one, else what they typed during
    onboarding — so a broken sync never blocks their targets."""
    wt = latest_metric(user, "weight_kg")
    if wt and wt > 0:
        return wt
    return db.setting_float(db.get_profile(user), "weight_kg", None)


# ---------- reading the model's answers ----------

def parse_nutrients(text: str) -> tuple[str, dict] | None:
    """Pull ITEMS plus every nutrient out of a "KEY: value" reply. Tolerates a model that
    thinks out loud first — we just grep the labelled lines.

    Returns None when there is no usable CALORIES line, which happens when a chatty vision
    model spends its whole budget itemising the plate and gets cut off before the block.
    Silent zeros are worse than no answer.
    """
    cal = re.search(r"CALORIES:\s*(\d+)", text)
    if not cal or int(cal.group(1)) <= 0:
        return None
    items_m = re.search(r"ITEMS:\s*(.+)", text)
    items = items_m.group(1).strip() if items_m else "your meal"
    macros = {}
    for k, _, _ in NUTRIENTS:
        m = re.search(rf"{k.upper()}:\s*(\d+)", text)
        macros[k] = int(m.group(1)) if m else 0
    return items, macros


_BLOCK = ("ITEMS: <comma-separated food items with rough portions>\n"
          "CALORIES: <kcal>\nPROTEIN: <g>\nCARBS: <g>\nFAT: <g>\nFIBER: <g>\n"
          "SUGAR: <g>\nSODIUM: <mg>\nIRON: <mg>\nCALCIUM: <mg>")


def _diet_hint(prof: dict) -> str:
    diet = prof.get("diet", "")
    cuisine = prof.get("cuisine", "")
    bits = []
    if diet and diet != "anything":
        bits.append(f"They eat a {diet.replace('_', ' ')} diet")
    if cuisine:
        bits.append(f"their food is usually {cuisine}")
    return (". ".join(bits) + ". ") if bits else ""


def vision_estimate(user: str, image: bytes, note: str = "") -> tuple[str, dict] | None:
    """Read a meal photo (plus any caption) into (items, {nutrient: value}), or None if the
    model never produced usable numbers — the caller then asks instead of logging zeros."""
    prof = db.get_profile(user)
    caption = (f"\nThe person also told you what this is: \"{note.strip()}\". TRUST their items "
               "and portions over your visual guess — they know what they ate.\n"
               if note and note.strip() else "\n")
    # The block comes FIRST and prose is banned outright. Asking a model to "think briefly,
    # then end with the block" is what breaks this: it writes a long itemised breakdown, hits
    # the token ceiling, and never reaches the numbers — so the meal logs as 0 kcal. Whatever
    # the model writes first is the one part that can never be truncated away.
    prompt = (
        "You are a meticulous nutrition estimator. " + _diet_hint(prof) +
        "Silently judge each food's PORTION from visual scale cues — plate, bowl or cup size, "
        "a hand, how full the dish is — and assume normal home cooking (some oil or butter) "
        "unless it visibly looks fat-free, then add the items up to a total."
        f"{caption}"
        "Reply with ONLY the block below and NOTHING else — no working, no explanation, no "
        "per-item breakdown, no markdown. Integers only (grams for macros and sugar, mg for "
        "sodium, iron and calcium):\n" + _BLOCK)
    for attempt in range(2):  # one retry: a chatty model sometimes ignores "block only"
        r = ai.client.chat(
            model=ai.VISION_MODEL,
            messages=[{"role": "user", "content": prompt, "images": [image]}],
            options={"num_ctx": 8192, "num_predict": 1200})
        parsed = parse_nutrients(r["message"]["content"])
        if parsed:
            return parsed
        print(f"[vision] no nutrition block in the reply (attempt {attempt + 1}/2)")
    return None


def recompute_meal(user: str, items: str, macros: dict, correction: str) -> tuple[str, dict]:
    """Re-estimate a whole meal from the original plus a correction in words ("3 rotis not 1",
    "big bowl", "add a spoon of oil"). Raises when there are no usable numbers, so the caller
    asks rather than silently overwriting a real meal with zeros."""
    prompt = (
        "You are a meticulous nutrition estimator. " + _diet_hint(db.get_profile(user)) +
        "A meal was logged as:\n"
        f"ITEMS: {items}\n{macro_line(macros)}; {micro_line(macros)}\n\n"
        f"The person now corrects or clarifies it: \"{correction.strip()}\"\n"
        "Apply their correction (trust them on portions and items) and recompute the WHOLE "
        "meal. Reply with ONLY this block, integers only:\n" + _BLOCK)
    r = ai.client.chat(model=ai.CHAT_MODEL, messages=[{"role": "user", "content": prompt}],
                       options={"num_predict": 400})
    parsed = parse_nutrients(r["message"]["content"])
    if not parsed:
        raise ValueError("recompute_meal: no nutrition block in the reply")
    return parsed


def estimate_text_meal(user: str, text: str, system_msgs: list[dict]) -> tuple[str, dict] | None:
    """"had a protein shake and two slices of toast" -> (items, macros), or None when the
    message isn't actually reporting food (so it falls through to normal chat)."""
    prompt = (
        "You are a meal logger and nutrition estimator. " + _diet_hint(db.get_profile(user)) +
        "Decide if the message is the person REPORTING food or drink they actually consumed "
        "(now or earlier today).\n"
        "IS a meal report: 'had a protein shake and 2 toast', 'just ate dal and rice', "
        "'lunch was a chicken wrap', 'finished a bowl of oats'.\n"
        "NOT a meal report (reply NONE): questions, meal planning, 'what should i eat', "
        "'i love pizza', 'feeling hungry', or correcting a previous meal.\n\n"
        "If NOT a meal report, reply with EXACTLY: NONE\n"
        "If it IS, estimate the full nutrition. If the message lists a WHOLE DAY of food "
        "(breakfast + lunch + dinner...), estimate the TOTAL across everything listed. Reply "
        "with ONLY this block, integers only:\n" + _BLOCK + f"\n\nMessage: \"{text.strip()}\"")
    r = ai.client.chat(model=ai.CHAT_MODEL,
                       messages=system_msgs + [{"role": "user", "content": prompt}],
                       options={"num_predict": 400})
    out = r["message"]["content"]
    if out.strip().upper().startswith("NONE") or "ITEMS:" not in out.upper():
        return None
    parsed = parse_nutrients(out)
    if not parsed:
        return None  # no usable numbers — chat about it rather than log zeros
    items, macros = parsed
    if not items or items == "your meal" or (macros.get("calories", 0) == 0
                                             and macros.get("protein", 0) == 0):
        return None
    return items, macros


# ---------- photo classification and body-scan reading ----------

def classify_image(image: bytes) -> str:
    """PLAN (a diet chart), STATS (a body-composition screen), or FOOD."""
    prompt = (
        "Look at this image and answer with ONE word only:\n"
        "PLAN  - if it is a diet plan, weekly meal chart, food schedule, or a coach's rules "
        "or supplement list\n"
        "STATS - if it is an app screenshot or chart of BODY measurements (weight, BMI, body "
        "fat %, muscle mass, body water, BMR)\n"
        "FOOD  - if it shows actual food, a meal, a plate, or a drink")
    r = ai.client.chat(model=ai.VISION_MODEL,
                       messages=[{"role": "user", "content": prompt, "images": [image]}],
                       options={"num_ctx": 8192, "num_predict": 4})
    out = r["message"]["content"].upper()
    if "PLAN" in out:
        return "PLAN"
    return "STATS" if "STAT" in out else "FOOD"


def parse_body_stats(text: str) -> dict:
    """Pull BODY_STATS values out of a "KEY: value" reply.

    A value written as a percentage is REFUSED for a *_kg key. Scale apps show "Bone(%) 4.3%",
    and a model will happily return that as BONE_MASS_KG — so the file then holds 4.3 "kg" of
    bone that is really 4.3 percent. It reads as plausible, which is exactly why nobody catches
    it later.
    """
    out = {}
    for k, _, _, is_int in BODY_STATS:
        m = re.search(rf"{k.upper()}:\s*([\d]+(?:\.[\d]+)?)\s*(%?)", text)
        if not m:
            continue
        if m.group(2) == "%" and k.endswith("_kg"):
            print(f"[body-stats] refused {k}={m.group(1)}% — that's a percentage, not kg")
            continue
        out[k] = int(float(m.group(1))) if is_int else float(m.group(1))
    return out


def vision_body_stats(image: bytes) -> dict:
    keys = ", ".join(k.upper() for k, _, _, _ in BODY_STATS)
    prompt = (
        "This is a screenshot from a body-composition or smart-scale app. Read the "
        "measurements shown. Respond ONLY with 'KEY: value' lines, numbers only, and OMIT any "
        f"key whose value you cannot clearly see. Weights in kg. Possible keys:\n{keys}\n\n"
        # Scale apps all name things differently, and a bare key list leaves the model
        # guessing — which is how the single number a muscle-retention goal turns on gets
        # silently dropped and appears frozen for weeks. Spell the mapping out.
        "Common label mappings — follow them exactly, and OMIT anything you would have to "
        "convert or guess:\n"
        "- MUSCLE_MASS_KG = the muscle figure in KILOGRAMS, often labelled 'Lean body mass'. "
        "NEVER use 'Skeletal Muscle(%)' or 'Muscle(%)' — those are percentages.\n"
        "- BONE_MASS_KG = bone in KILOGRAMS only. If bone is shown as a percentage, OMIT it.\n"
        "- METABOLIC_AGE = 'Body Age'. VISCERAL_FAT = 'Visceral Fat Index'.\n"
        "- Never put a percentage into a *_KG key. Keep the % sign on any value you do report "
        "as a percentage.")
    r = ai.client.chat(model=ai.VISION_MODEL,
                       messages=[{"role": "user", "content": prompt, "images": [image]}],
                       options={"num_ctx": 8192})
    text = r["message"]["content"]
    parsed = parse_body_stats(text)
    print(f"[body-stats] model read: {text!r}\n[body-stats] parsed: {parsed}")
    return parsed


def vision_plan_extract(image: bytes) -> str:
    """Transcribe a diet-plan or meal-chart image into clean text a coach can follow."""
    prompt = (
        "This is a diet plan or weekly meal chart. Transcribe it FAITHFULLY into clean, "
        "compact text. Include: any date range shown; each day's meals by slot (early morning, "
        "breakfast, mid-meals, lunch, dinner) with items and portions or counts; and any rules, "
        "eliminations, free foods, or supplement notes visible. Do not invent anything that is "
        "not in the image. Plain text only, no commentary.")
    r = ai.client.chat(model=ai.VISION_MODEL,
                       messages=[{"role": "user", "content": prompt, "images": [image]}],
                       options={"num_ctx": 8192, "num_predict": 1000})
    return r["message"]["content"].strip()
