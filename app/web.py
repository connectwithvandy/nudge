"""web.py — the website: sign up, answer the questions once, connect Telegram, edit settings.

Server-rendered HTML with no build step and no JavaScript framework, because the whole point
of this app is that it runs for free on a small box — and because a settings form does not
need a bundler.

The onboarding form is the heart of it. A single-user coach can hard-code someone's targets,
gym time and diet; a coach for strangers has to ask. Every answer here replaces something that
used to be a constant in the source.
"""
import html
import re
from zoneinfo import available_timezones

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import accounts, config, db, reminders

COOKIE = "nudge_session"
TIMEZONES = sorted(available_timezones())


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


# ---------- layout ----------

_CSS = """
:root{--bg:#fbfaf8;--card:#fff;--ink:#1b1a19;--muted:#6b6864;--line:#e7e3dd;
--accent:#4f46e5;--accent-ink:#fff;--good:#0f7a4d;--bad:#b3261e;--radius:14px}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1c1c1f;--ink:#f2f0ee;
--muted:#a3a09b;--line:#2e2e33;--accent:#8b85f5;--accent-ink:#16151a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
.wrap{max-width:720px;margin:0 auto;padding:28px 20px 80px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:16px;
padding:18px 20px;max-width:720px;margin:0 auto}
.brand{font-weight:700;font-size:17px;letter-spacing:-.01em;text-decoration:none;color:var(--ink)}
.brand span{color:var(--accent)}
nav a{margin-left:14px;font-size:14px;text-decoration:none;color:var(--muted)}
nav a:hover{color:var(--ink)}
h1{font-size:27px;line-height:1.2;letter-spacing:-.02em;margin:0 0 8px}
h2{font-size:17px;margin:0 0 4px;letter-spacing:-.01em}
p.sub{color:var(--muted);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:20px;margin-bottom:16px}
.card h2{margin-bottom:14px}
.hint{color:var(--muted);font-size:13px;margin:4px 0 0}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 5px}
input[type=text],input[type=email],input[type=password],input[type=number],
input[type=time],select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line);
border-radius:9px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:84px;resize:vertical;font-size:14px}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1 1 150px;min-width:0}
button,.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);border:0;
padding:11px 18px;border-radius:10px;font:inherit;font-weight:600;cursor:pointer;
text-decoration:none;text-align:center}
button:hover,.btn:hover{opacity:.9}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.btn.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
.msg{padding:11px 14px;border-radius:10px;margin-bottom:16px;font-size:14px}
.msg.err{background:rgba(179,38,30,.1);color:var(--bad)}
.msg.ok{background:rgba(15,122,77,.1);color:var(--good)}
.checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;
margin-top:8px}
.checks label{display:flex;align-items:center;gap:8px;font-weight:500;margin:0;
padding:9px 11px;border:1px solid var(--line);border-radius:9px;cursor:pointer}
.checks input{width:auto;margin:0}
code{background:var(--bg);border:1px solid var(--line);padding:2px 6px;border-radius:6px;
font-size:13px;word-break:break-all}
pre{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;
overflow-x:auto;font-size:13px}
.feat{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;
margin:22px 0}
.feat div{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:16px}
.feat b{display:block;margin-bottom:3px}
.feat p{margin:0;color:var(--muted);font-size:14px}
footer{color:var(--muted);font-size:13px;text-align:center;padding:30px 20px}
"""


def page(title: str, body: str, user=None, message=None, kind="ok") -> HTMLResponse:
    nav = ('<nav><a href="/app">Home</a><a href="/settings">Settings</a>'
           '<a href="/connect">Telegram</a>'
           '<form method="post" action="/logout" style="display:inline">'
           '<button class="btn ghost" style="padding:5px 11px;font-size:13px;margin-left:14px">'
           'Sign out</button></form></nav>') if user else (
        '<nav><a href="/login">Sign in</a><a href="/signup">Sign up</a></nav>')
    msg = f'<div class="msg {kind}">{e(message)}</div>' if message else ""
    app_name = e(config.APP_NAME)
    return HTMLResponse(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · {app_name}</title><style>{_CSS}</style></head><body>
<header class="top"><a class="brand" href="/">{app_name[:1]}<span>{app_name[1:]}</span></a>{nav}
</header><div class="wrap">{msg}{body}</div>
<footer>{app_name} · your data is yours — you can delete it any time from Settings.</footer>
</body></html>""")


def current_user(request: Request):
    return accounts.session_user(request.cookies.get(COOKIE, ""))


def _redirect(url: str, code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=code)


# ---------- landing ----------

def landing(user) -> HTMLResponse:
    app_name = e(config.APP_NAME)
    body = f"""
<h1>A fitness coach that actually texts you.</h1>
<p class="sub">{app_name} lives in your Telegram. Send a photo of your dinner and it logs the
macros. Miss a day and it checks in — kindly. It remembers what you eat, what you're working
toward, and when you asked it to leave you alone.</p>
<p><a class="btn" href="/signup">Get started</a>
<a class="btn ghost" href="/login">I have an account</a></p>
<div class="feat">
<div><b>📷 Snap your meal</b><p>Photo, voice note, or just type it. Full macros, logged
against your own targets.</p></div>
<div><b>⚖️ Real trends</b><p>Send your scale screenshot whenever. It compares scan to scan and
always tells you the gap it measured.</p></div>
<div><b>⏰ Your schedule</b><p>Your timezone, your training time, your reminders. Mute any of
them with a message.</p></div>
<div><b>🤐 Never nags</b><p>"Go quiet till Monday" works. So does "stop the water reminders".
Silence is the default when there's nothing useful to say.</p></div>
</div>
<div class="card"><h2>Free, and honest about why</h2>
<p class="hint">The coaching runs on a free AI tier shared by everyone here, so sign-ups are
invite-only — that's what keeps replies fast instead of rate-limited. If you don't have an
invite code, ask whoever sent you this link.</p></div>
"""
    return page("A fitness coach that texts you", body, user)


# ---------- auth ----------

def signup_form(message=None, kind="err", values=None) -> HTMLResponse:
    v = values or {}
    body = f"""
<h1>Create your account</h1>
<p class="sub">Two minutes of questions, then you're connected to Telegram.</p>
<form method="post" class="card">
<label for="name">Your name</label>
<input id="name" name="name" type="text" required value="{e(v.get('name'))}"
 placeholder="What should your coach call you?">
<label for="email">Email</label>
<input id="email" name="email" type="email" required value="{e(v.get('email'))}"
 autocomplete="email">
<label for="password">Password</label>
<input id="password" name="password" type="password" required minlength="8"
 autocomplete="new-password">
<p class="hint">At least 8 characters.</p>
<label for="invite">Invite code</label>
<input id="invite" name="invite" type="text" required value="{e(v.get('invite'))}">
<p class="hint">Sign-ups are invite-only while the shared free AI tier is the thing paying
for your replies.</p>
<p style="margin-top:20px"><button type="submit">Create account</button></p>
</form>
<p class="hint">Already have one? <a href="/login">Sign in</a>.</p>
"""
    return page("Sign up", body, None, message, kind)


def login_form(message=None, kind="err", email="") -> HTMLResponse:
    body = f"""
<h1>Welcome back</h1>
<form method="post" class="card">
<label for="email">Email</label>
<input id="email" name="email" type="email" required value="{e(email)}" autocomplete="email">
<label for="password">Password</label>
<input id="password" name="password" type="password" required autocomplete="current-password">
<p style="margin-top:20px"><button type="submit">Sign in</button></p>
</form>
<p class="hint">No account yet? <a href="/signup">Sign up</a>.</p>
"""
    return page("Sign in", body, None, message, kind)


# ---------- the questionnaire ----------

_GOALS = [("gentle_fat_loss", "Lose fat, gently and sustainably"),
          ("fat_loss", "Lose fat — I want to see it move"),
          ("recomp", "Recomposition — lose fat and build muscle"),
          ("maintain", "Maintain where I am and stay consistent"),
          ("muscle_gain", "Build muscle")]
_ACTIVITY = [("sedentary", "Sedentary — desk job, little exercise"),
             ("light", "Light — 1-3 sessions a week"),
             ("moderate", "Moderate — 3-5 sessions a week"),
             ("active", "Active — 6-7 sessions a week"),
             ("very_active", "Very active — physical job or twice a day")]
_DIETS = [("anything", "I eat anything"), ("vegetarian", "Vegetarian"),
          ("eggetarian", "Vegetarian + eggs"), ("vegan", "Vegan"),
          ("pescatarian", "Pescatarian"), ("halal", "Halal"), ("kosher", "Kosher"),
          ("keto", "Keto / low carb"), ("gluten_free", "Gluten free")]
_TONES = [("warm_friend", "Warm friend — playful, encouraging, a bit cheeky"),
          ("tough_love", "Tough love — direct, high standards, no sugar-coating"),
          ("calm", "Calm and factual — measured, minimal emoji, just the data"),
          ("cheerleader", "Cheerleader — high energy, celebrates everything")]
_LANGS = [("english", "English"), ("hinglish", "Hinglish (Hindi + English mix)"),
          ("spanish", "Spanish"), ("french", "French"), ("german", "German"),
          ("portuguese", "Portuguese"), ("arabic", "Arabic")]
_SEXES = [("female", "Female"), ("male", "Male"), ("other", "Prefer not to say")]
_REMINDERS = [("gym", "Training check-in"), ("meals", "Meal photo nudges"),
              ("water", "Water"), ("supplements", "Supplements"),
              ("steps", "Steps (evening)"), ("winddown", "Wind-down for bed"),
              ("summary", "Daily wrap-up"), ("weekly", "Weekly report")]


def _select(name: str, options, chosen, blank=None) -> str:
    opts = ""
    if blank:
        opts += f'<option value="">{e(blank)}</option>'
    for value, label in options:
        sel = " selected" if str(chosen) == str(value) else ""
        opts += f'<option value="{e(value)}"{sel}>{e(label)}</option>'
    return f'<select id="{e(name)}" name="{e(name)}">{opts}</select>'


def _tz_select(chosen: str) -> str:
    opts = "".join(
        f'<option value="{e(z)}"{" selected" if z == chosen else ""}>{e(z)}</option>'
        for z in TIMEZONES)
    return f'<select id="timezone" name="timezone">{opts}</select>'


def _checks(name: str, options, chosen: set) -> str:
    out = '<div class="checks">'
    for value, label in options:
        ck = " checked" if value in chosen else ""
        out += (f'<label><input type="checkbox" name="{e(name)}" value="{e(value)}"{ck}>'
                f'{e(label)}</label>')
    return out + "</div>"


def _txt(name, label, value, placeholder="", hint="", type_="text", extra="") -> str:
    out = f'<label for="{e(name)}">{e(label)}</label>'
    out += (f'<input id="{e(name)}" name="{e(name)}" type="{type_}" value="{e(value)}" '
            f'placeholder="{e(placeholder)}" {extra}>')
    if hint:
        out += f'<p class="hint">{e(hint)}</p>'
    return out


def questionnaire(user, prof: dict, first_time: bool, message=None, kind="ok") -> HTMLResponse:
    on = reminders.enabled_kinds(prof) if prof.get("reminders_on") is not None else {
        "gym", "meals", "summary", "weekly"}
    targets = accounts.computed_targets(prof)
    auto = ""
    if targets["calories"]:
        auto = (f'<p class="hint">Leave these blank and I\'ll use '
                f'<strong>{targets["calories"]} kcal</strong> and '
                f'<strong>{targets["protein"]}g protein</strong>, calculated from your answers '
                f'above ({e(targets["source"])}).</p>')
    else:
        auto = ('<p class="hint">Fill in your age, height and weight above and I can calculate '
                'these for you — or just set them yourself here.</p>')

    heading = ("Tell your coach about you" if first_time else "Settings")
    intro = ("Everything here shapes how your coach talks to you and when it messages. You can "
             "change any of it later." if first_time else
             "Change anything. Your coach picks it up on the next message.")

    body = f"""
<h1>{e(heading)}</h1>
<p class="sub">{e(intro)}</p>
<form method="post">

<div class="card"><h2>About you</h2>
<div class="row">
<div><label for="sex">Sex</label>{_select("sex", _SEXES, prof.get("sex", "female"))}
<p class="hint">Used only for the metabolic-rate formula.</p></div>
<div>{_txt("age", "Age", prof.get("age", ""), "30", "", "number", 'min="13" max="100"')}</div>
</div>
<div class="row">
<div>{_txt("height_cm", "Height (cm)", prof.get("height_cm", ""), "170", "", "number",
           'min="100" max="250" step="0.5"')}</div>
<div>{_txt("weight_kg", "Current weight (kg)", prof.get("weight_kg", ""), "70", "", "number",
           'min="30" max="300" step="0.1"')}</div>
</div>
<label for="timezone">Your timezone</label>{_tz_select(prof.get("timezone",
                                                                config.DEFAULT_TIMEZONE))}
<p class="hint">Every reminder time below is in this timezone, and your day rolls over here —
not on the server.</p>
</div>

<div class="card"><h2>Your goal</h2>
<label for="goal_type">What are you working toward?</label>
{_select("goal_type", _GOALS, prof.get("goal_type", "gentle_fat_loss"))}
<div class="row">
<div>{_txt("goal_weight_kg", "Goal weight (kg, optional)", prof.get("goal_weight_kg", ""),
           "65", "", "number", 'min="30" max="300" step="0.1"')}</div>
<div><label for="activity">How active are you?</label>
{_select("activity", _ACTIVITY, prof.get("activity", "moderate"))}</div>
</div>
{_txt("training", "How do you train?", prof.get("training", ""),
      "e.g. strength 4x/week plus a 30 min walk",
      "Free text — your coach reads this, so say it however you like.")}
</div>

<div class="card"><h2>Food</h2>
<label for="diet">Diet</label>{_select("diet", _DIETS, prof.get("diet", "anything"))}
{_txt("cuisine", "What do you usually eat?", prof.get("cuisine", ""),
      "e.g. North Indian, Mediterranean, British",
      "This makes photo estimates far more accurate — portions and cooking fats differ a lot "
      "between cuisines.")}
{_txt("allergies", "Allergies", prof.get("allergies", ""), "e.g. peanuts, shellfish",
      "Your coach will never suggest these.")}
{_txt("dislikes", "Foods you'd rather avoid", prof.get("dislikes", ""),
      "e.g. mushrooms, cottage cheese")}
</div>

<div class="card"><h2>Daily targets</h2>
<div class="row">
<div>{_txt("calorie_goal", "Calories", prof.get("calorie_goal", ""), "auto", "", "number",
           'min="800" max="6000"')}</div>
<div>{_txt("protein_goal", "Protein (g)", prof.get("protein_goal", ""), "auto", "", "number",
           'min="30" max="400"')}</div>
</div>
{auto}
<div class="row">
<div>{_txt("step_goal", "Step goal", prof.get("step_goal", config.DEFAULT_STEP_GOAL), "8000",
           "", "number", 'min="1000" max="50000"')}</div>
<div>{_txt("water_goal_ml", "Water goal (ml)",
           prof.get("water_goal_ml", config.DEFAULT_WATER_GOAL_ML), "2500", "", "number",
           'min="500" max="8000"')}</div>
</div>
</div>

<div class="card"><h2>Your coach</h2>
{_txt("coach_name", "What should it be called?", prof.get("coach_name", config.APP_NAME),
      config.APP_NAME)}
<label for="tone">Tone</label>{_select("tone", _TONES, prof.get("tone", "warm_friend"))}
<label for="language">Language</label>
{_select("language", _LANGS, prof.get("language", "english"))}
</div>

<div class="card"><h2>Reminders</h2>
<p class="hint">Only what you tick will ever be sent. You can also mute any of them later by
messaging your coach — "stop the water reminders", or "go quiet till Monday".</p>
{_checks("reminders_on", _REMINDERS, on)}
<div class="row">
<div>{_txt("gym_time", "Training check-in at", prof.get("gym_time", "08:30"), "", "", "time")}</div>
<div>{_txt("gym_days", "on these days", prof.get("gym_days", "mon-fri"), "mon-fri",
           "e.g. mon-fri, or mon,wed,fri. Blank means every day.")}</div>
</div>
{_txt("meal_times", "Meal photo nudges", prof.get("meal_times", ""),
      "13:00=lunch, 20:00=dinner",
      "Time=label, comma separated. Skipped automatically if you already logged a meal "
      "recently.")}
{_txt("water_times", "Water nudges", prof.get("water_times", "13:15, 19:15"), "13:15, 19:15",
      "Comma separated times.")}
<label for="supplements">Supplements</label>
<textarea id="supplements" name="supplements" placeholder="Vitamin D 1000IU | 09:00 | mon,thu
Creatine 5g | 08:00
Omega-3 | 10:30 | mon-fri">{e(prof.get("supplements", ""))}</textarea>
<p class="hint">One per line: <code>name | time | days</code>. Leave the days off for daily.</p>
<div class="row">
<div>{_txt("step_time", "Step nudge at", prof.get("step_time", "18:30"), "", "", "time")}</div>
<div>{_txt("winddown_time", "Wind-down at", prof.get("winddown_time", "22:30"), "", "", "time")}</div>
</div>
<div class="row">
<div>{_txt("summary_time", "Daily wrap-up at", prof.get("summary_time", "21:30"), "", "",
           "time")}</div>
<div>{_txt("weekly_day", "Weekly report on", prof.get("weekly_day", "mon"), "mon",
           "Day name, e.g. mon or sun.")}</div>
<div>{_txt("weekly_time", "at", prof.get("weekly_time", "09:00"), "", "", "time")}</div>
</div>
</div>

<p><button type="submit">{"Save and connect Telegram" if first_time else "Save changes"}</button>
</p>
</form>
"""
    if not first_time:
        body += _account_card(user)
    return page(heading, body, user, message, kind)


def _account_card(user) -> str:
    return f"""
<div class="card"><h2>Account</h2>
<p class="hint">Signed in as {e(user['email'])}.</p>
<form method="post" action="/password" style="margin-top:12px">
<label for="new_password">New password</label>
<input id="new_password" name="new_password" type="password" minlength="8"
 autocomplete="new-password">
<p style="margin-top:12px"><button class="btn ghost" type="submit">Change password</button></p>
</form>
<form method="post" action="/account/delete" style="margin-top:18px"
 onsubmit="return confirm('This permanently deletes your account, every meal you have logged, and all your body data. There is no undo. Continue?')">
<p class="hint">Deleting removes your account and every meal, measurement and note tied to it.
It cannot be undone.</p>
<p style="margin-top:10px"><button class="btn danger" type="submit">Delete my account</button></p>
</form>
</div>
"""


# The onboarding fields that are simply stored as-is on the profile.
PROFILE_FIELDS = (
    "sex", "age", "height_cm", "weight_kg", "timezone", "goal_type", "goal_weight_kg",
    "activity", "training", "diet", "cuisine", "allergies", "dislikes", "calorie_goal",
    "protein_goal", "step_goal", "water_goal_ml", "coach_name", "tone", "language",
    "gym_time", "gym_days", "meal_times", "water_times", "supplements", "step_time",
    "winddown_time", "summary_time", "weekly_day", "weekly_time",
)
_NUMERIC = {"age", "height_cm", "weight_kg", "goal_weight_kg", "calorie_goal", "protein_goal",
            "step_goal", "water_goal_ml"}


def save_questionnaire(user_id: str, form) -> str | None:
    """Write the answers to the profile. Returns an error message, or None on success.

    Numbers are validated rather than trusted: a stray character in a weight field would
    otherwise reach the metabolic-rate formula and produce a confident, wrong calorie target.
    """
    for field in PROFILE_FIELDS:
        raw = (form.get(field) or "").strip()
        if field in _NUMERIC and raw:
            try:
                float(raw)
            except ValueError:
                return f"'{raw}' isn't a number — check the {field.replace('_', ' ')} field."
        db.set_profile(user_id, field, raw)

    chosen = [k for k in form.getlist("reminders_on") if k in dict(_REMINDERS)]
    db.set_profile(user_id, "reminders_on", ",".join(chosen))

    tz = (form.get("timezone") or "").strip()
    if tz and tz not in TIMEZONES:
        return "That timezone isn't one I recognise — pick one from the list."

    # A weight typed here is also a real measurement, so it seeds the body-data history and
    # the first weekly report has something to compare against.
    w = (form.get("weight_kg") or "").strip()
    if w:
        from . import nutrition
        try:
            if not nutrition.latest_metric(user_id, "weight_kg"):
                nutrition.record_health(user_id, {"weight_kg": float(w)})
        except (ValueError, TypeError):
            pass
    return None


# ---------- connect ----------

def connect_page(user, message=None, kind="ok") -> HTMLResponse:
    acct = accounts.find_by_id(user["id"])
    bot = config.TELEGRAM_BOT_USERNAME
    if acct.get("chat_id"):
        body = f"""
<h1>Telegram connected ✅</h1>
<p class="sub">Your coach is live. Open Telegram and say hi.</p>
<div class="card"><h2>Try these</h2>
<p class="hint">Send a photo of your next meal. Or type <code>total</code> for today's numbers,
<code>week</code> for your weekly report, or just chat.</p>
{f'<p style="margin-top:16px"><a class="btn" href="https://t.me/{e(bot)}">Open the chat</a></p>'
 if bot else ''}
</div>
<div class="card"><h2>Disconnect</h2>
<p class="hint">This unlinks the Telegram chat. Your logged data stays exactly where it is,
and you can reconnect any time.</p>
<form method="post" action="/connect/reset" style="margin-top:12px">
<button class="btn ghost" type="submit">Disconnect Telegram</button></form>
</div>
{_health_card(user)}
"""
        return page("Telegram", body, user, message, kind)

    code = acct.get("link_code") or ""
    if not bot:
        link_bit = ('<p class="msg err">No Telegram bot is configured on this server yet — '
                    'set TELEGRAM_BOT_USERNAME and TELEGRAM_BOT_TOKEN.</p>')
    else:
        link_bit = (f'<p style="margin-top:16px">'
                    f'<a class="btn" href="https://t.me/{e(bot)}?start={e(code)}">'
                    f'Connect Telegram</a></p>'
                    f'<p class="hint">Opens Telegram with your personal code filled in. '
                    f'If it asks, tap <strong>Start</strong>.</p>')
    body = f"""
<h1>Connect Telegram</h1>
<p class="sub">One tap. No phone number to share, nothing to install if you already have
Telegram.</p>
<div class="card"><h2>Tap to connect</h2>{link_bit}
<p class="hint" style="margin-top:18px">On a computer without Telegram? Open the app on your
phone, search for <code>@{e(bot) or 'the bot'}</code>, and send:</p>
<pre>/start {e(code)}</pre>
<p class="hint">This code is yours alone and works once.</p>
</div>
{_health_card(user)}
"""
    return page("Connect Telegram", body, user, message, kind)


def _health_card(user) -> str:
    token = accounts.health_token(user["id"])
    base = config.PUBLIC_URL or "https://your-app-url"
    url = f"{base.rstrip('/')}/health?token={token}"
    return f"""
<div class="card"><h2>Sync your steps, sleep and weight (optional)</h2>
<p class="hint">Everything works without this — you can just tell your coach your weight, or
photograph your scale. But if you want steps and sleep to show up automatically, POST them to
your personal sync URL:</p>
<pre>{e(url)}</pre>
<p class="hint"><strong>iPhone:</strong> the free <em>Health Auto Export</em> app can POST your
Apple Health data to that URL on a schedule. Most bands — Fitbit, Garmin, Mi Band, Samsung —
can sync into Apple Health first, so this covers them too.</p>
<p class="hint"><strong>Android or anything else:</strong> any automation that can make a web
request works — Tasker, Health Sync, IFTTT, or a two-line script. Send flat JSON:</p>
<pre>{{"steps": 6200, "sleep_hours": 7.5, "weight_kg": 70.4}}</pre>
<p class="hint">Recognised fields: steps, water_ml, sleep_hours, weight_kg, resting_hr,
active_kcal, body_fat. Field names are matched loosely, so "Body Fat Percentage" works too.
Treat this URL like a password — it writes into your body data.</p>
</div>
"""


def home_page(user) -> HTMLResponse:
    """The signed-in landing spot: what's set up, what isn't, and what to do next."""
    uid = user["id"]
    prof = db.get_profile(uid)
    acct = accounts.find_by_id(uid)
    from . import nutrition
    n, tot = nutrition.day_totals(uid)
    cg, pg = accounts.calorie_goal(uid), accounts.protein_goal(uid)
    connected = bool(acct.get("chat_id"))
    on = sorted(reminders.enabled_kinds(prof))

    todo = []
    if not connected:
        todo.append('<a href="/connect">Connect Telegram</a> — until you do, your coach has no '
                    'way to reach you.')
    if not prof.get("weight_kg"):
        todo.append('<a href="/settings">Add your weight and height</a> so your targets can be '
                    'calculated.')
    todo_html = ("<div class=\"card\"><h2>Next steps</h2><ul><li>"
                 + "</li><li>".join(todo) + "</li></ul></div>") if todo else ""

    body = f"""
<h1>Hey {e(accounts.display_name(uid))} 👋</h1>
<p class="sub">{'Your coach is connected and running.' if connected
                else 'Almost there — one step left.'}</p>
{todo_html}
<div class="card"><h2>Today</h2>
<p class="hint">{n} meal{'' if n == 1 else 's'} logged ·
{tot['calories']}{f' / {cg}' if cg else ''} kcal ·
{tot['protein']}{f' / {pg}' if pg else ''}g protein</p>
<p class="hint" style="margin-top:10px">Everything is logged by messaging your coach — photos,
voice notes, or just typing what you ate.</p>
</div>
<div class="card"><h2>Your setup</h2>
<p class="hint">Telegram: {'connected ✅' if connected else 'not connected yet'}<br>
Timezone: {e(prof.get('timezone') or 'not set')}<br>
Tone: {e(prof.get('tone', 'warm_friend').replace('_', ' '))}<br>
Reminders on: {e(', '.join(on) if on else 'none')}</p>
<p style="margin-top:16px"><a class="btn ghost" href="/settings">Edit settings</a></p>
</div>
"""
    return page("Home", body, user)
