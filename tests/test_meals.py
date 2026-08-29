"""Meals: logging, day totals, corrections, and — above all — one user's data staying theirs."""
import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
nutrition = _safe.load("nutrition")
coach = _safe.load("coach")
accounts = _safe.load("accounts")

a = _safe.new_user(email="a1@example.com", name="Ann", timezone="UTC")
b = _safe.new_user(email="b1@example.com", name="Ben", timezone="UTC")

MEAL = {"calories": 420, "protein": 16, "carbs": 60, "fat": 11, "fiber": 7,
        "sugar": 3, "sodium": 480, "iron": 3, "calcium": 90}

mid = nutrition.log_meal(a, "2 rotis, dal", MEAL, None)
c("a meal gets an id", isinstance(mid, int), True)
n, tot = nutrition.day_totals(a)
c("it counts toward today", n, 1)
c("calories are totalled", tot["calories"], 420)
c("micros are totalled too", tot["sodium"], 480)

nutrition.log_meal(a, "yoghurt", {**MEAL, "calories": 150, "protein": 12}, None)
n, tot = nutrition.day_totals(a)
c("a second meal adds up", n, 2)
c("calories accumulate", tot["calories"], 570)
c("protein accumulates", tot["protein"], 28)

# ---- the isolation that matters most -------------------------------------------------------
n_b, tot_b = nutrition.day_totals(b)
c("another user's day is untouched", n_b, 0)
c("and their totals are zero", tot_b["calories"], 0)
nutrition.log_meal(b, "toast", {**MEAL, "calories": 200}, None)
c("their meal is theirs alone", nutrition.day_totals(b)[1]["calories"], 200)
c("and hasn't leaked into the first user", nutrition.day_totals(a)[1]["calories"], 570)

# ---- corrections ------------------------------------------------------------------------------
nutrition.remember_meal(a, mid, "2 rotis, dal", MEAL)
recent = nutrition.recent_meal(a)
c("the last meal is recoverable for a correction", recent["id"], mid)
c("with its items", recent["items"], "2 rotis, dal")

nutrition.update_meal(mid, "4 rotis, dal", {**MEAL, "calories": 700, "protein": 22})
n, tot = nutrition.day_totals(a)
c("a correction REPLACES the meal rather than adding one", n, 2)
c("and the day total reflects the fix", tot["calories"], 850)

# The in-memory cache is wiped by every redeploy. Without a database fallback, a correction
# sent after a restart is not recognised as one — it gets logged as a brand-new meal, silently
# inflating the day.
nutrition.LAST_MEAL.clear()
c("a correction still works after a restart wiped memory",
  nutrition.recent_meal(a) is not None, True)

# ---- an old meal is out of the correction window -----------------------------------------------
old = _safe.new_user(email="old@example.com", timezone="UTC")
with db.connect() as conn:
    conn.execute("INSERT INTO meals (user, ts, day, items, calories) VALUES (?,?,?,?,?)",
                 (old, "2020-01-01T09:00:00", "2020-01-01", "ancient toast", 100))
c("a meal from years ago is not offered as a correction target",
  nutrition.recent_meal(old), None)

# ---- formatting -----------------------------------------------------------------------------------
c("the macro line reads naturally", nutrition.macro_line(MEAL),
  "420 kcal, 16g protein, 60g carbs, 11g fat, 7g fiber")
c("micros are always marked as estimates", nutrition.micro_line(MEAL).startswith("~"), True)
c("a calorie line with a target shows what's left",
  nutrition.calorie_line(2000, 570), "🔥  570 / 2000 kcal  ·  1430 left")
c("going over says over, not a negative number",
  nutrition.calorie_line(500, 570), "🔥  570 / 500 kcal  ·  70 over")
c("no target still shows the number",
  nutrition.calorie_line(None, 570), "🔥  570 kcal")
c("a hit protein goal is celebrated",
  "smashed" in nutrition.protein_line(20, 28), True)
c("and a gap is stated plainly",
  "to go" in nutrition.protein_line(100, 28), True)
c("no protein target still shows the number",
  nutrition.protein_line(None, 28), "💪  28g protein")

# ---- on a structured plan, protein is tracked but never pushed -------------------------------------
plan_user = _safe.new_user(email="plan@example.com", timezone="UTC")
db.set_profile(plan_user, "protein_goal", "120")
nutrition.log_meal(plan_user, "lunch", MEAL, None)
n_p, tot_p = nutrition.day_totals(plan_user)
c("off-plan, the target is shown", "to go" in coach.today_block(plan_user, n_p, tot_p), True)
db.set_profile(plan_user, "current_plan", "Day 1: oats. Day 2: rice.")
block = coach.today_block(plan_user, n_p, tot_p)
c("on a plan, protein is shown as logged, not as a target missed", "to go" in block, False)
c("but the number is still there", "16g protein logged" in block, True)

# ---- meals logged recently -----------------------------------------------------------------------
c("a just-logged meal counts as recent", nutrition.logged_meal_recently(a, 2), True)
c("a user who logged nothing has nothing recent",
  nutrition.logged_meal_recently(_safe.new_user(email="empty@example.com"), 2), False)

raise SystemExit(0 if c.done("meals: ") else 1)
