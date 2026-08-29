"""db.py — the single SQLite database, its schema, and the cloud snapshot that outlives a deploy.

Every per-person table carries a `user` column holding the account's id as text, so one
database serves every signed-up user and no query can accidentally mix two people's data.
Always filter by `user`.
"""
import base64
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta

import httpx

from . import config

# Nutrition we store per meal. (key, label, unit)
MACROS = [("calories", "calories", "kcal"), ("protein", "protein", "g"),
          ("carbs", "carbs", "g"), ("fat", "fat", "g"), ("fiber", "fiber", "g")]
# Rough, photo-based estimates — never lab-precise, always shown with a "~".
MICROS = [("sugar", "sugar", "g"), ("sodium", "sodium", "mg"),
          ("iron", "iron", "mg"), ("calcium", "calcium", "mg")]
NUTRIENTS = MACROS + MICROS

# Metrics a phone or band can sync. (key, label, unit, integer?)
HEALTH = [("steps", "steps", "", True), ("water_ml", "water", "ml", True),
          ("sleep_hours", "sleep", "h", False), ("weight_kg", "weight", "kg", False),
          ("resting_hr", "resting HR", "bpm", True), ("active_kcal", "active", "kcal", True),
          ("body_fat", "body fat", "%", False)]

# Body-composition metrics read from a smart-scale app screenshot.
BODY_STATS = [
    ("weight_kg", "weight", "kg", False), ("bmi", "BMI", "", False),
    ("body_fat", "body fat", "%", False), ("body_water_pct", "body water", "%", False),
    ("muscle_mass_kg", "muscle mass", "kg", False), ("bone_mass_kg", "bone mass", "kg", False),
    ("protein_pct", "protein", "%", False), ("bmr_kcal", "BMR", "kcal", True),
    ("visceral_fat", "visceral fat", "", False), ("metabolic_age", "metabolic age", "yr", True),
]

HEALTH_KEYS = {k for k, _, _, _ in HEALTH}
ALL_METRICS = HEALTH + [m for m in BODY_STATS if m[0] not in HEALTH_KEYS]


def connect():
    """A fresh connection per call — simple and thread-safe. Creates/migrates tables on first
    use each boot, which is cheap and means a new column never needs a migration script."""
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, created_at TEXT, "
        "telegram_chat_id TEXT, link_code TEXT, onboarded INTEGER DEFAULT 0, "
        "active INTEGER DEFAULT 1)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "token TEXT PRIMARY KEY, user_id INTEGER, expires TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invites ("
        "code TEXT PRIMARY KEY, max_uses INTEGER, uses INTEGER DEFAULT 0, created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meals ("
        "id INTEGER PRIMARY KEY, user TEXT, ts TEXT, day TEXT, items TEXT, photo TEXT)")
    have = {row[1] for row in conn.execute("PRAGMA table_info(meals)")}
    for k, _, _ in NUTRIENTS:
        if k not in have:
            conn.execute(f"ALTER TABLE meals ADD COLUMN {k} INTEGER DEFAULT 0")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS health_metrics ("
        "id INTEGER PRIMARY KEY, user TEXT, ts TEXT, day TEXT, metric TEXT, value REAL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profile ("
        "user TEXT, key TEXT, value TEXT, ts TEXT, PRIMARY KEY(user, key))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learned ("
        "id INTEGER PRIMARY KEY, user TEXT, fact TEXT, ts TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_meals_user_day ON meals(user, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_health_user_metric "
                 "ON health_metrics(user, metric, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_chat ON users(telegram_chat_id)")
    return conn


# ---------- profile: every per-user setting and remembered fact, as key/value ----------

def set_profile(user: str, key: str, value: str):
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("INSERT INTO profile (user, key, value, ts) VALUES (?,?,?,?) "
                     "ON CONFLICT(user, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                     (user, key, str(value), now))


def get_profile(user: str) -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM profile WHERE user=?", (user,)).fetchall()
    return {k: v for k, v in rows}


def setting(user: str, key: str, default=None):
    return get_profile(user).get(key) or default


def setting_int(prof: dict, key: str, default: int) -> int:
    try:
        return int(float(prof.get(key)))
    except (TypeError, ValueError):
        return default


def setting_float(prof: dict, key: str, default: float | None) -> float | None:
    try:
        return float(prof.get(key))
    except (TypeError, ValueError):
        return default


# ---------- cloud snapshot (memory that survives a redeploy) ----------
# Free hosts wipe their disk on every deploy. Everything below is best-effort and must never
# be able to take the app down: no Turso configured simply means the feature is off.

_last_backup_sha: str | None = None


def _turso_pipeline(reqs: list[dict]) -> list[dict] | None:
    if not config.TURSO_URL:
        return None
    url = config.TURSO_URL.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
    try:
        r = httpx.post(url, timeout=30,
                       headers={"Authorization": f"Bearer {config.TURSO_TOKEN or ''}"},
                       json={"requests": reqs + [{"type": "close"}]})
        r.raise_for_status()
        results = r.json().get("results", [])
        bad = [x for x in results if x.get("type") != "ok"]
        if bad:
            print(f"[backup] turso rejected a statement: {bad[0].get('error')}")
            return None
        return results
    except Exception as e:
        print(f"[backup] turso unreachable: {e!r}")
        return None


def backup_to_cloud():
    """Snapshot the whole database file into one Turso row. Schema-agnostic, so any table
    added later rides along without touching this. Skips quietly when nothing changed."""
    global _last_backup_sha
    if not config.TURSO_URL:
        return
    try:
        conn = sqlite3.connect(config.DB_PATH)
        data = bytes(conn.serialize())  # point-in-time copy, safe alongside writers
        conn.close()
    except Exception as e:
        print(f"[backup] couldn't read local db: {e!r}")
        return
    sha = hashlib.sha256(data).hexdigest()
    if sha == _last_backup_sha:
        return
    ok = _turso_pipeline([
        {"type": "execute", "stmt": {"sql":
            "CREATE TABLE IF NOT EXISTS db_backup "
            "(id INTEGER PRIMARY KEY, data BLOB, saved_at TEXT)"}},
        {"type": "execute", "stmt": {
            "sql": "INSERT INTO db_backup (id, data, saved_at) VALUES (1, ?, ?) "
                   "ON CONFLICT(id) DO UPDATE SET data=excluded.data, saved_at=excluded.saved_at",
            "args": [{"type": "blob", "base64": base64.b64encode(data).decode()},
                     {"type": "text", "value": datetime.now().isoformat(timespec="seconds")}]}},
    ])
    if ok:
        _last_backup_sha = sha
        print(f"[backup] pushed {max(1, len(data) // 1024)}KB to cloud")


def restore_from_cloud():
    """On a fresh boot with NO local file, pull the last snapshot back. Never overwrites an
    existing local database — the local file is always the boss."""
    if not config.TURSO_URL:
        return
    if os.path.exists(config.DB_PATH):
        print("[backup] local db exists — no restore needed")
        return
    res = _turso_pipeline([{"type": "execute",
                            "stmt": {"sql": "SELECT data FROM db_backup WHERE id=1"}}])
    try:
        blob = res[0]["response"]["result"]["rows"][0][0]["base64"]
    except Exception:
        print("[backup] no cloud snapshot yet — starting fresh")
        return
    data = base64.b64decode(blob + "=" * (-len(blob) % 4))  # tolerate unpadded base64
    with open(config.DB_PATH, "wb") as f:
        f.write(data)
    print(f"[backup] restored {max(1, len(data) // 1024)}KB — memory survived the redeploy")


def purge_expired_sessions():
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?",
                     (datetime.now().isoformat(timespec="seconds"),))


def seed_invites():
    """Make the INVITE_CODES from the environment usable. Existing codes keep their use count,
    so a redeploy never silently hands out fresh signups on a code that was already spent."""
    if not config.INVITE_CODES:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        for code in config.INVITE_CODES:
            conn.execute("INSERT OR IGNORE INTO invites (code, max_uses, uses, created_at) "
                         "VALUES (?,?,0,?)", (code.lower(), config.INVITE_USES, now))
    print(f"[invites] {len(config.INVITE_CODES)} code(s) from the environment are live")


def days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
