# Nudge — rules for working on this project

## What this is

A multi-user fitness accountability partner: a website you sign up on, a Telegram bot you
connect, and a coach that logs your food from photos and nudges you on your own schedule.
It runs entirely on free tiers and must keep doing so — **never add a paid service.**

## The rules that matter most

- **It serves many people now.** Every table is keyed by account id (`user`, holding
  `str(users.id)`). Every query filters by it. A query that forgets returns *nothing*, which
  is the safe failure — never write one that could return everything.
- **Nothing personal belongs in the code.** Targets, times, timezone, diet, tone, goal weight,
  supplements: all of it lives in that user's `profile` rows. If you find yourself adding a
  constant that only fits one person, it belongs in the onboarding form instead.
- **The chat model never computes anyone's numbers.** Vision reads the photo, SQLite holds the
  totals, the chat model is handed facts and only phrases them.
- **Never let a failed estimate become a number.** Anything unparseable returns `None` and asks
  the user. A 0-kcal meal is invisible in the moment and then corrupts that day's totals and
  every weekly average after it.
- **Run `python3 tests/run_all.py` before every deploy.** Ten suites, seconds, no network and
  no AI calls. `tests/_safe.py` blanks `TELEGRAM_*` / `TURSO_*` / `GROQ_API_KEY` *before*
  importing anything from `app/` and asserts they're gone — without it a test run fires real
  messages at real users, and shutdown pushes a throwaway database over the live snapshot.

## Things that have already gone wrong, and must not again

- **Compare body composition SCAN TO SCAN, never over a fixed window.** People weigh in
  whenever they get round to it — real gaps run 1, 5, 6, 8 and 13 days. A 7-day window fails
  both ways: on a short gap the comparison silently vanishes (a missing change looks exactly
  like no change), and on a long one it reaches *past* the reading it should have used and
  reports a change against something three weeks old, with the right sign, under a heading
  saying "your week". `bodystats.scan_days` / `scan_trend` anchor on days carrying a metric
  only a scale screenshot can produce, so a weight synced from a phone never becomes the
  baseline a screenshot is judged against. **Always print the gap measured** — a change means
  nothing until you know whether it took 6 days or 13.
- **Groq answers a per-minute token limit with `413`, not `429`.** "Payload Too Large" from
  `api.groq.com` almost never means the prompt is big (a full coaching prompt is ~7k tokens) —
  it means this one request would exceed what's left of the allowance, and it arrives after a
  burst. `GroqClient.chat` must fall back on 413 exactly like 429. Always log
  `e.response.text`; the status code alone sends you hunting for the wrong bug entirely.
- **A reasoning model spends `max_tokens` thinking and returns `""`.** No error, no clue, just
  nothing — which callers turn into a meal with no items. Hence `REASONING_RESERVE`, and the
  rule that an empty reply is never handed back as an answer.
- **`.strip()` every secret read from the environment.** A value pasted into a hosting
  dashboard carries a trailing newline and httpx rejects it client-side
  (`LocalProtocolError: Illegal header value`) before any request is sent. Everything goes
  through `config.env()`; never read `os.environ` directly for a secret.
- **Never swallow an exception in a background task.** `_photo_worker`, `process_voice` and
  `process_correction` run outside the request, so a bare `except: send_oops()` erases the only
  evidence — that's how a perfectly-transcribed voice note comes back as "couldn't catch that"
  with nothing to debug. Always `traceback.print_exc()` first.
- **Each failure stage must say something DIFFERENT to the user.** Server logs roll off in days
  on a free host; the coach's own reply is the only failure signal that survives. Distinct
  wording for download vs transcribe vs reply is what localises a bug in one retry. A free-tier
  refusal ("send it again in 20s") must never read like a crash.
- **Photos arrive as separate messages.** A burst goes through `enqueue_photo` — one worker per
  person, in order, ONE reply at the end. Never process them concurrently: it rate-limits vision
  against itself, and plan pages read-modify-writing the same profile row silently overwrite
  each other (pages vanish while the reply still says "added ✅").
- **A model Groq deleted must be marked in `_gone`, not `_out_today`.** The daily check asks
  "is this marker today?", so a far-future sentinel date is never equal to today and the
  retired model gets retried on every single message.
- **Timestamps read back from the database may be naive.** Subtracting one from an aware
  "now" raises `TypeError` and takes down the whole path — not with a missing meal, with a
  crash. Attach the user's zone before comparing.

## Layout and deployment

- `main.py` is routes and boot only. The work is in `app/`; see the README for the map.
- `prompts/persona.md` is the voice, and it is the product — iterate on it more than the code.
  It holds only what's true for **every** user; anything per-person is injected at runtime by
  `coach.voice_block()` from their settings.
- **Production auto-deploys from `main` on Render, and a redeploy WIPES the free disk.** The
  Turso snapshot restores it on boot, before anything opens the database. Bundle changes and
  deploy rarely; every deploy risks the minutes since the last snapshot. Changing an env var in
  Render also restarts the service — same cost.
- Verify a deploy from outside: `GET /healthz` returns `{"status":"alive","version":"<sha>"}`.
  Compare it to `git rev-parse --short HEAD` — that proves which code is live.
- `python3 tools/pull_snapshot.py` copies the live database down to read. Read-only by intent;
  the running app owns the real file.

## Working style

- Explain simply, one step at a time. Ask clarifying questions BEFORE building.
- Simple beats clever. Never over-engineer.
- Go easy on live Groq calls while testing — the free tier's budget is shared by every user,
  so a debugging session spends their replies.
