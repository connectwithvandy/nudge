"""Shared setup for every test here. Import this FIRST, before importing anything from app/.

The same code that runs these tests can reach a real Telegram bot, a real Groq budget, and the
Turso snapshot that IS everybody's memory. Two accidents are one careless import away: a
scheduled job firing real messages at real users, and shutdown pushing a stale local database
over the live snapshot.

So every test starts by blanking those secrets, and then ASSERTS they are gone. python-dotenv
does not override variables that are already set, so blanking them here beats .env.
"""
import os
import sys
import tempfile

for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_BOT_USERNAME",
           "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "GROQ_API_KEY",
           "PUBLIC_URL", "RENDER_EXTERNAL_URL"):
    os.environ[_k] = ""
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["MEDIA_DIR"] = tempfile.mkdtemp()
os.environ["DEFAULT_TIMEZONE"] = "UTC"
os.environ["INVITE_CODES"] = "testcode"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(module: str):
    """Import an app module with every outbound path disarmed."""
    import importlib
    from app import config
    assert config.TURSO_URL is None, "SECRET LEAK: Turso is reachable from a test — abort"
    assert config.TELEGRAM_TOKEN is None, "SECRET LEAK: Telegram is reachable — abort"
    assert config.GROQ_API_KEY is None, "SECRET LEAK: Groq is reachable — abort"
    assert "test.db" in config.DB_PATH, f"tests must not touch {config.DB_PATH}"
    return importlib.import_module(f"app.{module}")


def new_user(email="a@b.com", name="Sam", **profile) -> str:
    """Create a signed-up, onboarded user and return their id."""
    accounts = load("accounts")
    db = load("db")
    u = accounts.find_by_email(email) or accounts.create_user(email, "password123", name)
    accounts.mark_onboarded(u["id"])
    accounts.set_chat_id(u["id"], f"chat-{u['id']}")
    defaults = {"timezone": "UTC", "tone": "warm_friend", "language": "english"}
    for k, v in {**defaults, **profile}.items():
        db.set_profile(u["id"], k, str(v))
    return u["id"]


class Check:
    """A tiny assert helper, so a failure prints what differed instead of a bare traceback."""

    def __init__(self):
        self.ok = self.bad = 0

    def __call__(self, name, got, want):
        if got == want:
            self.ok += 1
            print(f"  ok   {name}")
        else:
            self.bad += 1
            print(f"  FAIL {name}: got {got!r} want {want!r}")

    def true(self, name, got):
        self(name, bool(got), True)

    def done(self, title=""):
        print(f"\n{title}{self.ok} passed, {self.bad} failed")
        return self.bad == 0
