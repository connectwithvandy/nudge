# Nudge

A fitness accountability partner that lives in your Telegram.

You sign up on the website, answer a short set of questions once, and tap one link to connect
Telegram. After that it's a conversation: send a photo of your dinner and it estimates the
macros and logs them against *your* targets; send a voice note or just type it; send a
screenshot of your smart scale and it tells you what actually moved since your last one.

It runs entirely on free tiers.

---

## What it does

| | |
|---|---|
| 📷 **Meal photos** | Full macros and micros, estimated from the photo, logged against your targets. Send a burst — it reads them all and replies once. |
| 🎤 **Voice notes** | Transcribed and treated exactly like a typed message. |
| 💬 **Typed meals** | "had oats and a coffee" logs it. "3 slices not 1" corrects it. |
| ⚖️ **Body composition** | Screenshot your scale app. It compares **scan to scan** and always names the gap it measured. |
| 📋 **Your own diet plan** | Photograph a plan from any coach or nutritionist. It reads it and coaches by it instead of by generic advice. |
| ⏰ **Reminders** | Training, meals, water, supplements, steps, wind-down, daily wrap-up, weekly report — your times, your timezone, only the ones you tick. |
| 🤐 **Silence on request** | "Go quiet till Monday." "Stop the water reminders." Both work, from chat, immediately. |
| 🧠 **Memory** | It remembers what you like, what you avoid, and what you're working toward. |

Your coach's **name, tone and language** are yours — warm friend, tough love, calm and
factual, or cheerleader; English, Hinglish, Spanish and more.

---

## Running it

### 1. Get the free accounts

| Service | For | Cost |
|---|---|---|
| [Groq](https://console.groq.com/keys) | chat, vision, voice transcription | free tier |
| [@BotFather](https://t.me/botfather) on Telegram | the bot itself | free, unlimited |
| [Turso](https://turso.tech) | the cloud snapshot of the database | free tier |
| [Render](https://render.com) | hosting | free tier |

### 2. Locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill it in — every setting is documented there
.venv/bin/python tests/run_all.py
.venv/bin/uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>. Create an invite code for yourself with
`python3 tools/invite.py new`, then sign up.

For Telegram to reach a laptop you need a public URL — run `ngrok http 8000` and put the
https URL in `PUBLIC_URL`. The webhook re-registers itself on every boot.

### 3. Deploying

Push to `main`. Render builds from `render.yaml`; paste the secrets into its dashboard. The
app registers its own Telegram webhook at startup from `RENDER_EXTERNAL_URL`.

**A redeploy wipes Render's free disk.** The Turso snapshot is what restores it on boot, and
it is pulled back *before* anything opens the database. Without `TURSO_DATABASE_URL` set, one
deploy erases every user's history. Set it before you have real users.

Verify a deploy from outside:

```bash
curl -s https://<your-app>.onrender.com/healthz     # {"status":"alive","version":"<sha>"}
git rev-parse --short HEAD                           # should match
```

---

## Why invite-only

One shared free Groq key answers every message for every user here. That budget is a
per-minute and per-day token allowance, so the number of active accounts is what decides
whether replies arrive in two seconds or come back as "give it 20 seconds". `INVITE_CODES` and
`MAX_USERS` are the throttle. Mint codes with `python3 tools/invite.py new`.

When you outgrow it, the honest options are: raise the cap and accept slower replies, or have
each user bring their own free Groq key.

---

## How it's put together

```
main.py              routes, the Telegram webhook, the health endpoint, boot
app/
  config.py          every environment setting, read and stripped in one place
  db.py              schema, per-user profile store, the cloud snapshot
  accounts.py        sign-up, passwords, sessions, invites, per-user time and targets
  ai.py              Groq (and local Ollama), with the free tier's failure modes handled
  nutrition.py       meals, macros, health metrics — numbers, no personality
  bodystats.py       scan-to-scan body composition trends
  healthsync.py      steps/sleep/weight coming in from a phone or band
  coach.py           the personality: what it knows, how it decides, how it speaks
  messenger.py       Telegram send/receive and the photo queue
  reminders.py       one sweep a minute, on every user's own clock
  web.py             the website
prompts/
  persona.md         the voice. This file is the product — iterate on it most.
  nutrition.md       the nutrition knowledge behind coaching answers
tests/               10 suites, seconds, no network and no AI calls
tools/               invite codes, and pulling the live database down to read
```

Two rules hold the design together:

**The chat model never computes anybody's numbers.** The vision model reads the photo, the
totals live in SQLite, and the chat model is handed the facts and only phrases them. It cannot
invent your calories.

**A failed estimate never becomes a number.** Anything unparseable returns `None` and asks you,
because a 0-kcal meal is invisible in the moment and then quietly corrupts that day's totals
and every weekly average after it.

---

## Testing

```bash
python3 tests/run_all.py
```

Ten suites, a couple of seconds, no network and no AI calls. `tests/_safe.py` blanks every
credential *before* importing anything from `app/` and then asserts they're gone — without
that, a test run can fire real messages at real users and push a throwaway database over the
live snapshot.

---

## Privacy

Everything a user sends lives in one SQLite database and its snapshot: their meals, body
measurements, and the facts the coach has learned. Meal photos are kept on disk under
`media/`. **Delete my account** in Settings removes the account and every row belonging to it.

Nothing is shared between users, and no query returns another user's data — every table is
keyed by account id, so a query that forgets to filter returns nothing rather than everything.

---

## Not a doctor

Nudge estimates, encourages and remembers. It does not diagnose, does not prescribe, and is
told to point people at an actual professional for anything clinical. Photo-based macro
estimates are estimates.
