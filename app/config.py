"""config.py — every setting Nudge reads from the environment, in one place.

Nothing here is about a particular person. Anything that varies per user (targets, reminder
times, timezone, tone, diet) lives in that user's row in the database, not in an env var —
that is the whole difference between this and a single-user app.
"""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def env(key: str, default: str = "") -> str:
    """Read an env var and ALWAYS strip it.

    A value pasted into a hosting dashboard carries a trailing newline, and httpx rejects
    that client-side ("Illegal header value") before any request is sent — so a token that
    looks correct in the dashboard fails every call with an error that points nowhere near
    the cause. Never read os.environ directly for a secret; go through here.
    """
    return (os.environ.get(key) or default).strip()


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_NAME = env("APP_NAME", "Nudge")
# Shown on the heartbeat so "did my fix actually deploy?" is answerable from outside.
DEPLOYED_COMMIT = (env("RENDER_GIT_COMMIT", "dev"))[:7] or "dev"
PUBLIC_URL = env("PUBLIC_URL") or env("RENDER_EXTERNAL_URL")

DB_PATH = env("DB_PATH") or os.path.join(BASE, "nudge.db")
MEDIA_DIR = env("MEDIA_DIR") or os.path.join(BASE, "media")

# ---- the AI brain (free tiers only) ------------------------------------------------------
GROQ_API_KEY = env("GROQ_API_KEY") or None
GROQ_CHAT_MODEL = env("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
GROQ_CHAT_FALLBACK = env("GROQ_CHAT_FALLBACK", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = env("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
GROQ_ROUTER_MODEL = env("GROQ_ROUTER_MODEL", "qwen/qwen3.6-27b")
GROQ_STT_MODEL = env("GROQ_STT_MODEL", "whisper-large-v3")
GROQ_STT_FALLBACK = env("GROQ_STT_FALLBACK", "whisper-large-v3-turbo")
REASONING_RESERVE = env_int("REASONING_RESERVE", 1024)

# Local Ollama, for offline development. Used only when GROQ_API_KEY is unset.
OLLAMA_MODEL = env("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_VISION_MODEL = env("VISION_MODEL", "qwen2.5vl:7b")
OLLAMA_HOST = env("OLLAMA_HOST") or None

# ---- Telegram ----------------------------------------------------------------------------
TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN") or None
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME").lstrip("@")
# Telegram signs nothing, so the webhook path itself is the secret: we register
# /telegram/<TELEGRAM_WEBHOOK_SECRET> with Telegram and reject every other path.
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET")

# ---- cloud snapshot of the DB ------------------------------------------------------------
# The local SQLite file stays the only database we read and write (a remote DB in the reply
# path makes every message crawl). A background job snapshots the whole file to Turso, and a
# fresh boot with no local file pulls it back. Free hosts wipe their disk on every deploy.
TURSO_URL = env("TURSO_DATABASE_URL") or None
TURSO_TOKEN = env("TURSO_AUTH_TOKEN") or None
DB_BACKUP_MIN = env_int("DB_BACKUP_MINUTES", 10)

# ---- website -----------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", "dev-only-insecure-key-change-me")
SESSION_DAYS = env_int("SESSION_DAYS", 30)
# Signups are invite-only on purpose: one shared free Groq key serves everyone, so the number
# of active users is the thing that decides whether the app feels fast or rate-limited.
INVITE_CODES = [c.strip() for c in env("INVITE_CODES").split(",") if c.strip()]
INVITE_USES = env_int("INVITE_USES", 5)
MAX_USERS = env_int("MAX_USERS", 25)

# ---- defaults for a brand-new account ----------------------------------------------------
# These are STARTING POINTS shown in the onboarding form, not anybody's real numbers.
DEFAULT_TIMEZONE = env("DEFAULT_TIMEZONE", "UTC")
DEFAULT_STEP_GOAL = env_int("DEFAULT_STEP_GOAL", 8000)
DEFAULT_WATER_GOAL_ML = env_int("DEFAULT_WATER_GOAL_ML", 2500)

CORRECTION_WINDOW_MIN = env_int("CORRECTION_WINDOW_MIN", 30)
MIN_REMINDER_GAP_MIN = env_int("MIN_REMINDER_GAP_MIN", 4)
PHOTO_SETTLE_SEC = env_float("PHOTO_SETTLE_SEC", 8)
PHOTO_QUEUE_MAX = env_int("PHOTO_QUEUE_MAX", 30)
PLAN_APPEND_MIN = env_int("PLAN_APPEND_MIN", 20)
MAX_LEARNED = env_int("MAX_LEARNED", 40)
MAX_TURNS = env_int("MAX_TURNS", 12)


def tz(name: str | None):
    """A ZoneInfo for a user's timezone, falling back to the app default and then UTC.
    Never raises: a bad timezone string in one user's row must not break the scheduler
    sweep for everybody else."""
    for candidate in (name, DEFAULT_TIMEZONE, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return None
