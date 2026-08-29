"""messenger.py — Telegram: sending, receiving, and the photo queue.

Telegram is the whole reason this app can be free for everyone. The Bot API costs nothing, has
no per-message billing, no 24-hour reply window, and no join code to re-send every few days —
so a new user taps one link and is connected, and the coach can message them at 7am next
Tuesday without needing them to say something first.

Everything user-visible is funnelled through send() / send_reminder() so that mutes, breaks
and the anti-burst gap are enforced in exactly one place.
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

import httpx

from . import accounts, ai, bodystats, coach, config, db, nutrition

API = "https://api.telegram.org"

# Anti-burst safety net: if two proactive nudges would fire in the same minute, skip the
# second. The real spacing comes from the schedule itself; this gap is small on purpose,
# because a large one here silently DROPS legitimate nudges rather than spreading them.
LAST_REMINDER: dict[str, datetime] = {}


def _api(method: str, **payload):
    if not config.TELEGRAM_TOKEN:
        print(f"[telegram] no bot token — would have called {method}")
        return None
    try:
        r = httpx.post(f"{API}/bot{config.TELEGRAM_TOKEN}/{method}", json=payload, timeout=30)
        if r.status_code >= 400:
            # Telegram explains refusals in the body ("bot was blocked by the user",
            # "chat not found"). The status code alone tells you nothing useful.
            print(f"[telegram] {method} -> {r.status_code}: {r.text[:300]}")
            return None
        return r.json().get("result")
    except Exception as e:
        print(f"[telegram] {method} failed: {e!r}")
        return None


def _chunks(text: str, limit: int = 3900):
    """Telegram rejects anything over 4096 characters. Split on paragraph breaks so a long
    weekly report arrives as readable messages rather than mid-sentence fragments."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def send(user: str, text: str) -> bool:
    """Send a message to a user by account id. Returns False if they aren't connected."""
    acct = accounts.find_by_id(user)
    if not acct or not acct.get("chat_id"):
        print(f"[telegram] user {user} has no connected chat — dropping message")
        return False
    ok = True
    for part in _chunks(text):
        if not part.strip():
            continue
        if _api("sendMessage", chat_id=acct["chat_id"], text=part,
                disable_web_page_preview=True) is None:
            ok = False
    return ok


def send_typing(user: str):
    acct = accounts.find_by_id(user)
    if acct and acct.get("chat_id"):
        _api("sendChatAction", chat_id=acct["chat_id"], action="typing")


def send_reminder(user: str, text: str, kind: str = "") -> bool:
    """Send a PROACTIVE nudge — unless they've asked for a break (then stay completely
    silent), muted this kind, or another nudge went out moments ago."""
    if coach.quiet_until(user):
        return False
    if kind and kind in coach.muted_kinds(user):
        return False
    prev = LAST_REMINDER.get(user)
    now = accounts.now_local(user)
    if prev and (now - prev).total_seconds() < config.MIN_REMINDER_GAP_MIN * 60:
        print(f"[reminders] skipped {kind} for {user} (within "
              f"{config.MIN_REMINDER_GAP_MIN}m of the last nudge)")
        return False
    if not send(user, text):
        return False
    LAST_REMINDER[user] = now
    return True


def fetch_file(file_id: str, attempts: int = 3) -> tuple[bytes | None, str]:
    """Download a photo or voice note. Returns (bytes, file_path). Retried, because a
    just-uploaded file can briefly 404 while Telegram finishes storing it — and one lost GET
    used to throw away a whole voice note."""
    if not config.TELEGRAM_TOKEN:
        return None, ""
    info = _api("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return None, ""
    path = info["file_path"]
    url = f"{API}/file/bot{config.TELEGRAM_TOKEN}/{path}"
    for i in range(attempts):
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            r.raise_for_status()
            return r.content, path
        except Exception as e:
            if i == attempts - 1:
                print(f"[telegram] file download failed after {attempts} tries: {e!r}")
                return None, path
            print(f"[telegram] file download failed ({e!r}) — retry {i + 1} in 2s")
            time.sleep(2)
    return None, path


def set_webhook(base_url: str) -> bool:
    """Point Telegram at this deployment. Called on boot so a redeploy re-registers itself and
    nobody has to remember a manual curl."""
    if not (config.TELEGRAM_TOKEN and config.TELEGRAM_WEBHOOK_SECRET and base_url):
        print("[telegram] webhook not registered (missing token, secret, or public URL)")
        return False
    url = f"{base_url.rstrip('/')}/telegram/{config.TELEGRAM_WEBHOOK_SECRET}"
    res = _api("setWebhook", url=url, secret_token=config.TELEGRAM_WEBHOOK_SECRET,
               allowed_updates=["message"], drop_pending_updates=False)
    print(f"[telegram] webhook -> {url} ({'ok' if res else 'FAILED'})")
    return bool(res)


# ---------- failure wording ----------
# Each stage says something DIFFERENT on purpose. Server logs roll off in days on a free host,
# so the coach's own reply is the only failure signal that survives — and one generic "couldn't
# catch that" for download, transcription AND reply failures makes a bug undiagnosable. A
# free-tier refusal must never read like a crash: it tells them the message survives a resend.

_OOPS_BRAIN = "my brain lagged for a sec 😅 say that again?"
_OOPS_BUSY = ("too many at once for my little free brain 😅 give it 20 seconds and send that "
              "again — it'll go through")
_OOPS_PHOTO = "something went wrong reading that photo 😕 mind sending it again?"


def _oops(e: Exception) -> str:
    return _OOPS_BUSY if ai.busy_brain(e) else _OOPS_BRAIN


# ---------- the photo queue ----------
# A multi-select of photos arrives as SEPARATE messages a second or two apart. Handled naively
# that spawns one background task per photo: N vision calls rate-limiting each other, and N
# plan pages racing to overwrite the same saved plan. One worker per person fixes both —
# photos are processed in arrival order and the burst gets ONE reply instead of N.

_photo_q: dict[str, list] = {}
_photo_arrived: dict[str, float] = {}
_photo_lock = threading.Lock()


def photos_pending(user: str) -> bool:
    with _photo_lock:
        return user in _photo_q


def enqueue_photo(user: str, file_id: str, note: str = ""):
    with _photo_lock:
        _photo_arrived[user] = time.monotonic()
        q = _photo_q.get(user)
        if q is None:
            _photo_q[user] = [(file_id, note)]
            start = True
        else:
            if len(q) >= config.PHOTO_QUEUE_MAX:
                print(f"[photos] queue full for {user}, dropping one")
                return
            q.append((file_id, note))
            start = False
    if start:
        threading.Thread(target=_photo_worker, args=(user,), daemon=True).start()


def _new_batch() -> dict:
    return {"meals": [], "pages": [], "stats": [], "unreadable": 0, "lost": 0,
            "saved_pages": 0, "appended": False}


def _save_photo(user: str, image: bytes, file_path: str) -> str:
    now = accounts.now_local(user)
    folder = accounts.media_dir(user, now.strftime("%Y-%m-%d"))
    ext = (file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg")[:4] or "jpg"
    import os
    path = os.path.join(folder, now.strftime("%H%M%S") + "." + ext)
    with open(path, "wb") as f:
        f.write(image)
    return path


def _photo_step(user: str, file_id: str, note: str, batch: dict):
    """Handle ONE photo and record what happened. Deliberately sends nothing — the whole burst
    answers once, at the end."""
    img, file_path = fetch_file(file_id)
    if img is None:
        batch["lost"] += 1
        return
    saved = _save_photo(user, img, file_path)
    kind = nutrition.classify_image(img)
    if kind == "PLAN":
        text = nutrition.vision_plan_extract(img)
        if text and len(text) >= 40:
            batch["pages"].append(text)
        else:
            batch["unreadable"] += 1
        return
    if kind == "STATS":
        stats = nutrition.vision_body_stats(img)
        if stats:
            nutrition.record_health(user, stats)
            batch["stats"].append(stats)
        else:
            batch["unreadable"] += 1
        return
    estimate = nutrition.vision_estimate(user, img, note=note)
    if not estimate:
        # We could see a plate but couldn't size it up. Do NOT log a 0-kcal meal to paper over
        # that — it hides in the day's totals and every weekly average after it. Ask instead.
        batch["unreadable"] += 1
        return
    items, macros = estimate
    meal_id = nutrition.log_meal(user, items, macros, saved)
    nutrition.remember_meal(user, meal_id, items, macros)
    batch["meals"].append((items, macros))


def _send_batch_reply(user: str, batch: dict):
    """ONE reply for a whole burst of photos, however many arrived."""
    meals, pages, stats = batch["meals"], batch["pages"], batch["stats"]
    bad, lost = batch["unreadable"], batch["lost"]
    if not (meals or pages or stats):
        if lost and not bad:
            send(user, f"couldn't open {'those pics' if lost > 1 else 'that pic'} 😕 mind "
                       "sending again, or just telling me what was in them?")
        else:
            send(user, f"i can see {'them' if (bad + lost) > 1 else 'it'} but couldn't read it "
                       "properly 😕 tell me what's there and i'll add it")
        return

    # A single meal photo on its own keeps the familiar, personal reply.
    if len(meals) == 1 and not pages and not stats and not bad and not lost:
        items, macros = meals[0]
        n, tot = nutrition.day_totals(user)
        voice = coach.meal_react(
            user, f"They just sent a meal photo. You saw: {items}. This meal (estimated): "
            f"{nutrition.macro_line(macros)}. Their running totals today: "
            f"{nutrition.macro_line(tot)}.")
        send(user, voice + coach.meal_footer(
            user, "📷 Snapped:", items, macros, n, tot,
            hint='wrong? just tell me — e.g. "2 slices not 1" or "big bowl" 🙂'))
        return

    what = []
    if pages:
        what.append(f"{batch['saved_pages']} page(s) of their diet plan")
    if meals:
        what.append("meals: " + "; ".join(i for i, _ in meals))
    if stats:
        what.append(f"{len(stats)} body-stats screenshot(s)")
    try:
        voice = coach.say_in_voice(
            user, "They just sent several photos in one go and you've read them all: "
            + " | ".join(what) + ". Warmly confirm you've got the lot, in ONE or TWO short "
            "lines. Do not list them back — the details follow underneath your message.")
    except Exception:
        voice = "got them all 🙌"

    lines = []
    if pages:
        n = batch["saved_pages"]
        tail = " (added to the pages you'd already sent)" if batch["appended"] else ""
        lines.append(f"📋 read all {n} page{'s' if n != 1 else ''} of your plan ✅{tail}\n"
                     "ask me \"what's my lunch today?\" anytime")
    if meals:
        n_day, tot = nutrition.day_totals(user)
        rows = "\n".join(f"• {items} — {nutrition.macro_line(m)}" for items, m in meals)
        lines.append(f"📷 logged {len(meals)} meals\n{rows}\n\n"
                     f"{coach.today_block(user, n_day, tot)}")
    if stats:
        # The useful answer to a scale screenshot is "what MOVED", not a flat list of today's
        # numbers. Falls back to the list when there's nothing to compare against yet.
        lines.append(bodystats.compare_block(user) or (
            "📊 saved your body stats: " + nutrition.health_line(
                nutrition.health_snapshot(user, defs=db.BODY_STATS), defs=db.BODY_STATS)))
    if bad:
        lines.append(f"😕 {bad} photo{'s' if bad != 1 else ''} i couldn't read — tell me what "
                     f"{'they were' if bad != 1 else 'it was'} and i'll add "
                     f"{'them' if bad != 1 else 'it'}")
    if lost:
        lines.append(f"📷 {lost} didn't download — send {'those' if lost != 1 else 'that'} again?")
    send(user, voice + "\n\n" + "\n\n".join(lines))


def _photo_worker(user: str):
    """Drain one person's queue IN ORDER, then answer the whole burst once."""
    batch = _new_batch()
    try:
        while True:
            item = None
            with _photo_lock:
                q = _photo_q.get(user)
                if q:
                    item = q.pop(0)
            if item is None:
                # Wait only for what's LEFT of the settle window since the last photo ARRIVED.
                # Measured from arrival, never from "we finished working": reading a photo
                # already takes 10-20s, so by the time the queue drains the burst has long
                # since landed. Sleeping the full window anyway adds ~8s of dead air to every
                # single-photo reply.
                with _photo_lock:
                    since = time.monotonic() - _photo_arrived.get(user, 0)
                left = config.PHOTO_SETTLE_SEC - since
                if left > 0:
                    time.sleep(left)
                with _photo_lock:
                    if not _photo_q.get(user):
                        del _photo_q[user]      # frees the slot; the next photo starts a worker
                        _photo_arrived.pop(user, None)
                        break
                continue
            try:
                _photo_step(user, item[0], item[1], batch)
            except Exception:
                traceback.print_exc()  # one bad photo must not kill the rest of the burst
                batch["unreadable"] += 1
    except Exception:
        traceback.print_exc()  # background thread: without this the failure is invisible
        with _photo_lock:
            _photo_q.pop(user, None)
    try:
        batch["saved_pages"], batch["appended"] = coach.save_plan_pages(user, batch["pages"])
        _send_batch_reply(user, batch)
    except Exception:
        traceback.print_exc()
        send(user, _OOPS_PHOTO)


# ---------- voice notes ----------

import re as _re
_NO_SPEECH_RE = _re.compile(r"^[\W_]*$")


def process_voice(user: str, file_id: str, mime: str | None):
    """Download, transcribe, then run the text through the same brain as a typed message."""
    try:
        audio, _ = fetch_file(file_id)
        if not audio:
            send(user, "that voice note wouldn't download 📥 mind sending it again?")
            return
    except Exception:
        traceback.print_exc()
        send(user, "that voice note wouldn't download 📥 mind sending it again?")
        return
    try:
        text = ai.transcribe(audio, mime)
    except Exception:
        traceback.print_exc()
        send(user, "my ears glitched on that one 🙉 give it one more go?")
        return
    # Whisper returns something like " ." for a note with no speech in it (a pocket tap, a mic
    # held too briefly). It's non-empty, so without this it sails through as a real message.
    if not text or _NO_SPEECH_RE.match(text):
        send(user, "that one came through silent 🤫 the mic didn't catch anything — say it "
                   "again or type it?")
        return
    try:
        reply = coach.respond_to_text(user, text, send)
        if reply is not None:  # corrections send their own confirmation
            send(user, f'🎧 heard: "{text}"\n\n{reply}')
    except Exception as e:
        # We DID hear them — never let their words vanish because the reply failed.
        traceback.print_exc()
        send(user, f'🎧 heard: "{text}"\n\n…but {_oops(e)}')


def process_text(user: str, text: str):
    """A typed message, handled in the background so the webhook can answer Telegram instantly
    (Telegram retries an update the webhook is slow to acknowledge, which would double-reply
    and double-log the same meal)."""
    try:
        send_typing(user)
        reply = coach.respond_to_text(user, text, send)
        if reply is not None:
            send(user, reply)
    except Exception as e:
        traceback.print_exc()
        send(user, _oops(e))


# ---------- linking an account ----------

_WELCOME = (
    "hey! 👋 i'm {coach}, your fitness accountability partner.\n\n"
    "you're connected ✅ here's what i can do:\n\n"
    "📷 send a photo of your meal — i'll estimate the macros and log it\n"
    "🎤 or just send a voice note / type it: \"had oats and a coffee\"\n"
    "⚖️ send a screenshot of your smart scale and i'll track the trend\n"
    "📋 send photos of a diet plan and i'll coach you by it\n"
    "💬 or just talk to me — i'm here for that too\n\n"
    "try \"total\" any time for today's numbers, or \"week\" for your weekly report.\n"
    "want me to stop nudging? say \"pause the water reminders\" or \"go quiet till monday\".\n\n"
    "so — what are we eating today? 😄")


def handle_start(chat_id, payload: str) -> str:
    """/start, with or without a link code. This is the entire connection flow: the website
    hands them a link carrying a one-time code, they tap it, and the account is bound."""
    existing = accounts.find_by_chat_id(chat_id)
    code = (payload or "").strip()
    if existing:
        if code and accounts.find_by_link_code(code):
            return ("this chat is already connected to an account 🙂 if you want to move it to "
                    "a different one, disconnect from the website first.")
        return _WELCOME.format(coach=db.get_profile(existing["id"]).get("coach_name")
                               or config.APP_NAME)
    if not code:
        site = config.PUBLIC_URL or "the website"
        return (f"hey! 👋 i'm {config.APP_NAME}. i need to know which account you are before we "
                f"start.\n\nsign in at {site}, open the Connect page, and tap the button there "
                "— it opens this chat with your personal code and we're away 🚀")
    user = accounts.find_by_link_code(code)
    if not user:
        return ("that connect link has already been used or has expired 😕 grab a fresh one "
                "from the Connect page on the website and tap it again.")
    accounts.set_chat_id(user["id"], chat_id)
    if not user["onboarded"]:
        return ("connected ✅ but you haven't finished setting up yet — pop back to the website "
                "and finish the questions so i know your goals, times and targets. then say hi "
                "here and we're off 🚀")
    return _WELCOME.format(coach=db.get_profile(user["id"]).get("coach_name") or config.APP_NAME)


def handle_update(update: dict, background) -> None:
    """One Telegram update. Everything slow is pushed to the background so the webhook returns
    immediately; Telegram treats a slow webhook as a failure and re-sends the update."""
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()

    if text.startswith("/start"):
        payload = text[len("/start"):].strip()
        send_raw(chat_id, handle_start(chat_id, payload))
        return

    acct = accounts.find_by_chat_id(chat_id)
    if not acct:
        site = config.PUBLIC_URL or "the website"
        send_raw(chat_id, f"i don't know which account this chat belongs to yet 🙂 sign in at "
                          f"{site} and tap the connect button there.")
        return
    if not acct["onboarded"]:
        send_raw(chat_id, "almost there — finish the setup questions on the website and i'll "
                          "have everything i need to coach you properly 🙌")
        return

    user = acct["id"]
    db.set_profile(user, "last_inbound", datetime.now().isoformat(timespec="seconds"))

    if text in ("/help", "/start@", "/commands"):
        send(user, _WELCOME.format(
            coach=db.get_profile(user).get("coach_name") or config.APP_NAME))
        return

    if msg.get("voice") or msg.get("audio"):
        blob = msg.get("voice") or msg.get("audio")
        background(process_voice, user, blob["file_id"], blob.get("mime_type"))
        send(user, "🎧 got your voice note, listening...")
        return

    photo = msg.get("photo")
    doc = msg.get("document")
    if photo or (doc and str(doc.get("mime_type", "")).startswith("image/")):
        # Telegram sends several sizes of the same photo; the last is the largest.
        file_id = photo[-1]["file_id"] if photo else doc["file_id"]
        already = photos_pending(user)
        enqueue_photo(user, file_id, caption)
        if not already:
            send(user, "ooh 👀 taking a look, one sec... (send as many as you like, i'll read "
                       "them all and reply once)")
        return

    body = text or caption
    if not body:
        send(user, "say something and i'm here 💙")
        return
    background(process_text, user, body)


def send_raw(chat_id, text: str):
    """Send to a chat we may not have an account for yet (the /start handshake)."""
    for part in _chunks(text):
        _api("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)
