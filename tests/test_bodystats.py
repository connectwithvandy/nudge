"""Body composition: the scan-to-scan rule, and the percent-into-a-kg-field guard.

These are the two mistakes that produce a WRONG number rather than a missing one, which is
why they get a suite of their own. A missing comparison is visible; a comparison against the
wrong date reads perfectly plausible and is believed.
"""
import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
nutrition = _safe.load("nutrition")
bodystats = _safe.load("bodystats")

uid = _safe.new_user(email="scan@example.com")


def scan(day: str, **metrics):
    """Record a scale screenshot on a specific day (bypassing 'today' so we can build history)."""
    with db.connect() as conn:
        for k, v in metrics.items():
            conn.execute(
                "INSERT INTO health_metrics (user, ts, day, metric, value) VALUES (?,?,?,?,?)",
                (uid, f"{day}T09:00:00", day, k, v))


def synced(day: str, **metrics):
    """A weight that arrived from a phone or band — NOT a scan the person chose to do."""
    scan(day, **metrics)


# ---- the exact failure this rule exists to prevent -----------------------------------------
# Scans 13 days apart, with an older one in between. A fixed 7-day window reaches PAST the
# reading it should use and reports the change against the wrong baseline — with the right
# sign, so it looks entirely believable.
scan("2026-08-05", weight_kg=62.7, muscle_mass_kg=41.5, bmi=23.0)
scan("2026-08-18", weight_kg=60.4, muscle_mass_kg=41.7, bmi=22.2)

w, change, day, gap = bodystats.scan_trend(uid, "weight_kg")
c("latest weight is the newest scan", w, 60.4)
c("compared against the PREVIOUS SCAN, not 7 days ago", change, -2.3)
c("the gap it measured is reported", gap, 13)
c("the date of the reading is reported", day, "2026-08-18")

# ---- a SHORT gap must still produce a comparison -------------------------------------------
# 6 days apart. A 7-day window returns nothing here, and a missing change looks exactly like
# no change.
uid2 = _safe.new_user(email="short@example.com")
with db.connect() as conn:
    for day_, wt in (("2026-08-18", 60.4), ("2026-08-24", 60.1)):
        conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                     (uid2, f"{day_}T09:00:00", day_, "weight_kg", wt))
        conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                     (uid2, f"{day_}T09:00:00", day_, "bmi", 22.0))
w2, change2, _, gap2 = bodystats.scan_trend(uid2, "weight_kg")
c("a 6-day gap still compares", change2, -0.3)
c("and names the 6 days", gap2, 6)

# ---- a synced weight must never become the baseline a screenshot is judged against ----------
uid3 = _safe.new_user(email="sync@example.com")
with db.connect() as conn:
    # A scan on the 1st, then a phone-synced weight on the 10th, then a scan on the 12th.
    conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                 (uid3, "2026-08-01T09:00:00", "2026-08-01", "weight_kg", 70.0))
    conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                 (uid3, "2026-08-01T09:00:00", "2026-08-01", "bmi", 24.0))
    conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                 (uid3, "2026-08-10T09:00:00", "2026-08-10", "weight_kg", 69.5))
    conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                 (uid3, "2026-08-12T09:00:00", "2026-08-12", "weight_kg", 69.0))
    conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                 (uid3, "2026-08-12T09:00:00", "2026-08-12", "bmi", 23.6))
_, change3, _, gap3 = bodystats.scan_trend(uid3, "weight_kg")
c("the synced day is not treated as a scan", change3, -1.0)
c("so the gap is scan to scan (11 days), not 2", gap3, 11)

# ---- a single scan compares to nothing, and says so ----------------------------------------
uid4 = _safe.new_user(email="first@example.com")
scan_day = "2026-08-20"
with db.connect() as conn:
    for k, v in (("weight_kg", 80.0), ("bmi", 25.0)):
        conn.execute("INSERT INTO health_metrics (user,ts,day,metric,value) VALUES (?,?,?,?,?)",
                     (uid4, f"{scan_day}T09:00:00", scan_day, k, v))
val4, change4, _, gap4 = bodystats.scan_trend(uid4, "weight_kg")
c("a first scan has a value", val4, 80.0)
c("but no change", change4, None)
c("and no gap", gap4, None)
c("the reply says it's the first one on file",
  "first scan on file" in bodystats.compare_block(uid4), True)
c("no scans at all produces nothing rather than a broken block",
  bodystats.compare_block(_safe.new_user(email="none@example.com")), "")

# ---- the comparison block names the gap out loud -------------------------------------------
block = bodystats.compare_block(uid)
c("the block names the gap it measured", "13 days ago" in block, True)
c("and shows the direction", "↓" in block, True)
c("muscle that barely moved reads as held", "held" in block, True)

# ---- a percentage must never be stored into a kilogram field -------------------------------
parsed = nutrition.parse_body_stats(
    "WEIGHT_KG: 70.2\nBONE_MASS_KG: 4.3%\nMUSCLE_MASS_KG: 41.7\nBODY_FAT: 22.1%")
c("a % value is refused for a *_kg field", "bone_mass_kg" in parsed, False)
c("a real kg value is kept", parsed["muscle_mass_kg"], 41.7)
c("a % value is fine for a % field", parsed["body_fat"], 22.1)
c("plain numbers are read", parsed["weight_kg"], 70.2)
c("an empty reply parses to nothing", nutrition.parse_body_stats(""), {})

raise SystemExit(0 if c.done("body stats: ") else 1)
