"""accounts.py — who the users are: sign-up, sign-in, sessions, invites, Telegram linking.

Every other module addresses a person by their account id as a string (e.g. "7"). That id is
the `user` column on every table, so a query that forgets to filter by it returns nobody
else's data by accident — it returns nothing.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from . import config, db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


# ---------- passwords ----------
# scrypt is in the standard library, so there is no dependency to install and nothing to keep
# patched. Each password gets its own random salt; the stored string carries the parameters,
# so they can be raised later without invalidating existing accounts.

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                             n=int(n), r=int(r), p=int(p), dklen=len(key_hex) // 2)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def password_problem(password: str) -> str | None:
    if len(password or "") < 8:
        return "Password needs to be at least 8 characters."
    return None


# ---------- invites ----------
# Sign-ups are invite-only because one shared free Groq key serves everyone: the number of
# active users is what decides whether the app feels instant or rate-limited.

def invite_ok(code: str) -> bool:
    code = (code or "").strip().lower()
    if not code:
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT max_uses, uses FROM invites WHERE code=?", (code,)).fetchone()
    return bool(row) and row[1] < row[0]


def spend_invite(code: str):
    with db.connect() as conn:
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE code=?", ((code or "").lower(),))


def create_invite(max_uses: int = 1) -> str:
    code = secrets.token_urlsafe(6).lower()
    with db.connect() as conn:
        conn.execute("INSERT INTO invites (code, max_uses, uses, created_at) VALUES (?,?,0,?)",
                     (code, max_uses, datetime.now().isoformat(timespec="seconds")))
    return code


def user_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]


# ---------- accounts ----------

def find_by_email(email: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, pw_hash, name, telegram_chat_id, link_code, onboarded, active "
            "FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
    return _row_to_user(row)


def find_by_id(user_id) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, pw_hash, name, telegram_chat_id, link_code, onboarded, active "
            "FROM users WHERE id=?", (int(user_id),)).fetchone()
    return _row_to_user(row)


def find_by_chat_id(chat_id) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, pw_hash, name, telegram_chat_id, link_code, onboarded, active "
            "FROM users WHERE telegram_chat_id=?", (str(chat_id),)).fetchone()
    return _row_to_user(row)


def find_by_link_code(code: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, pw_hash, name, telegram_chat_id, link_code, onboarded, active "
            "FROM users WHERE link_code=?", ((code or "").strip(),)).fetchone()
    return _row_to_user(row)


def _row_to_user(row) -> dict | None:
    if not row:
        return None
    return {"id": str(row[0]), "email": row[1], "pw_hash": row[2], "name": row[3],
            "chat_id": row[4], "link_code": row[5], "onboarded": bool(row[6]),
            "active": bool(row[7])}


def create_user(email: str, password: str, name: str) -> dict:
    email = (email or "").strip().lower()
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, pw_hash, name, created_at, link_code, onboarded, active) "
            "VALUES (?,?,?,?,?,0,1)",
            (email, hash_password(password), (name or "").strip(), now, new_link_code()))
        uid = cur.lastrowid
    return find_by_id(uid)


def new_link_code() -> str:
    """The one-time code a new account sends to the bot to prove which account it is."""
    return secrets.token_urlsafe(8)


def set_chat_id(user_id: str, chat_id):
    """Bind a Telegram chat to an account and burn the link code so it can't be reused."""
    with db.connect() as conn:
        conn.execute("UPDATE users SET telegram_chat_id=?, link_code=NULL WHERE id=?",
                     (str(chat_id), int(user_id)))


def unlink_chat(user_id: str) -> str:
    """Disconnect Telegram and hand back a fresh link code."""
    code = new_link_code()
    with db.connect() as conn:
        conn.execute("UPDATE users SET telegram_chat_id=NULL, link_code=? WHERE id=?",
                     (code, int(user_id)))
    return code


def mark_onboarded(user_id: str):
    with db.connect() as conn:
        conn.execute("UPDATE users SET onboarded=1 WHERE id=?", (int(user_id),))


def set_password(user_id: str, password: str):
    with db.connect() as conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?",
                     (hash_password(password), int(user_id)))


def active_users() -> list[dict]:
    """Everyone the scheduler should consider. Only accounts that finished onboarding AND
    connected Telegram can receive anything, so they are the only ones worth waking for."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, email, pw_hash, name, telegram_chat_id, link_code, onboarded, active "
            "FROM users WHERE active=1 AND onboarded=1 AND telegram_chat_id IS NOT NULL"
        ).fetchall()
    return [_row_to_user(r) for r in rows]


def delete_user(user_id: str):
    """Remove an account and everything belonging to it. Used by 'delete my account' —
    a person who signs up must be able to leave with their data actually gone."""
    uid = str(user_id)
    with db.connect() as conn:
        for table in ("meals", "health_metrics", "profile", "learned"):
            conn.execute(f"DELETE FROM {table} WHERE user=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (int(uid),))
        conn.execute("DELETE FROM users WHERE id=?", (int(uid),))


# ---------- sessions ----------

def start_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=config.SESSION_DAYS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("INSERT INTO sessions (token, user_id, expires) VALUES (?,?,?)",
                     (token, int(user_id), expires))
    return token


def session_user(token: str) -> dict | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT user_id, expires FROM sessions WHERE token=?",
                           (token,)).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row[1]) < datetime.now():
            end_session(token)
            return None
    except ValueError:
        return None
    user = find_by_id(row[0])
    return user if user and user["active"] else None


def end_session(token: str):
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---------- per-user time ----------

def now_local(user: str) -> datetime:
    """The current time in THIS user's timezone.

    Everything that decides "what day is it" — a meal's day, a daily total, whether a reminder
    is due — must go through here. A server running in UTC would otherwise roll a user's day
    over in the middle of their evening, and their dinner would land on tomorrow.
    """
    return datetime.now(config.tz(db.get_profile(user).get("timezone")))


def today(user: str) -> str:
    return now_local(user).strftime("%Y-%m-%d")


# ---------- targets ----------
# Calculated, never guessed, and always overridable — a person who knows their own numbers
# should be able to just say so.

ACTIVITY_FACTORS = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725, "very_active": 1.9,
}
GOAL_ADJUST = {           # kcal/day applied to maintenance
    "fat_loss": -400, "gentle_fat_loss": -250, "recomp": 0, "maintain": 0, "muscle_gain": 250,
}
PROTEIN_PER_KG = {        # g per kg bodyweight
    "fat_loss": 2.0, "gentle_fat_loss": 1.9, "recomp": 1.9, "maintain": 1.6, "muscle_gain": 1.8,
}


def bmr(weight_kg: float, height_cm: float, age: int, sex: str) -> float | None:
    """Mifflin-St Jeor. Returns None unless we have every input — a resting metabolic rate
    computed from a missing height is a confident-looking wrong number."""
    if not (weight_kg and height_cm and age):
        return None
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if sex == "male":
        return base + 5
    if sex == "female":
        return base - 161
    return base - 78  # midpoint when they'd rather not say


def computed_targets(prof: dict) -> dict:
    """{calories, protein, source} from the onboarding answers. `source` says how each number
    was reached so the settings page can show it, and so nothing is presented as measured
    when it was estimated."""
    weight = db.setting_float(prof, "weight_kg", None)
    height = db.setting_float(prof, "height_cm", None)
    age = db.setting_int(prof, "age", 0)
    sex = prof.get("sex", "")
    goal = prof.get("goal_type", "gentle_fat_loss")
    activity = prof.get("activity", "moderate")

    out = {"calories": None, "protein": None, "source": "not enough info yet"}
    b = bmr(weight, height, age, sex)
    if b:
        maintenance = b * ACTIVITY_FACTORS.get(activity, 1.55)
        cals = round(maintenance + GOAL_ADJUST.get(goal, 0))
        # A floor, because a deficit taken too far stalls training, wrecks sleep, and
        # backfires. Nothing here should ever prescribe a crash.
        floor = 1500 if sex == "male" else 1200
        out["calories"] = max(cals, floor)
        out["source"] = (f"Mifflin-St Jeor BMR {round(b)} kcal x {activity} activity, "
                         f"adjusted for {goal.replace('_', ' ')}")
    if weight:
        out["protein"] = round(weight * PROTEIN_PER_KG.get(goal, 1.8))
    return out


def calorie_goal(user: str) -> int | None:
    """What they set beats what we calculated; calculated beats nothing. None when we
    genuinely have no basis — better a missing number than an invented one."""
    prof = db.get_profile(user)
    manual = db.setting_int(prof, "calorie_goal", 0)
    if manual > 0:
        return manual
    return computed_targets(prof)["calories"]


def protein_goal(user: str) -> int | None:
    prof = db.get_profile(user)
    manual = db.setting_int(prof, "protein_goal", 0)
    if manual > 0:
        return manual
    return computed_targets(prof)["protein"]


def step_goal(user: str) -> int:
    return db.setting_int(db.get_profile(user), "step_goal", config.DEFAULT_STEP_GOAL)


def water_goal(user: str) -> int:
    return db.setting_int(db.get_profile(user), "water_goal_ml", config.DEFAULT_WATER_GOAL_ML)


def display_name(user: str) -> str:
    u = find_by_id(user)
    name = (u or {}).get("name") or ""
    return name.split()[0] if name else "there"


def health_token(user: str) -> str:
    """A per-user secret for the health-sync endpoint, minted on first use. Per-user so one
    person's leaked shortcut can never write into somebody else's body data."""
    prof = db.get_profile(user)
    tok = prof.get("health_token")
    if not tok:
        tok = secrets.token_urlsafe(18)
        db.set_profile(user, "health_token", tok)
    return tok


def user_by_health_token(token: str) -> str | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user FROM profile WHERE key='health_token' AND value=?", (token,)).fetchone()
    return row[0] if row else None


def media_dir(user: str, day: str) -> str:
    path = os.path.join(config.MEDIA_DIR, f"u{user}", day)
    os.makedirs(path, exist_ok=True)
    return path
