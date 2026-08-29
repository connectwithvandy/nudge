"""Boot: the snapshot restore that has to run BEFORE anything opens the database.

Free hosts wipe their disk on every deploy. If the restore doesn't run first, the app starts
with an empty database, everybody's history is gone, and nothing in the logs says why — so
this is checked directly rather than assumed.
"""
import os
import sqlite3
import tempfile

import _safe

c = _safe.Check()
db = _safe.load("db")
config = _safe.load("config")
import main  # noqa: E402

# ---- the tables all exist after a cold start -------------------------------------------------
main._startup()
with db.connect() as conn:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("users", "sessions", "invites", "meals", "health_metrics", "profile", "learned"):
    c(f"table {t} exists", t in tables, True)

# ---- every nutrient column is present, including ones added later ------------------------------
with db.connect() as conn:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(meals)")}
missing = [k for k, _, _ in db.NUTRIENTS if k not in cols]
c("every nutrient has a column", missing, [])

# ---- an older database gains new columns on boot rather than breaking ---------------------------
old = os.path.join(tempfile.mkdtemp(), "old.db")
conn = sqlite3.connect(old)
conn.execute("CREATE TABLE meals (id INTEGER PRIMARY KEY, user TEXT, ts TEXT, day TEXT, "
             "items TEXT, photo TEXT, calories INTEGER, protein INTEGER)")
conn.commit()
conn.close()
config.DB_PATH = old
with db.connect() as conn2:
    cols2 = {r[1] for r in conn2.execute("PRAGMA table_info(meals)")}
c("a database missing newer columns is migrated, not rejected", "calcium" in cols2, True)
c("and the old columns survive", "protein" in cols2, True)

# ---- restore must not clobber a live local database ----------------------------------------------
# Local is always the boss: overwriting a running database with an older snapshot would lose
# everything logged since that snapshot was taken.
config.TURSO_URL = "libsql://example.invalid"
calls = []
db._turso_pipeline = lambda reqs: calls.append(reqs) or None
db.restore_from_cloud()
c("restore is skipped when a local database already exists", calls, [])

# ---- with no local file, restore is attempted ------------------------------------------------------
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "absent.db")
db.restore_from_cloud()
c("restore IS attempted when the local file is missing", len(calls), 1)

# ---- with no cloud configured the whole feature is silently off, never an error ---------------------
config.TURSO_URL = None
calls.clear()
db.restore_from_cloud()
db.backup_to_cloud()
c("no cloud configured means no calls and no crash", calls, [])

raise SystemExit(0 if c.done("boot: ") else 1)
