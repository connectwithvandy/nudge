"""Reminders: per-user times, timezones, day matching, and the guards that stop a burst.

Nothing here sends anything — messenger.send_reminder is swapped for a recorder, so a bug in
this file can never turn into a message at a real person.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
accounts = _safe.load("accounts")
coach = _safe.load("coach")
messenger = _safe.load("messenger")
reminders = _safe.load("reminders")

# Every outbound path is replaced before a single sweep runs.
SENT = []
messenger.send_reminder = lambda user, text, kind="": SENT.append((user, kind)) or True
messenger.send = lambda user, text: SENT.append((user, "direct")) or True
coach.say_in_voice = lambda user, situation, knowledge=False: "(voice)"

# ---- time parsing ---------------------------------------------------------------------------
c("HH:MM parses", reminders._parse_hhmm("08:30"), (8, 30))
c("a bare hour parses", reminders._parse_hhmm("9"), (9, 0))
c("nonsense is refused", reminders._parse_hhmm("nope"), None)
c("an impossible hour is refused", reminders._parse_hhmm("29:00"), None)
c("blank is refused", reminders._parse_hhmm(""), None)

c("labelled times parse", reminders._times("13:00=lunch, 20:00=dinner"),
  [(13, 0, "lunch"), (20, 0, "dinner")])
c("unlabelled times parse", reminders._times("13:15,19:15"), [(13, 15, ""), (19, 15, "")])
c("an empty list is empty", reminders._times(""), [])
c("a malformed entry is skipped, not guessed",
  reminders._times("13:00=lunch, banana, 20:00=dinner"),
  [(13, 0, "lunch"), (20, 0, "dinner")])

# ---- day matching ---------------------------------------------------------------------------
mon = datetime(2026, 8, 24)   # a Monday
sat = datetime(2026, 8, 29)   # a Saturday
c("mon-fri includes Monday", reminders.dow_matches("mon-fri", mon), True)
c("mon-fri excludes Saturday", reminders.dow_matches("mon-fri", sat), False)
c("a list matches", reminders.dow_matches("mon,wed,fri", mon), True)
c("a list excludes", reminders.dow_matches("tue,thu", mon), False)
c("blank means every day", reminders.dow_matches("", sat), True)
c("a wrapping range works", reminders.dow_matches("sat-sun", sat), True)

# ---- supplements are free text, and a bad line is skipped ------------------------------------
supps = reminders.parse_supplements(
    "Vitamin D 1000IU | 09:00 | mon,thu\nCreatine 5g | 08:00\nbroken line\n\n")
c("two good lines parse", len(supps), 2)
c("the name is kept verbatim", supps[0][0], "Vitamin D 1000IU")
c("the time parses", supps[0][1], (9, 0))
c("days are kept", supps[0][2], "mon,thu")
c("a line with no days means daily", supps[1][2], "")
c("no supplements is empty", reminders.parse_supplements(""), [])

# ---- a reminder fires once, and only once ----------------------------------------------------
uid = _safe.new_user(email="rem@example.com", timezone="UTC",
                     reminders_on="gym,summary", gym_time="08:30", gym_days="mon-fri",
                     summary_time="21:30")
now = datetime(2026, 8, 24, 8, 30, tzinfo=ZoneInfo("UTC"))
c("due at the target minute", reminders._due(uid, now, "gym", 8, 30), True)
c("not due a second time the same day", reminders._due(uid, now, "gym", 8, 30), False)
c("a delayed sweep inside the grace window still fires",
  reminders._due(uid, datetime(2026, 8, 24, 9, 2, tzinfo=ZoneInfo("UTC")), "late", 9, 0), True)
c("but not hours later",
  reminders._due(uid, datetime(2026, 8, 24, 14, 0, tzinfo=ZoneInfo("UTC")), "old", 9, 0), False)
c("not due before the time",
  reminders._due(uid, datetime(2026, 8, 24, 7, 0, tzinfo=ZoneInfo("UTC")), "early", 8, 30),
  False)

# ---- two users, two timezones, one clock ------------------------------------------------------
# The same instant is 08:30 for one of them and not for the other. This is the whole reason the
# sweep asks each user what time it is where THEY are.
tokyo = _safe.new_user(email="tokyo@example.com", timezone="Asia/Tokyo",
                       reminders_on="gym", gym_time="08:30", gym_days="")
london = _safe.new_user(email="london@example.com", timezone="Europe/London",
                        reminders_on="gym", gym_time="08:30", gym_days="")
c("Tokyo and London are different zones",
  accounts.now_local(tokyo).hour == accounts.now_local(london).hour, False)

# ---- the guards --------------------------------------------------------------------------------
SENT.clear()
quiet = _safe.new_user(email="quiet@example.com", timezone="UTC", reminders_on="water",
                       water_times="00:00,01:00,02:00,03:00,04:00,05:00,06:00,07:00,08:00,"
                                   "09:00,10:00,11:00,12:00,13:00,14:00,15:00,16:00,17:00,"
                                   "18:00,19:00,20:00,21:00,22:00,23:00")
coach.set_break_from_text(quiet, "go quiet for 3 days")
reminders._sweep_user(quiet)
c("a user on a break gets nothing at all", SENT, [])

SENT.clear()
muted = _safe.new_user(email="muted@example.com", timezone="UTC", reminders_on="water,summary")
coach.set_muted(muted, {"water"})
c("a muted kind is not in the live set",
  "water" in (reminders.enabled_kinds(db.get_profile(muted)) - coach.muted_kinds(muted)), False)
c("an unmuted kind still is",
  "summary" in (reminders.enabled_kinds(db.get_profile(muted)) - coach.muted_kinds(muted)), True)

SENT.clear()
off = _safe.new_user(email="off@example.com", timezone="UTC", reminders_on="")
reminders._sweep_user(off)
c("a user with no reminders enabled gets nothing", SENT, [])

# ---- the sweep survives one broken user --------------------------------------------------------
broken = _safe.new_user(email="broken@example.com", timezone="Not/AZone",
                        reminders_on="gym", gym_time="whenever")
try:
    reminders.sweep()
    c("a bad timezone and a bad time don't stop the sweep", True, True)
except Exception as ex:
    c("a bad timezone and a bad time don't stop the sweep", repr(ex), True)

raise SystemExit(0 if c.done("reminders: ") else 1)
