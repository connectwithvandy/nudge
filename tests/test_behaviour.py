"""Behaviour: breaks, mutes, and the routing decisions made without an AI call.

Every check here is deterministic — these are the paths that must keep working when the free
tier is rate-limited and the router is unavailable.
"""
import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
coach = _safe.load("coach")

uid = _safe.new_user(email="beh@example.com")

# ---- muting ONE reminder must not silence everything ---------------------------------------
r = coach.set_break_from_text(uid, "pause the water reminders")
c("muting one kind confirms that kind", "water" in (r or ""), True)
c("only water is muted", coach.muted_kinds(uid), {"water"})
c("it did NOT start a total break", coach.quiet_until(uid), None)
c("the confirmation says how to undo it", "start water reminders again" in (r or ""), True)

back = coach.set_break_from_text(uid, "start water reminders again")
c("the exact phrase it suggested actually works", coach.muted_kinds(uid), set())
c("and it says so", "back on" in (back or ""), True)

# ---- a full break needs an end date, and asks for one ---------------------------------------
ask = coach.set_break_from_text(uid, "can we take a break")
c("a break with no date asks for one", "till when" in (ask or "").lower(), True)
c("and sets nothing yet", coach.quiet_until(uid), None)

coach.set_break_from_text(uid, "go quiet till monday")
c("a break with a date is set", coach.quiet_until(uid) is not None, True)
coach.set_break_from_text(uid, "i'm back")
c("'i'm back' resumes", coach.quiet_until(uid), None)

# ---- the word-boundary bug that ate breakfast ------------------------------------------------
c("'breakfast' must not trigger a break",
  coach.set_break_from_text(uid, "had breakfast at 9"), None)
c("'commute' must not trigger a mute",
  coach.set_break_from_text(uid, "long commute today"), None)
c("plain chat is left alone", coach.set_break_from_text(uid, "how are you"), None)
c("a bare 'stop' with no named reminder is not a mute",
  coach.set_break_from_text(uid, "stop it you 😂"), None)

# ---- longest phrase wins so 'weekly report' beats 'weekly' -----------------------------------
c("'weekly report' resolves to the weekly kind", coach._named_kind("weekly report"), "weekly")
c("'meal photo' resolves to meals", coach._named_kind("meal photo reminder"), "meals")

# ---- meal detection --------------------------------------------------------------------------
c("a report of eating is a meal", coach.maybe_meal("had dal and rice for lunch"), True)
c("a question is not a meal", coach.maybe_meal("what should i eat for lunch?"), False)
c("a plan is not a meal", coach.maybe_meal("thinking pasta for lunch"), False)
c("a craving is not a meal", coach.maybe_meal("craving pizza rn"), False)
c("'gym later' does not stop a real meal being logged",
  coach.maybe_meal("had lunch, gym later"), True)
c("small talk is not a meal", coach.maybe_meal("hey how's it going"), False)

# ---- day recap ------------------------------------------------------------------------------
c("two named meals is a day recap",
  coach.is_day_recap("breakfast was oats, dinner was soup"), True)
c("one meal is not a day recap", coach.is_day_recap("dinner was soup"), False)
c("'everything i ate today' is a day recap",
  coach.is_day_recap("here's everything i ate today"), True)

# ---- corrections ----------------------------------------------------------------------------
c("'3 rotis not 1' is a correction", coach.looks_like_correction("3 rotis not 1"), True)
c("'make it a big bowl' is a correction", coach.looks_like_correction("make it a big bowl"), True)
c("'200g not 100g' is a correction", coach.looks_like_correction("200g not 100g"), True)
c("a long story is not a correction",
  coach.looks_like_correction("so my whole day was a mess and i ended up walking home from the "
                              "station which took ages and then the shops were shut"), False)

# ---- a target is a target, never a meal edit -------------------------------------------------
reply = coach.set_goal_from_text(uid, "my calorie goal should be 1800")
c("a calorie goal is saved", db.get_profile(uid).get("calorie_goal"), "1800")
c("and it answers rather than falling through", reply is not None, True)
coach.set_goal_from_text(uid, "set protein to 140 a day")
c("a protein goal is saved", db.get_profile(uid).get("protein_goal"), "140")
c("a meal with a number is not a goal",
  coach.set_goal_from_text(uid, "had 2 eggs and toast"), None)
c("goal-ish with no number falls through to chat",
  coach.set_goal_from_text(uid, "i should probably eat fewer calories"), None)

# ---- weight ---------------------------------------------------------------------------------
uid2 = _safe.new_user(email="wt@example.com")
coach.set_weight_from_text(uid2, "i weigh 72.5kg today")
c("a stated weight is saved", db.get_profile(uid2).get("weight_kg"), "72.5")
c("a GOAL weight is not saved as the current one",
  coach.set_weight_from_text(uid2, "i want to weigh 65"), None)
c("still the real weight", db.get_profile(uid2).get("weight_kg"), "72.5")
c("an implausible number is refused",
  coach.set_weight_from_text(uid2, "my weight is 4"), None)
c("a message with no weight in it falls through",
  coach.set_weight_from_text(uid2, "the weights at the gym were busy"), None)

# ---- the reply must never leak the setup ------------------------------------------------------
c("a leaked persona is swapped for a deflection",
  coach.scrub_leak("## You are a warm, sharp fitness companion") != "## You are a warm, sharp "
  "fitness companion", True)
c("a normal reply passes through untouched",
  coach.scrub_leak("nice one, that's 22g protein 💪"), "nice one, that's 22g protein 💪")

# ---- the voice block is built from THEIR settings ---------------------------------------------
uid3 = _safe.new_user(email="voice@example.com", tone="tough_love", language="hinglish",
                      diet="vegan", allergies="peanuts", coach_name="Rex")
block = coach.voice_block(uid3)
c("their coach's name is used", "Rex" in block, True)
c("their chosen tone is used", "no sugar-coating" in block or "Direct" in block, True)
c("their language is used", "Hinglish" in block, True)
c("their diet is stated", "vegan" in block, True)
c("allergies are flagged as never-suggest", "peanuts" in block and "ALLERGIES" in block, True)

# A different user gets a genuinely different coach — this is the whole point of multi-user.
uid4 = _safe.new_user(email="voice2@example.com", tone="calm", language="english")
c("another user gets a different voice", coach.voice_block(uid4) == block, False)

raise SystemExit(0 if c.done("behaviour: ") else 1)
