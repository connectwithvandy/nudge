"""Accounts: passwords, sessions, invites, per-user isolation, and calculated targets."""
import _safe

c = _safe.Check()
accounts = _safe.load("accounts")
db = _safe.load("db")
db.connect().close()
db.seed_invites()

# ---- passwords ----
h = accounts.hash_password("correct horse")
c("password verifies", accounts.verify_password("correct horse", h), True)
c("wrong password rejected", accounts.verify_password("Correct horse", h), False)
c("two hashes of one password differ (salted)",
  accounts.hash_password("x") == accounts.hash_password("x"), False)
c("garbage hash doesn't crash", accounts.verify_password("x", "nonsense"), False)
c("short password refused", bool(accounts.password_problem("abc")), True)
c("8 chars accepted", accounts.password_problem("abcdefgh"), None)

# ---- invites ----
c("seeded code is valid", accounts.invite_ok("testcode"), True)
c("unknown code refused", accounts.invite_ok("nope"), False)
c("blank code refused", accounts.invite_ok(""), False)
one = accounts.create_invite(max_uses=1)
accounts.spend_invite(one)
c("single-use code is spent after one use", accounts.invite_ok(one), False)

# ---- accounts and sessions ----
u = accounts.create_user("Sam@Example.com ".strip(), "password123", "Sam")
c("email is stored lowercased", accounts.find_by_email("sam@example.com") is not None, True)
c("display name is the first name", accounts.display_name(u["id"]), "Sam")
tok = accounts.start_session(u["id"])
c("session resolves to the user", accounts.session_user(tok)["id"], u["id"])
accounts.end_session(tok)
c("ended session is dead", accounts.session_user(tok), None)
c("garbage token is dead", accounts.session_user("nope"), None)
c("empty token is dead", accounts.session_user(""), None)

# ---- linking ----
code = accounts.find_by_id(u["id"])["link_code"]
c("new account has a link code", bool(code), True)
accounts.set_chat_id(u["id"], "12345")
c("chat id binds to the account", accounts.find_by_chat_id("12345")["id"], u["id"])
c("link code is burned after use", accounts.find_by_link_code(code), None)

# ---- health tokens are per user, never shared ----
u2 = accounts.create_user("b@example.com", "password123", "Alex")
t1, t2 = accounts.health_token(u["id"]), accounts.health_token(u2["id"])
c("health tokens differ between users", t1 == t2, False)
c("token maps back to its owner", accounts.user_by_health_token(t1), u["id"])
c("unknown health token maps to nobody", accounts.user_by_health_token("xxx"), None)
c("blank health token maps to nobody", accounts.user_by_health_token(""), None)
c("token is stable across calls", accounts.health_token(u["id"]), t1)

# ---- calculated targets ----
prof = {"weight_kg": "70", "height_cm": "175", "age": "30", "sex": "male",
        "activity": "moderate", "goal_type": "gentle_fat_loss"}
t = accounts.computed_targets(prof)
# Mifflin-St Jeor: 10*70 + 6.25*175 - 5*30 + 5 = 1648.75; x1.55 = 2555.6; -250 = ~2306
c("calorie target is calculated, not guessed", 2280 <= t["calories"] <= 2330, True)
c("protein scales off bodyweight", t["protein"], round(70 * 1.9))
c("source explains where the number came from", "Mifflin" in t["source"], True)

c("no height means no calorie target",
  accounts.computed_targets({"weight_kg": "70", "age": "30", "sex": "male"})["calories"], None)
c("no inputs at all is safe",
  accounts.computed_targets({})["calories"], None)

# A deficit must never be allowed to reach a dangerous number.
tiny = accounts.computed_targets({"weight_kg": "45", "height_cm": "150", "age": "60",
                                  "sex": "female", "activity": "sedentary",
                                  "goal_type": "fat_loss"})
c("a calorie floor stops a crash deficit", tiny["calories"] >= 1200, True)

# ---- an explicit override always wins over the formula ----
uid = _safe.new_user(email="c@example.com", weight_kg="70", height_cm="175", age="30",
                     sex="male", activity="moderate", goal_type="gentle_fat_loss")
c("calculated target is used when nothing is set", 2280 <= accounts.calorie_goal(uid) <= 2330,
  True)
db.set_profile(uid, "calorie_goal", "1800")
c("their own number beats the formula", accounts.calorie_goal(uid), 1800)
db.set_profile(uid, "protein_goal", "140")
c("their own protein beats the formula", accounts.protein_goal(uid), 140)

# ---- deleting an account really deletes it ----
gone = accounts.create_user("gone@example.com", "password123", "Gone")
db.set_profile(gone["id"], "weight_kg", "80")
accounts.delete_user(gone["id"])
c("account is gone", accounts.find_by_email("gone@example.com"), None)
c("their profile rows are gone", db.get_profile(gone["id"]), {})

raise SystemExit(0 if c.done("accounts: ") else 1)
