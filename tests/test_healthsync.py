"""Health sync: loose field matching, unit fixes, and per-user day boundaries."""
import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
healthsync = _safe.load("healthsync")
nutrition = _safe.load("nutrition")

# ---- loose field names, because every exporter names things differently -------------------
c("an exact key matches", healthsync.canonical("steps"), "steps")
c("a spaced label matches", healthsync.canonical("Body Fat Percentage"), "body_fat")
c("an alias matches", healthsync.canonical("resting_heart_rate"), "resting_hr")
c("case doesn't matter", healthsync.canonical("STEP_COUNT"), "steps")
c("an unknown field is ignored, not an error", healthsync.canonical("mood_score"), None)
c("a blank field is ignored", healthsync.canonical(""), None)

# ---- flat JSON from a script or automation ------------------------------------------------
flat = healthsync.parse_flat({"steps": 6200, "Sleep": 7.5, "weight": 70.4, "token": "x",
                              "user": "y", "nonsense": 1})
c("known fields are kept", flat["steps"], 6200)
c("aliases are resolved", flat["sleep_hours"], 7.5)
c("the token is never stored as data", "token" in flat, False)
c("unknown fields are dropped", "nonsense" in flat, False)

# Water in litres would otherwise be stored as 2.5ml — nobody logs under 20ml of water.
c("water in litres becomes ml", healthsync.parse_flat({"water": 2.5})["water_ml"], 2500.0)
c("water already in ml is left alone", healthsync.parse_flat({"water_ml": 2500})["water_ml"],
  2500)

# ---- an Apple Health Auto Export bundle ------------------------------------------------------
bundle = {"data": {"metrics": [
    {"name": "step_count", "units": "count",
     "data": [{"date": "2026-08-29 08:00:00", "qty": 2000},
              {"date": "2026-08-29 12:00:00", "qty": 3000},
              {"date": "2026-08-28 12:00:00", "qty": 9999}]},
    {"name": "weight_body_mass", "units": "kg",
     "data": [{"date": "2026-08-29 07:00:00", "qty": 70.1},
              {"date": "2026-08-29 21:00:00", "qty": 70.4}]},
    {"name": "dietary_water", "units": "L",
     "data": [{"date": "2026-08-29 10:00:00", "qty": 1.2}]},
    {"name": "not_a_metric", "data": [{"date": "2026-08-29", "qty": 5}]},
]}}
parsed = healthsync.parse_auto_export(bundle, "2026-08-29")
c("cumulative metrics are summed across today", parsed["steps"], 5000.0)
c("yesterday's readings are not counted", parsed["steps"] != 14999.0, True)
c("point-in-time metrics take the LATEST reading", parsed["weight_kg"], 70.4)
c("litres are converted to ml", parsed["water_ml"], 1200.0)
c("unknown metrics are skipped", "not_a_metric" in parsed, False)
c("a payload that isn't a bundle returns nothing so flat parsing takes over",
  healthsync.parse_auto_export({"steps": 10}, "2026-08-29"), {})

# ---- the day a reading lands on is the USER'S day, not the server's -----------------------------
# A server in UTC would roll a Tokyo user's day over in the middle of their evening, and their
# dinner would be logged against tomorrow.
tokyo = _safe.new_user(email="tk@example.com", timezone="Asia/Tokyo")
utc = _safe.new_user(email="utc@example.com", timezone="UTC")
accounts = _safe.load("accounts")
c("two users in different zones can be on different days",
  isinstance(accounts.today(tokyo), str) and isinstance(accounts.today(utc), str), True)

nutrition.record_health(tokyo, {"steps": 4000})
c("a reading is stored against the user's own day",
  nutrition.health_snapshot(tokyo).get("steps"), 4000.0)
c("and not against anyone else", nutrition.health_snapshot(utc).get("steps"), None)

# ---- a value we can't make a number of is skipped rather than stored as garbage ------------------
nutrition.record_health(utc, {"steps": "lots", "sleep_hours": 7})
snap = nutrition.health_snapshot(utc)
c("an unparseable value is dropped", "steps" in snap, False)
c("but the good value beside it is kept", snap.get("sleep_hours"), 7.0)

raise SystemExit(0 if c.done("health sync: ") else 1)
