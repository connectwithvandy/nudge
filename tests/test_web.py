"""The website end to end: invite-gated sign-up, sign-in, onboarding, connecting, isolation.

Uses FastAPI's TestClient, so it exercises the real routes and the real cookie session — but
never touches the network.

Assertions unescape the HTML before matching: the page escapes apostrophes to &#x27;, so a
literal "isn't" search silently never matches and the test passes for the wrong reason.
"""
from html import unescape

import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

main._startup()
client = TestClient(main.app, follow_redirects=False)

# ---- the landing page ---------------------------------------------------------------------
r = client.get("/")
c("the landing page loads", r.status_code, 200)
c("it explains the invite gate", "invite-only" in r.text, True)
c("signed-out visitors are not shown the app", "/signup" in r.text, True)

# ---- sign-up is gated on a valid invite -----------------------------------------------------
bad = client.post("/signup", data={"name": "Sam", "email": "sam@example.com",
                                   "password": "password123", "invite": "wrong"})
c("a bad invite code is refused", "isn't valid" in unescape(bad.text), True)
c("and no account was created", _safe.load("accounts").find_by_email("sam@example.com"), None)

short = client.post("/signup", data={"name": "Sam", "email": "sam@example.com",
                                     "password": "short", "invite": "testcode"})
c("a short password is refused", "8 characters" in short.text, True)

nope = client.post("/signup", data={"name": "Sam", "email": "not-an-email",
                                    "password": "password123", "invite": "testcode"})
c("a malformed email is refused", "email address" in nope.text, True)

ok = client.post("/signup", data={"name": "Sam Lee", "email": "sam@example.com",
                                  "password": "password123", "invite": "testcode"})
c("a good sign-up redirects to onboarding", ok.headers.get("location"), "/onboarding")
c("and sets a session cookie", "nudge_session" in ok.cookies or
  any("nudge_session" in v for v in ok.headers.get_list("set-cookie")), True)

dup = client.post("/signup", data={"name": "Sam", "email": "sam@example.com",
                                   "password": "password123", "invite": "testcode"})
c("the same email can't sign up twice", "already an account" in dup.text, True)

# ---- signed out, everything private redirects to sign-in -------------------------------------
anon = TestClient(main.app, follow_redirects=False)
for path in ("/app", "/settings", "/connect", "/onboarding"):
    c(f"{path} is private", anon.get(path).headers.get("location"), "/login")

# ---- onboarding writes every answer -----------------------------------------------------------
form = {
    "sex": "female", "age": "31", "height_cm": "165", "weight_kg": "68",
    "timezone": "Europe/London", "goal_type": "recomp", "goal_weight_kg": "62",
    "activity": "active", "training": "strength 4x a week",
    "diet": "vegetarian", "cuisine": "Mediterranean", "allergies": "peanuts",
    "dislikes": "olives", "calorie_goal": "", "protein_goal": "",
    "step_goal": "9000", "water_goal_ml": "3000",
    "coach_name": "Robin", "tone": "calm", "language": "english",
    "reminders_on": ["gym", "summary"], "gym_time": "07:00", "gym_days": "mon,wed,fri",
    "meal_times": "13:00=lunch", "water_times": "14:00", "supplements": "Vit D | 09:00",
    "step_time": "18:00", "winddown_time": "22:00", "summary_time": "21:00",
    "weekly_day": "sun", "weekly_time": "10:00",
}
saved = client.post("/onboarding", data=form)
c("onboarding sends them to connect Telegram", saved.headers.get("location"), "/connect")

accounts = _safe.load("accounts")
user = accounts.find_by_email("sam@example.com")
prof = db.get_profile(user["id"])
c("the account is marked onboarded", accounts.find_by_id(user["id"])["onboarded"], True)
c("their timezone is stored", prof["timezone"], "Europe/London")
c("their goal is stored", prof["goal_type"], "recomp")
c("their coach's name is stored", prof["coach_name"], "Robin")
c("their allergy is stored", prof["allergies"], "peanuts")
c("only the ticked reminders are on", prof["reminders_on"], "gym,summary")
c("their step goal is stored", accounts.step_goal(user["id"]), 9000)
c("a blank calorie target falls back to the calculation",
  accounts.calorie_goal(user["id"]) is not None, True)
c("the weight typed in onboarding seeds their body history",
  _safe.load("nutrition").latest_metric(user["id"], "weight_kg"), 68.0)

bad_tz = client.post("/settings", data={**form, "timezone": "Mars/Olympus"})
c("an unknown timezone is refused",
  "timezone isn't one i recognise" in unescape(bad_tz.text).lower(), True)
bad_num = client.post("/settings", data={**form, "age": "thirty"})
c("a non-numeric age is refused", "isn't a number" in unescape(bad_num.text), True)

# ---- the connect page hands out a working link -------------------------------------------------
conn = client.get("/connect")
c("the connect page loads", conn.status_code, 200)
code = accounts.find_by_id(user["id"])["link_code"]
c("it shows their personal /start command", f"/start {code}" in conn.text, True)
c("it explains the optional health sync", "Health Auto Export" in conn.text, True)
c("their health sync URL is personal to them",
  accounts.health_token(user["id"]) in conn.text, True)

# ---- sign in and out ----------------------------------------------------------------------------
fresh = TestClient(main.app, follow_redirects=False)
wrong = fresh.post("/login", data={"email": "sam@example.com", "password": "nope"})
c("a wrong password is refused", "don't match" in unescape(wrong.text), True)
c("an unknown email gives the SAME message (no account enumeration)",
  "don't match" in unescape(fresh.post("/login", data={"email": "who@example.com",
                                                       "password": "x"}).text), True)
good = fresh.post("/login", data={"email": "sam@example.com", "password": "password123"})
c("a correct password signs them in", good.headers.get("location"), "/app")
c("the home page loads once signed in", fresh.get("/app").status_code, 200)
fresh.post("/logout")
c("signing out ends the session", fresh.get("/app").headers.get("location"), "/login")

# ---- one user can never see another's data -------------------------------------------------------
second = TestClient(main.app, follow_redirects=False)
second.post("/signup", data={"name": "Alex", "email": "alex@example.com",
                             "password": "password123", "invite": "testcode"})
second.post("/onboarding", data={**form, "coach_name": "Coach A", "weight_kg": "80"})
alex = accounts.find_by_email("alex@example.com")
c("the second account is a different id", alex["id"] == user["id"], False)
c("their coach names are separate", db.get_profile(alex["id"])["coach_name"], "Coach A")
c("Sam's coach name is untouched", db.get_profile(user["id"])["coach_name"], "Robin")
c("their weights are separate",
  _safe.load("nutrition").latest_metric(alex["id"], "weight_kg"), 80.0)
c("Sam's weight is untouched",
  _safe.load("nutrition").latest_metric(user["id"], "weight_kg"), 68.0)

# ---- the health endpoint identifies the user by THEIR token ----------------------------------------
sam_token = accounts.health_token(user["id"])
r = client.post("/health", json={"token": sam_token, "steps": 6200, "sleep_hours": 7.5})
c("a valid token is accepted", r.json()["ok"], True)
c("the steps land on the right user",
  _safe.load("nutrition").health_snapshot(user["id"]).get("steps"), 6200.0)
c("and NOT on the other user",
  "steps" in _safe.load("nutrition").health_snapshot(alex["id"]), False)
c("a bad token is rejected",
  client.post("/health", json={"token": "nope", "steps": 1}).status_code, 401)
c("a missing token is rejected — it never falls back to 'some user'",
  client.post("/health", json={"steps": 1}).status_code, 401)
c("water sent in litres is stored as ml",
  client.post("/health", json={"token": sam_token, "water": 2.5}).json()["ok"], True)
c("2.5 litres became 2500ml",
  _safe.load("nutrition").health_snapshot(user["id"]).get("water_ml"), 2500.0)

# ---- the Telegram webhook refuses anything without the shared secret ---------------------------------
c("a wrong webhook secret is refused",
  client.post("/telegram/wrong", json={"message": {"chat": {"id": 1}, "text": "hi"}}).status_code,
  403)

# ---- the heartbeat is bare on purpose ------------------------------------------------------------------
hb = client.get("/healthz").json()
c("the heartbeat reports alive", hb["status"], "alive")
c("and a version, so a deploy is verifiable", "version" in hb, True)
c("and nothing else", sorted(hb.keys()), ["status", "version"])

raise SystemExit(0 if c.done("web: ") else 1)
