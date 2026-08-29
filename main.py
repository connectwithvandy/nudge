"""main.py — the app: web routes, the Telegram webhook, the health-sync endpoint, and boot.

    uvicorn main:app --reload --port 8000

Everything of substance lives in app/. This file is the wiring: it maps URLs to the modules
that do the work, and it owns startup and shutdown.
"""
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app import accounts, coach, config, db, healthsync, messenger, nutrition, reminders, web


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot and shutdown, as a lifespan handler.

    Deliberately NOT the deprecated @app.on_event decorators. requirements.txt pins no major
    version, so the release that finally removes them would quietly stop running startup — and
    startup is where the cloud snapshot is restored. On a host that wipes its disk each deploy,
    the first symptom would be every user's history gone, with nothing in the logs to say why.
    """
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------- website ----------

@app.get("/")
async def index(request: Request):
    user = web.current_user(request)
    if user:
        return RedirectResponse("/app", status_code=303)
    return web.landing(None)


@app.get("/app")
async def home(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user["onboarded"]:
        return RedirectResponse("/onboarding", status_code=303)
    return web.home_page(user)


@app.get("/signup")
async def signup_get(request: Request):
    if web.current_user(request):
        return RedirectResponse("/app", status_code=303)
    return web.signup_form()


@app.post("/signup")
async def signup_post(request: Request):
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k in ("name", "email", "invite")}
    email = values["email"].lower()
    password = form.get("password") or ""

    if not web.re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email):
        return web.signup_form("That doesn't look like an email address.", values=values)
    problem = accounts.password_problem(password)
    if problem:
        return web.signup_form(problem, values=values)
    if not accounts.invite_ok(values["invite"]):
        return web.signup_form(
            "That invite code isn't valid or has been used up. Ask whoever invited you for a "
            "fresh one.", values=values)
    if accounts.user_count() >= config.MAX_USERS:
        return web.signup_form(
            "This instance is full right now — the shared free AI tier only stretches so far. "
            "Try again later.", values=values)
    if accounts.find_by_email(email):
        return web.signup_form("There's already an account with that email. "
                               "Try signing in instead.", values=values)

    user = accounts.create_user(email, password, values["name"])
    accounts.spend_invite(values["invite"])
    db.set_profile(user["id"], "timezone", config.DEFAULT_TIMEZONE)
    resp = RedirectResponse("/onboarding", status_code=303)
    _set_session(resp, user["id"])
    return resp


@app.get("/login")
async def login_get(request: Request):
    if web.current_user(request):
        return RedirectResponse("/app", status_code=303)
    return web.login_form()


@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    user = accounts.find_by_email(email)
    # One message for both "no such account" and "wrong password", so this page can't be used
    # to find out which email addresses have accounts here.
    if not user or not accounts.verify_password(form.get("password") or "", user["pw_hash"]):
        return web.login_form("That email and password don't match.", email=email)
    resp = RedirectResponse("/app" if user["onboarded"] else "/onboarding", status_code=303)
    _set_session(resp, user["id"])
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(web.COOKIE, "")
    if token:
        accounts.end_session(token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(web.COOKIE)
    return resp


def _set_session(resp, user_id: str):
    resp.set_cookie(web.COOKIE, accounts.start_session(user_id),
                    max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=bool(config.PUBLIC_URL.startswith("https://")))


@app.get("/onboarding")
async def onboarding_get(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return web.questionnaire(user, db.get_profile(user["id"]), first_time=True)


@app.post("/onboarding")
async def onboarding_post(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    err = web.save_questionnaire(user["id"], form)
    if err:
        return web.questionnaire(user, db.get_profile(user["id"]), True, err, "err")
    accounts.mark_onboarded(user["id"])
    return RedirectResponse("/connect", status_code=303)


@app.get("/settings")
async def settings_get(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    saved = request.query_params.get("saved")
    return web.questionnaire(user, db.get_profile(user["id"]), False,
                             "Saved ✅ your coach has it already." if saved else None)


@app.post("/settings")
async def settings_post(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    err = web.save_questionnaire(user["id"], form)
    if err:
        return web.questionnaire(user, db.get_profile(user["id"]), False, err, "err")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/password")
async def password_post(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    new = form.get("new_password") or ""
    problem = accounts.password_problem(new)
    if problem:
        return web.questionnaire(user, db.get_profile(user["id"]), False, problem, "err")
    accounts.set_password(user["id"], new)
    return web.questionnaire(user, db.get_profile(user["id"]), False, "Password changed ✅")


@app.get("/connect")
async def connect_get(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return web.connect_page(user)


@app.post("/connect/reset")
async def connect_reset(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    accounts.unlink_chat(user["id"])
    return web.connect_page(accounts.find_by_id(user["id"]),
                            "Telegram disconnected. Your data is untouched.")


@app.post("/account/delete")
async def account_delete(request: Request):
    user = web.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    accounts.delete_user(user["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(web.COOKIE)
    return resp


# ---------- Telegram ----------

@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request, background_tasks: BackgroundTasks):
    """Telegram doesn't sign its updates, so the shared secret is checked twice: it's in the
    URL we registered, and Telegram echoes it back in a header. Both must match, and both are
    compared in constant time so the check can't be probed one character at a time."""
    import hmac
    expected = config.TELEGRAM_WEBHOOK_SECRET
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected or not (hmac.compare_digest(secret, expected)
                            and hmac.compare_digest(header, expected)):
        print("[telegram] rejected an update with a bad secret")
        return Response(status_code=403)
    try:
        update = await request.json()
    except Exception:
        return Response(status_code=204)
    # Answer Telegram immediately and do the slow work after. A webhook that takes too long is
    # treated as failed and the same update is re-sent — which double-replies and double-logs
    # the same meal.
    try:
        messenger.handle_update(update, background_tasks.add_task)
    except Exception:
        import traceback
        traceback.print_exc()
    return Response(status_code=200)


# ---------- health sync ----------

@app.post("/health")
async def health_ingest(request: Request):
    """Where a phone or automation POSTs steps, sleep, weight and the rest.

    The token identifies WHICH user is writing, so it is per-user and never a single shared
    secret: one leaked shortcut must not be able to write into somebody else's body data.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "send JSON"}, status_code=400)

    # The token may arrive in the body, a header, or the query string — whichever the phone
    # app makes easy.
    auth = request.headers.get("authorization", "")
    supplied = (data.get("token") or request.headers.get("x-health-token")
                or (auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else None)
                or request.query_params.get("token"))
    user = accounts.user_by_health_token((supplied or "").strip())
    if not user:
        # Fails CLOSED. A missing or wrong token can never fall back to "some user".
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)

    metrics = healthsync.parse(data, user)
    if not metrics:
        return JSONResponse({"ok": True, "stored": "nothing recognised in that payload"})
    try:
        nutrition.record_health(user, metrics)
        healthsync.stamp_sync(user)
        stored = nutrition.health_line(nutrition.health_snapshot(user))
    except Exception as ex:  # e.g. a rare "database is locked" — never 500 at the phone
        print(f"[health] store failed: {ex!r}")
        return JSONResponse({"ok": False, "error": "couldn't store right now, try again"})
    print(f"[health] user {user}: stored {sorted(metrics.items())}")
    return {"ok": True, "stored": stored}


@app.get("/healthz")
def heartbeat():
    """Public heartbeat and keep-awake target. Deliberately bare — no config, no key flags, no
    model names for strangers to read. The commit SHA is the one exception: without it there is
    no way to tell which build is actually being served, so "did my fix deploy?" is
    unanswerable, and a commit id from a private repo gives away nothing."""
    return {"status": "alive", "version": config.DEPLOYED_COMMIT}


# ---------- boot ----------

def _startup():
    db.restore_from_cloud()   # BEFORE anything opens the DB: a fresh deploy pulls memory back
    db.connect().close()      # create/migrate tables
    db.seed_invites()
    db.purge_expired_sessions()
    import os
    os.makedirs(config.MEDIA_DIR, exist_ok=True)

    if config.PUBLIC_URL:
        messenger.set_webhook(config.PUBLIC_URL)
    else:
        print("[telegram] no PUBLIC_URL — set it (or run ngrok) so Telegram can reach this app")

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler()
        # Registered FIRST and independent of any messaging credential — a missing bot token
        # must never silently take the database backups down with it.
        if config.TURSO_URL:
            sched.add_job(db.backup_to_cloud, "interval", minutes=config.DB_BACKUP_MIN)
            print(f"[backup] cloud snapshot every {config.DB_BACKUP_MIN} min")
        if config.PUBLIC_URL:
            def keep_awake(url=config.PUBLIC_URL.rstrip("/") + "/healthz"):
                import httpx
                try:
                    httpx.get(url, timeout=10)
                except Exception:
                    pass
            sched.add_job(keep_awake, "interval", minutes=10)
            print("[keepalive] self-ping every 10 min")
        # ONE job for every reminder of every user. It asks each user what time it is where
        # they are — which is the only way per-user times and timezones can work at all.
        sched.add_job(reminders.sweep, "interval", minutes=1, max_instances=1,
                      coalesce=True, misfire_grace_time=120)
        sched.add_job(reminders.clear_old_fired_flags, "interval", hours=12)
        sched.add_job(db.purge_expired_sessions, "interval", hours=24)
        sched.start()
        print("[reminders] per-user sweep running every minute")
    except Exception as ex:
        print(f"[reminders] scheduler failed: {ex!r}")


def _shutdown():
    """A host sends a shutdown signal right before a redeploy wipes the disk — push one last
    snapshot so at most seconds, rather than minutes, of anybody's data can be lost."""
    db.backup_to_cloud()
