"""Telegram handling: linking, routing an update, the photo queue, and long messages.

Nothing leaves the process — the Telegram API call is replaced with a recorder, so a bug here
can never message a real person.
"""
import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
accounts = _safe.load("accounts")
messenger = _safe.load("messenger")

CALLS = []
messenger._api = lambda method, **payload: CALLS.append((method, payload)) or {"ok": True}

QUEUED = []


def background(fn, *args):
    QUEUED.append((fn.__name__, args))


# ---- an unknown chat is told where to go, and nothing is logged for it ----------------------
CALLS.clear()
messenger.handle_update({"message": {"chat": {"id": 999}, "text": "hello"}}, background)
c("an unknown chat gets one reply", len(CALLS), 1)
c("and it points at the website", "sign in" in CALLS[0][1]["text"].lower(), True)
c("and nothing was queued for processing", QUEUED, [])

# ---- /start with no code explains the flow ---------------------------------------------------
CALLS.clear()
messenger.handle_update({"message": {"chat": {"id": 999}, "text": "/start"}}, background)
c("/start with no code explains what to do", "Connect page" in CALLS[0][1]["text"], True)

# ---- /start with a valid code links the account -----------------------------------------------
u = accounts.create_user("tg@example.com", "password123", "Kim")
accounts.mark_onboarded(u["id"])
code = accounts.find_by_id(u["id"])["link_code"]
CALLS.clear()
messenger.handle_update({"message": {"chat": {"id": 555}, "text": f"/start {code}"}}, background)
c("the chat is bound to the account", accounts.find_by_chat_id("555")["id"], u["id"])
c("and they get the welcome", "you're connected" in CALLS[0][1]["text"].lower(), True)

# ---- a used code can't be replayed by someone else ---------------------------------------------
CALLS.clear()
messenger.handle_update({"message": {"chat": {"id": 777}, "text": f"/start {code}"}}, background)
c("a spent link code is refused", "already been used" in CALLS[0][1]["text"], True)
c("and no second chat was bound", accounts.find_by_chat_id("777"), None)

# ---- an account that hasn't finished onboarding is told so ---------------------------------------
half = accounts.create_user("half@example.com", "password123", "Pat")
accounts.set_chat_id(half["id"], "111")
CALLS.clear()
QUEUED.clear()
messenger.handle_update({"message": {"chat": {"id": 111}, "text": "had lunch"}}, background)
c("an un-onboarded user is sent back to finish setup", "finish the setup" in CALLS[0][1]["text"],
  True)
c("and their message is NOT processed", QUEUED, [])

# ---- a text message is handled in the background so the webhook can answer instantly -------------
CALLS.clear()
QUEUED.clear()
messenger.handle_update({"message": {"chat": {"id": 555}, "text": "had oats"}}, background)
c("a text message is queued, not run inline", [q[0] for q in QUEUED], ["process_text"])
c("and the webhook itself sent nothing", CALLS, [])

# ---- a voice note acknowledges immediately, then works in the background ---------------------------
CALLS.clear()
QUEUED.clear()
messenger.handle_update(
    {"message": {"chat": {"id": 555}, "voice": {"file_id": "v1", "mime_type": "audio/ogg"}}},
    background)
c("a voice note is queued", [q[0] for q in QUEUED], ["process_voice"])
c("and acknowledged straight away", "listening" in CALLS[0][1]["text"], True)

# ---- photos go through the queue, and a burst is acknowledged ONCE ----------------------------------
sent = []
messenger.enqueue_photo = lambda user, file_id, note="": sent.append(file_id)
CALLS.clear()
photo_msg = {"message": {"chat": {"id": 555},
                         "photo": [{"file_id": "small"}, {"file_id": "large"}],
                         "caption": "lunch"}}
messenger.handle_update(photo_msg, background)
c("the LARGEST size is the one used", sent, ["large"])
c("the first photo of a burst is acknowledged", len(CALLS), 1)

# The second photo of the same burst must not repeat "ooh, taking a look".
messenger.photos_pending = lambda user: True
CALLS.clear()
messenger.handle_update(photo_msg, background)
c("a follow-up photo in the burst says nothing", CALLS, [])

# ---- long messages are split, because Telegram rejects anything over 4096 chars ------------------------
long_text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(40))
parts = messenger._chunks(long_text)
c("a long message is split", len(parts) > 1, True)
c("every part is under the limit", all(len(p) <= 3900 for p in parts), True)
c("nothing is lost", sum(len(p) for p in parts) >= len(long_text) - 2 * len(parts), True)
c("a short message stays one part", messenger._chunks("hi"), ["hi"])

# ---- sending to a user with no connected chat fails safely rather than crashing --------------------------
lonely = accounts.create_user("lonely@example.com", "password123", "Lee")
c("sending to an unconnected user returns False", messenger.send(lonely["id"], "hi"), False)

raise SystemExit(0 if c.done("messaging: ") else 1)
