"""FastAPI application: routes, auth flow, static SPA, startup tasks."""
import asyncio
import hmac
import json
import logging
import os
import re
from datetime import date

import httpx
from fastapi import (Cookie, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature
from itsdangerous.url_safe import URLSafeTimedSerializer
from pathlib import Path
from pydantic import BaseModel, Field

from . import (accounts, auth, backups, colors, config, db, events, logsafe,
               migrations, plex, plex_ratings, seerr, service, stats, tmdb)
from . import settings as app_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, including TMDB's query-string API key.
# Keep integration failures visible through our own module loggers without ever
# copying credentials into routine container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Belt-and-braces: scrub query-string secrets / webhook paths from ALL log
# output, so an error that carries a URL (e.g. TMDB's ?api_key=) can't leak it.
logsafe.install()
log = logging.getLogger("filmclub")

STATIC_DIR = Path(__file__).parent.parent / "static"

# Short-lived signer for carrying the Plex pin across the OAuth redirect.
_pin_signer = URLSafeTimedSerializer(config.EFFECTIVE_SESSION_SECRET, salt="filmclub-pin")
PIN_COOKIE = "filmclub_pin"

app = FastAPI(title="Film Club Tracker")


class BroadcastMiddleware:
    """Pure-ASGI middleware: after any successful state-changing API request,
    notify live SSE clients that something changed.

    Doing it here (rather than in each endpoint) keeps the broadcast in one
    place. It's pure ASGI — not BaseHTTPMiddleware — so it never buffers or
    interferes with the streaming /api/events response; it only inspects the
    response status and passes everything through untouched. The change is
    already committed by the time the response finishes, so clients that
    re-fetch on the ping see fresh data. The originating client id (from the
    X-Client-Id header) is echoed so a client can ignore its own change."""

    _METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        path = scope["path"]
        if (scope["method"] in self._METHODS
                and path.startswith("/api/")
                and path != "/api/events"
                and path != "/api/admin/settings/test"
                and not path.startswith("/api/plex/webhook/")
                and status["code"] < 400):
            headers = dict(scope.get("headers") or [])
            client = (headers.get(b"x-client-id", b"").decode() or None)
            events.broadcast({"path": path, "client": client})


app.add_middleware(BroadcastMiddleware)


async def _backfill_movie_languages() -> None:
    """Populate the new language field for existing TMDB-backed movies.

    This runs once in the background after startup and only selects rows still
    missing a language, so normal restarts do no external work after the first
    successful pass.
    """
    if not config.TMDB_API_KEY:
        return
    conn = db.connect()
    try:
        rows = db.query_all(
            conn,
            "SELECT id, tmdb_id FROM movies WHERE language IS NULL AND tmdb_id IS NOT NULL",
        )
    finally:
        conn.close()
    if not rows:
        return

    semaphore = asyncio.Semaphore(4)

    async def fetch(row):
        async with semaphore:
            try:
                meta = await tmdb.details(row["tmdb_id"])
                return row["id"], meta.get("language")
            except Exception as exc:  # noqa: BLE001 — retry on next restart
                log.warning("Language backfill failed for movie %s: %s", row["id"], exc)
                return row["id"], None

    results = await asyncio.gather(*(fetch(row) for row in rows))
    updates = [(language, movie_id) for movie_id, language in results if language]
    if not updates:
        return
    conn = db.connect()
    try:
        conn.executemany("UPDATE movies SET language = ? WHERE id = ?", updates)
        conn.commit()
    finally:
        conn.close()
    log.info("Backfilled language for %d movies", len(updates))
    events.broadcast({"path": "/api/metadata/languages", "client": None})


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    conn = db.connect()
    try:
        recolored = colors.reconcile_member_colors(conn)
        conn.commit()
    finally:
        conn.close()
    if recolored:
        log.info("Reassigned %d duplicate member colour(s)", recolored)
    app_settings.load_into_config()
    auth.purge_expired_sessions()
    # Preserve the zero-touch upgrade path for already Plex-configured installs.
    # A fresh local-only install stays in the wizard so it can create its owner.
    if (not app_settings.is_setup_complete() and not config.missing_required()
            and config.plex_configured()):
        app_settings.save({}, complete=True)
    if app_settings.is_setup_complete():
        # Upgrade compatibility: preserve an existing installation's admin as
        # owner instead of allowing the next ordinary member login to claim it.
        conn = db.connect()
        try:
            has_owner = db.query_one(conn, "SELECT id FROM members WHERE is_owner=1 LIMIT 1")
            if not has_owner:
                rows = db.query_all(conn, "SELECT id, plex_id, is_admin FROM members ORDER BY id")
                candidate = next((r for r in rows if r["plex_id"] in config.ADMIN_PLEX_IDS), None)
                candidate = candidate or next((r for r in rows if r["is_admin"]), None)
                if candidate:
                    db.execute(conn, "UPDATE members SET is_owner=1, is_admin=1 WHERE id=?",
                               (candidate["id"],))
        finally:
            conn.close()
    if not app_settings.is_setup_complete():
        code, _ = app_settings.ensure_setup_code()
        if code:
            log.warning("FIRST-RUN SETUP REQUIRED — setup code: %s (valid ~%d min)",
                        code, app_settings.SETUP_CODE_TTL // 60)
        else:
            log.warning("FIRST-RUN SETUP REQUIRED — a setup code is already active; "
                        "restart after it expires to issue a new one")
    missing = config.missing_required()
    if missing:
        log.warning("Missing required env vars: %s", ", ".join(missing))
    if config.DEV_BYPASS_USER:
        log.warning("DEV_BYPASS_USER=%s active — Plex auth is bypassed.",
                    config.DEV_BYPASS_USER)
    if not config.PLEX_WEBHOOK_SECRET:
        log.info("PLEX_WEBHOOK_SECRET not set — Plex-to-Film-Club rating sync is disabled")
    # Kick off Plex library enrichment in the background.
    asyncio.create_task(plex.refresh_loop())
    asyncio.create_task(_backfill_movie_languages())


# --- request models --------------------------------------------------------

class AddMovie(BaseModel):
    tmdb_id: int


class ProfileIn(BaseModel):
    # Chosen display name; blank/whitespace/null clears it (revert to Plex name).
    display_name: str | None = Field(default=None, max_length=40)


class RatingSyncPreferenceIn(BaseModel):
    enabled: bool


class VoteIn(BaseModel):
    voted: bool


class PriorView(BaseModel):
    # true = seen, false = not seen, null = unknown (clears the row)
    seen: bool | None = None


class RatingIn(BaseModel):
    score: float = Field(..., ge=0.5, le=5.0)
    seen_before: bool = False
    note: str | None = None


class MergeIn(BaseModel):
    from_id: int
    into_id: int


class AdminFlagIn(BaseModel):
    is_admin: bool


class DiscussDate(BaseModel):
    date: str  # ISO 'YYYY-MM-DD'


class SettingsIn(BaseModel):
    setup_code: str | None = None
    clear_secrets: list[str] = Field(default_factory=list)
    TMDB_API_KEY: str | None = None
    PLEX_URL: str | None = None
    PLEX_TOKEN: str | None = None
    PLEX_MACHINE_ID: str | None = None
    APP_URL: str | None = None
    PLEX_WEBHOOK_SECRET: str | None = None
    PLEX_REFRESH_INTERVAL: int | None = Field(default=None, ge=60, le=86400)
    SEERR_URL: str | None = None
    SEERR_API_KEY: str | None = None
    SEERR_TIMEOUT: float | None = Field(default=None, ge=1, le=120)


class SetupOwnerIn(BaseModel):
    setup_code: str
    username: str
    password: str


def _settings_values(body: SettingsIn) -> dict:
    return body.model_dump(exclude={"setup_code", "clear_secrets"}, exclude_none=True)


def _settings_candidate(body: SettingsIn) -> dict:
    """Return effective form changes without persisting them.

    Blank secret inputs mean "keep the current value" unless the optional
    secret's explicit clear checkbox was selected. Both save and connection-test
    routes use this so they evaluate the same candidate configuration.
    """
    values = _settings_values(body)
    clear = {
        key for key in body.clear_secrets
        if key in app_settings.FIELDS and app_settings.FIELDS[key]["secret"]
        and not app_settings.FIELDS[key]["required"]
    }
    values = {
        key: value for key, value in values.items()
        if not (app_settings.FIELDS[key]["secret"] and value == "" and key not in clear)
    }
    values.update({key: "" for key in clear})
    return values


async def _test_settings_connections(
    values: dict, *, require_all: bool,
) -> tuple[dict[str, str], list[dict]]:
    """Validate candidate settings and test integrations without saving them."""
    merged = {key: values.get(key, getattr(config, key, ""))
              for key in app_settings.FIELDS}
    errors: dict[str, str] = {}
    if require_all:
        for key, meta in app_settings.FIELDS.items():
            if meta["required"] and not str(merged.get(key) or "").strip():
                errors[key] = "Required"
    for key in ("PLEX_URL", "APP_URL", "SEERR_URL"):
        value = str(merged.get(key) or "").strip()
        if value and not re.match(r"^https?://[^\s]+$", value):
            errors[key] = "Enter a complete http:// or https:// URL"

    checks = [{
        "id": "app_url",
        "label": "Application URL",
        "status": "error" if "APP_URL" in errors else "ok",
        "detail": errors.get("APP_URL", "URL format is valid"),
    }]

    plex_values = {key: str(merged.get(key) or "").strip()
                   for key in ("PLEX_URL", "PLEX_TOKEN", "PLEX_MACHINE_ID")}
    if any(plex_values.values()) and not all(plex_values.values()):
        for key, value in plex_values.items():
            if not value:
                errors[key] = "Required when Plex is enabled"

    async def test_tmdb() -> dict:
        if not str(merged.get("TMDB_API_KEY") or "").strip():
            return {"id": "tmdb", "label": "TMDB", "status": "error",
                    "detail": "API key is required"}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    "https://api.themoviedb.org/3/configuration",
                    params={"api_key": merged["TMDB_API_KEY"]},
                )
                response.raise_for_status()
            return {"id": "tmdb", "label": "TMDB", "status": "ok",
                    "detail": "Connected and API key accepted"}
        except Exception:
            return {"id": "tmdb", "label": "TMDB", "status": "error",
                    "detail": "API key rejected or service unreachable"}

    async def test_plex() -> dict:
        if not any(plex_values.values()):
            return {"id": "plex", "label": "Plex", "status": "skipped",
                    "detail": "Not configured"}
        if not all(plex_values.values()) or "PLEX_URL" in errors:
            return {"id": "plex", "label": "Plex", "status": "error",
                    "detail": "URL, token, and machine identifier are required"}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{plex_values['PLEX_URL'].rstrip('/')}/identity",
                    headers={"X-Plex-Token": plex_values["PLEX_TOKEN"],
                             "Accept": "application/json"},
                )
                response.raise_for_status()
                actual = str(response.json().get("MediaContainer", {}).get(
                    "machineIdentifier") or "")
                if actual != plex_values["PLEX_MACHINE_ID"]:
                    return {"id": "plex", "label": "Plex", "status": "error",
                            "detail": "Connected, but machine identifier does not match"}
            return {"id": "plex", "label": "Plex", "status": "ok",
                    "detail": "Connected, authenticated, and machine identifier matched"}
        except Exception:
            return {"id": "plex", "label": "Plex", "status": "error",
                    "detail": "Could not authenticate to this Plex server"}

    seerr_url = str(merged.get("SEERR_URL") or "").strip().rstrip("/")
    seerr_key = str(merged.get("SEERR_API_KEY") or "").strip()
    if bool(seerr_url) != bool(seerr_key):
        if not seerr_url:
            errors["SEERR_URL"] = "Required when Seerr is enabled"
        if not seerr_key:
            errors["SEERR_API_KEY"] = "Required when Seerr is enabled"

    async def test_seerr() -> dict:
        if not seerr_url and not seerr_key:
            return {"id": "seerr", "label": "Seerr", "status": "skipped",
                    "detail": "Not configured"}
        if (not seerr_url or not seerr_key
                or "SEERR_URL" in errors or "SEERR_API_KEY" in errors):
            return {"id": "seerr", "label": "Seerr", "status": "error",
                    "detail": "URL and API key are both required"}
        try:
            timeout = float(merged.get("SEERR_TIMEOUT") or 10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{seerr_url}/api/v1/settings/main",
                    headers={"X-Api-Key": seerr_key, "Accept": "application/json"},
                )
                response.raise_for_status()
            return {"id": "seerr", "label": "Seerr", "status": "ok",
                    "detail": "Connected and API key accepted"}
        except Exception:
            return {"id": "seerr", "label": "Seerr", "status": "error",
                    "detail": "API key rejected or service unreachable"}

    integration_checks = await asyncio.gather(test_tmdb(), test_plex(), test_seerr())
    checks.extend(integration_checks)
    for check in integration_checks:
        if check["status"] != "error":
            continue
        if check["id"] == "tmdb":
            errors.setdefault("TMDB_API_KEY", "TMDB rejected this key or could not be reached")
        elif check["id"] == "plex":
            field = "PLEX_MACHINE_ID" if "machine identifier" in check["detail"] else "PLEX_URL"
            errors.setdefault(field, check["detail"])
        elif check["id"] == "seerr":
            errors.setdefault("SEERR_URL", check["detail"])
    return errors, checks


async def _validate_settings(values: dict, *, require_all: bool) -> dict[str, str]:
    errors, _ = await _test_settings_connections(values, require_all=require_all)
    return errors


def _validate_score(score: float) -> float:
    # 0.5 .. 5.0 in 0.5 increments
    steps = round(score * 2)
    if steps < 1 or steps > 10 or abs(steps / 2 - score) > 1e-9:
        raise HTTPException(status_code=400, detail="score must be 0.5–5.0 in 0.5 steps")
    return steps / 2


# --- auth routes -----------------------------------------------------------

@app.get("/api/setup/status")
async def api_setup_status():
    complete = app_settings.is_setup_complete()
    conn = db.connect()
    try:
        has_owner = db.query_one(conn, "SELECT id FROM members WHERE is_owner = 1 LIMIT 1")
        has_members = db.query_one(conn, "SELECT id FROM members LIMIT 1")
    finally:
        conn.close()
    return {
        "required": not complete,
        "owner_required": not complete and not bool(has_members),
        "owner_exists": bool(has_owner),
        "plex_enabled": config.plex_configured(),
        "settings": app_settings.public_values(reveal_nonsecrets=False) if not complete else None,
    }


@app.post("/api/setup/owner")
async def api_setup_owner(body: SetupOwnerIn):
    if app_settings.is_setup_complete():
        raise HTTPException(status_code=409, detail="Setup is already complete")
    result = app_settings.verify_setup_code(body.setup_code)
    if result == "locked":
        raise HTTPException(status_code=429,
                            detail="Too many attempts — wait a minute and try again")
    if result != "ok":
        raise HTTPException(status_code=403, detail="Invalid or expired setup code")
    try:
        member_id = accounts.create_first_owner(body.username, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    resp = JSONResponse({"ok": True, "next": "#/setup"})
    _issue_session(resp, member_id)
    return resp


@app.post("/api/setup")
async def api_setup(body: SettingsIn):
    if app_settings.is_setup_complete():
        raise HTTPException(status_code=409, detail="Setup is already complete")
    result = app_settings.verify_setup_code(body.setup_code or "")
    if result == "locked":
        raise HTTPException(status_code=429,
                            detail="Too many attempts — wait a minute and try again")
    if result != "ok":
        raise HTTPException(status_code=403, detail="Invalid or expired setup code")
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM members WHERE is_owner = 1 LIMIT 1"):
            raise HTTPException(status_code=409, detail="Create the owner account first")
    finally:
        conn.close()
    values = _settings_values(body)
    errors = await _validate_settings(values, require_all=True)
    if errors:
        return JSONResponse({"detail": "Check the highlighted settings",
                             "errors": errors}, status_code=422)
    app_settings.save(values, complete=True)
    app_settings.remove_setup_code()
    if config.plex_configured():
        await plex.refresh_library()
    return {"ok": True, "next": "/"}


@app.get("/auth/login")
async def auth_login():
    if not app_settings.is_setup_complete():
        return RedirectResponse("/#/setup")
    if config.DEV_BYPASS_USER:
        return RedirectResponse("/")
    if not config.plex_configured():
        raise HTTPException(status_code=503, detail="Plex login is not configured")
    return await _start_plex_auth()


@app.get("/auth/plex/link")
async def auth_plex_link(member=Depends(auth.current_member)):
    if not config.plex_configured():
        raise HTTPException(status_code=503, detail="Plex is not configured")
    return await _start_plex_auth(link_member_id=member["id"])


async def _start_plex_auth(link_member_id: int | None = None):
    try:
        pin = await plex.create_pin()
    except Exception as e:  # noqa: BLE001
        log.error("Failed to create Plex pin: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach Plex to start login")
    resp = RedirectResponse(plex.auth_url(pin["code"]))
    token = _pin_signer.dumps({
        "id": pin["id"], "code": pin["code"], "link_member_id": link_member_id,
    })
    resp.set_cookie(PIN_COOKIE, token, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request):
    raw = request.cookies.get(PIN_COOKIE)
    if not raw:
        return _login_error("Login session expired. Please try again.")
    try:
        pin = _pin_signer.loads(raw, max_age=600)
    except BadSignature:
        return _login_error("Login session invalid. Please try again.")

    token = await plex.poll_pin(pin["id"], pin["code"])
    if not token:
        return _login_error("Timed out waiting for Plex authorisation. Please try again.")

    # Authorisation: a valid Plex account is not enough — require access to
    # OUR server. This check is not optional.
    if not await plex.has_server_access(token):
        return _login_error(
            "You authenticated with Plex, but you don't have access to this "
            "Plex server, so you can't use the film club tracker.")

    identity = await plex.get_user(token)
    link_member_id = pin.get("link_member_id")
    if link_member_id is not None:
        session = auth.resolve_session(request.cookies.get(config.SESSION_COOKIE))
        if not session or session["id"] != link_member_id:
            return _login_error("Your Film Club session expired. Sign in and try linking again.")
        try:
            accounts.link_plex_identity(
                link_member_id,
                plex_id=identity["uuid"],
                username=identity["username"],
                email=identity.get("email"),
                thumb=identity.get("thumb"),
                plex_account_id=identity.get("account_id"),
                plex_token=token,
            )
        except accounts.AccountError as exc:
            return _login_error(str(exc))
        resp = RedirectResponse("/#/profile")
        resp.delete_cookie(PIN_COOKIE)
        return resp

    # Retain an encrypted server-side copy for this member's future Plex rating
    # writes. The raw token never enters the signed browser session.
    member = auth.upsert_member(
        plex_id=identity["uuid"],
        username=identity["username"],
        email=identity.get("email"),
        thumb=identity.get("thumb"),
        plex_account_id=identity.get("account_id"),
        plex_token=token,
    )

    resp = RedirectResponse("/")
    resp.delete_cookie(PIN_COOKIE)
    _issue_session(resp, member["id"])
    return resp


def _issue_session(resp, member_id: int) -> None:
    """Attach a fresh server-side session cookie to a response."""
    resp.set_cookie(
        config.SESSION_COOKIE,
        auth.create_session(member_id),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=config.APP_URL.startswith("https"),
    )


@app.post("/auth/logout")
async def auth_logout(filmclub_session: str | None = Cookie(default=None)):
    auth.revoke_session(filmclub_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


# --- local invite-only accounts --------------------------------------------

class InviteCreate(BaseModel):
    email: str | None = None
    ttl_hours: int = Field(default=accounts.DEFAULT_INVITE_TTL_HOURS, ge=1, le=720)


class LocalCredentials(BaseModel):
    code: str | None = None
    username: str
    password: str


class LoginCredentials(BaseModel):
    username: str
    password: str


class PasswordResetIn(BaseModel):
    token: str
    password: str


class PasswordResetCreate(BaseModel):
    ttl_hours: int = Field(default=accounts.DEFAULT_RESET_TTL_HOURS, ge=1, le=168)


@app.post("/api/admin/invites")
async def api_create_invite(body: InviteCreate, admin=Depends(auth.require_admin)):
    invite = accounts.create_invite(admin["id"], ttl_hours=body.ttl_hours, email=body.email)
    return {
        "id": invite["id"],
        "code": invite["code"],
        "expires_at": invite["expires_at"],
        "invite_url": f"{config.APP_URL}/#/invite/{invite['code']}",
    }


@app.get("/api/admin/invites")
async def api_list_invites(admin=Depends(auth.require_admin)):
    return {"invites": accounts.list_invites()}


@app.post("/api/admin/members/{member_id}/password-reset")
async def api_create_password_reset(member_id: int, body: PasswordResetCreate,
                                    admin=Depends(auth.require_admin)):
    try:
        reset = accounts.create_password_reset(
            admin["id"], member_id, ttl_hours=body.ttl_hours
        )
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "expires_at": reset["expires_at"],
        "reset_url": f"{config.APP_URL}/#/reset/{reset['token']}",
    }


@app.get("/auth/local/invite/{code}")
async def api_invite_status(code: str):
    return accounts.invite_status(code)


@app.post("/auth/local/register")
async def api_local_register(body: LocalCredentials):
    try:
        member_id = accounts.redeem_invite(body.code or "", body.username, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    resp = JSONResponse({"ok": True, "next": "/"})
    _issue_session(resp, member_id)
    return resp


@app.post("/auth/local/login")
async def api_local_login(body: LoginCredentials):
    member_id = accounts.authenticate_local(body.username, body.password)
    if not member_id:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    resp = JSONResponse({"ok": True, "next": "/"})
    _issue_session(resp, member_id)
    return resp


@app.get("/auth/local/reset/{token}")
async def api_password_reset_status(token: str):
    return accounts.password_reset_status(token)


@app.post("/auth/local/reset")
async def api_password_reset(body: PasswordResetIn):
    try:
        member_id = accounts.redeem_password_reset(body.token, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    resp = JSONResponse({"ok": True, "next": "/"})
    _issue_session(resp, member_id)
    return resp


def _login_error(message: str) -> HTMLResponse:
    """Minimal standalone error page with a retry link."""
    html = f"""<!doctype html><html><head><meta charset="utf-8">
    <title>Film Club — sign in</title>
    <style>body{{background:#0d0d10;color:#e8e8ea;font-family:system-ui,sans-serif;
    display:grid;place-items:center;height:100vh;margin:0}}
    .box{{max-width:26rem;text-align:center;padding:2rem}}
    a{{color:#5b8def}}</style></head>
    <body><div class="box"><h1>Can't sign you in</h1>
    <p>{message}</p><p><a href="/auth/login">Try again</a></p></div></body></html>"""
    return HTMLResponse(html, status_code=403)


# --- API routes ------------------------------------------------------------

@app.get("/api/me")
async def api_me(member=Depends(auth.current_member)):
    return auth.with_connection_status(member)


@app.patch("/api/me")
async def api_update_me(body: ProfileIn, member=Depends(auth.current_member)):
    """Update the current member's profile (currently just the display name)."""
    conn = db.connect()
    try:
        service.set_display_name(conn, member["id"], body.display_name)
        row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member["id"],))
        return auth.with_connection_status(db.member_public(row))
    finally:
        conn.close()


@app.patch("/api/me/plex-rating-sync")
async def api_update_rating_sync(body: RatingSyncPreferenceIn,
                                 member=Depends(auth.current_member)):
    """Enable or pause future Plex rating synchronization for this member."""
    conn = db.connect()
    try:
        service.set_plex_rating_sync_enabled(conn, member["id"], body.enabled)
        row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member["id"],))
        return auth.with_connection_status(db.member_public(row))
    finally:
        conn.close()


@app.get("/api/me/todo")
async def api_me_todo(member=Depends(auth.current_member)):
    """Per-member reminder counts for the nav badges."""
    conn = db.connect()
    try:
        return service.todo_counts(conn, member["id"])
    finally:
        conn.close()


@app.get("/api/events")
async def api_events(request: Request, member=Depends(auth.current_member)):
    """Server-Sent Events stream of 'something changed' pings for live updates.

    Clients re-fetch the relevant view when they receive an event. Each event
    carries the originating client id so a client ignores the echo of its own
    action (it already updated optimistically)."""
    async def event_stream():
        q = events.subscribe()
        try:
            # Prime the stream so proxies flush the response headers immediately.
            yield ": connected\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat: keeps the connection alive through Cloudflare's
                    # ~100s idle timeout when nothing is happening.
                    yield ": keepalive\n\n"
                if await request.is_disconnected():
                    break
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # ask any proxy not to buffer the stream
        },
    )


@app.get("/api/members")
async def api_members(member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        return service.all_members(conn)
    finally:
        conn.close()


@app.get("/api/members/{member_id}/profile")
async def api_member_profile(member_id: int, member=Depends(auth.current_member)):
    """A member's public film-club activity page."""
    conn = db.connect()
    try:
        prof = service.member_profile(conn, member_id, member["id"])
        if not prof:
            raise HTTPException(status_code=404, detail="Member not found")
        return prof
    finally:
        conn.close()


@app.get("/api/backlog")
async def api_backlog(
    member=Depends(auth.current_member),
    sort: str = Query("seconds"),
    eligible_only: bool = Query(False),
):
    conn = db.connect()
    try:
        return {"items": service.backlog(conn, member["id"], sort=sort,
                                         eligible_only=eligible_only),
                "me": member}
    finally:
        conn.close()


@app.get("/api/thisweek")
async def api_thisweek(member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        return {"items": service.this_week(conn), "me": member}
    finally:
        conn.close()


@app.get("/api/watched")
async def api_watched(member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        return {"items": service.watched(conn, member["id"])}
    finally:
        conn.close()


@app.get("/api/movies/{movie_id}")
async def api_movie(movie_id: int, member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        detail = service.movie_detail(conn, movie_id, member["id"])
        if not detail:
            raise HTTPException(status_code=404, detail="Movie not found")
        detail["my_rating_default"] = {
            "seen_before": service.default_seen_before(conn, movie_id, member["id"]),
        }
        return detail
    finally:
        conn.close()


@app.post("/api/movies")
async def api_add_movie(body: AddMovie, member=Depends(auth.current_member)):
    try:
        meta = await tmdb.details(body.tmdb_id)
    except Exception as e:  # noqa: BLE001
        log.error("TMDB details failed for %s: %s", body.tmdb_id, e)
        raise HTTPException(status_code=502, detail="Could not fetch film metadata from TMDB")
    conn = db.connect()
    try:
        existing = db.query_one(
            conn, "SELECT id, status FROM movies WHERE tmdb_id = ?", (body.tmdb_id,))
        if existing:
            raise HTTPException(status_code=409,
                                detail="That film is already on the list")
        movie_id = service.add_suggestion(conn, meta, member["id"])
        # Auto-request from Seerr if the film isn't already on Plex. This runs
        # AFTER the insert and never raises, so the add itself can't be broken by
        # a Seerr/Plex hiccup — worst case the film is added with status 'failed'.
        seerr_result = await _maybe_request_from_seerr(conn, movie_id, meta)
        return {"id": movie_id, "seerr": seerr_result}
    finally:
        conn.close()


async def _maybe_request_from_seerr(conn, movie_id: int, meta: dict) -> dict:
    """Decide presence and (if missing) request from Seerr. Returns a status dict.

    Existence check is cache-first: a positive `library_match` is trusted (a GUID
    in the set means it's really on Plex). On a cache miss we do one targeted live
    Plex lookup to catch films added since the last interval refresh, and only
    request from Seerr if that also misses. Never raises."""
    if not seerr.is_configured():
        return {"status": "disabled"}

    tmdb_id, imdb_id = meta.get("tmdb_id"), meta.get("imdb_id")
    in_library = bool(plex.library_match(tmdb_id, imdb_id))
    if not in_library:
        in_library = await plex.library_has_live(tmdb_id, imdb_id, meta.get("title"))

    if in_library:
        result = {"status": "in_library"}
    else:
        result = await seerr.request_movie(tmdb_id)

    try:
        service.set_seerr_status(conn, movie_id, result["status"])
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        log.warning("Could not store seerr_status for movie %s: %s", movie_id, e)
    return result


@app.post("/api/movies/{movie_id}/schedule")
async def api_schedule(movie_id: int, member=Depends(auth.current_member)):
    """Pick a backlog film as this week's movie (suggested -> scheduled)."""
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        changed = service.schedule_movie(conn, movie_id)
        return {"ok": True, "changed": changed}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/unschedule")
async def api_unschedule(movie_id: int, member=Depends(auth.current_member)):
    """Send this week's movie back to the backlog (scheduled -> suggested)."""
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        changed = service.unschedule_movie(conn, movie_id)
        return {"ok": True, "changed": changed}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/discuss_date")
async def api_discuss_date(movie_id: int, body: DiscussDate, member=Depends(auth.current_member)):
    """Change the discussion date for this week's pick."""
    try:
        d = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        if not service.set_discuss_date(conn, movie_id, d.isoformat()):
            raise HTTPException(status_code=400,
                                detail="Only this week's pick has a discussion date")
        return {"ok": True, "date": d.isoformat()}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/watch")
async def api_archive(movie_id: int, member=Depends(auth.current_member)):
    """Close out this week's movie into the archive (scheduled -> watched)."""
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        changed = service.archive_movie(conn, movie_id)
        return {"ok": True, "changed": changed}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/unwatch")
async def api_unmark_watched(movie_id: int, member=Depends(auth.current_member)):
    """Send an archived film back to the backlog (watched -> suggested)."""
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        changed = service.unmark_watched(conn, movie_id)
        return {"ok": True, "changed": changed}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/return-to-this-week")
async def api_return_to_this_week(movie_id: int,
                                  admin=Depends(auth.require_admin)):
    """Reopen an archived film as this week's pick without deleting any data."""
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        changed = service.return_to_this_week(conn, movie_id)
        return {"ok": True, "changed": changed}
    finally:
        conn.close()


@app.delete("/api/movies/{movie_id}")
async def api_delete_movie(movie_id: int, member=Depends(auth.current_member)):
    """Delete a backlog film. Allowed for an admin or the member who added it,
    and only while the film is still in the backlog (not scheduled/watched)."""
    conn = db.connect()
    try:
        mv = db.query_one(
            conn, "SELECT status, suggested_by FROM movies WHERE id = ?", (movie_id,))
        if not mv:
            raise HTTPException(status_code=404, detail="Movie not found")
        if mv["status"] != "suggested":
            raise HTTPException(status_code=400,
                                detail="Only backlog films can be deleted")
        if not (member.get("is_admin") or mv["suggested_by"] == member["id"]):
            raise HTTPException(status_code=403,
                                detail="You can only delete films you added")
        service.delete_movie(conn, movie_id)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/prior_view")
async def api_prior_view(movie_id: int, body: PriorView, member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="Movie not found")
        service.set_prior_view(conn, movie_id, member["id"], body.seen)
        # Return refreshed coverage so the card can update in place.
        return {"coverage": service.coverage_for(conn, movie_id)}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/vote")
async def api_vote(movie_id: int, body: VoteIn, member=Depends(auth.current_member)):
    """Second (or un-second) a backlog film. The suggester can't vote for their
    own, and only backlog films can be seconded."""
    conn = db.connect()
    try:
        mv = db.query_one(
            conn, "SELECT status, suggested_by FROM movies WHERE id = ?", (movie_id,))
        if not mv:
            raise HTTPException(status_code=404, detail="Movie not found")
        if mv["status"] != "suggested":
            raise HTTPException(status_code=400,
                                detail="Only backlog films can be seconded")
        if mv["suggested_by"] == member["id"]:
            raise HTTPException(status_code=400,
                                detail="You can't second a film you suggested")
        count = service.set_vote(conn, movie_id, member["id"], body.voted)
        return {"vote_count": count, "voted": body.voted}
    finally:
        conn.close()


@app.post("/api/movies/{movie_id}/rating")
async def api_rate(movie_id: int, body: RatingIn, member=Depends(auth.current_member)):
    score = _validate_score(body.score)
    conn = db.connect()
    try:
        mv = db.query_one(conn, "SELECT * FROM movies WHERE id = ?", (movie_id,))
        if not mv:
            raise HTTPException(status_code=404, detail="Movie not found")
        # Ratings open once a film is picked (members rate through the week) and
        # stay open in the archive.
        if mv["status"] not in ("scheduled", "watched"):
            raise HTTPException(status_code=400,
                                detail="Can only rate this week's or watched films")
        service.upsert_rating(conn, movie_id, member["id"], score,
                              body.seen_before, (body.note or "").strip() or None)
        movie = dict(mv)
    finally:
        conn.close()
    plex_sync = await plex_ratings.push_rating(movie, member["id"], score)
    return {"ok": True, "plex": plex_sync}


@app.post("/api/plex/webhook/{secret}")
async def api_plex_webhook(secret: str, payload: str = Form(...)):
    """Receive Plex `media.rate` webhooks for inbound rating synchronization."""
    expected = config.PLEX_WEBHOOK_SECRET
    if not expected or not hmac.compare_digest(secret, expected):
        # Use 404 rather than revealing whether a webhook secret is configured.
        raise HTTPException(status_code=404, detail="Not found")
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid Plex webhook payload")
    conn = db.connect()
    try:
        result = await plex_ratings.apply_webhook(conn, body)
    finally:
        conn.close()
    if result.get("status") == "updated":
        # The webhook path contains a secret, so never pass it through the
        # generic middleware event. Broadcast only a non-sensitive marker.
        events.broadcast({"path": "/api/plex/rating-sync", "client": None})
    return result


@app.get("/api/tmdb/search")
async def api_tmdb_search(q: str, member=Depends(auth.current_member)):
    try:
        return {"results": await tmdb.search(q)}
    except Exception as e:  # noqa: BLE001
        log.error("TMDB search failed: %s", e)
        raise HTTPException(status_code=502, detail="TMDB search failed")


@app.get("/api/tmdb/movies/{tmdb_id}")
async def api_tmdb_movie_preview(
    tmdb_id: int,
    member=Depends(auth.current_member),
):
    """Return full TMDB metadata for an add-suggestion preview."""
    try:
        return await tmdb.details(tmdb_id)
    except Exception as e:  # noqa: BLE001
        log.error("TMDB preview failed for %s: %s", tmdb_id, e)
        raise HTTPException(status_code=502, detail="Could not fetch film details from TMDB")


@app.get("/api/stats")
async def api_stats(member=Depends(auth.current_member)):
    conn = db.connect()
    try:
        return stats.compute(conn)
    finally:
        conn.close()


# --- admin routes (owner only) ---------------------------------------------

@app.get("/api/admin/settings")
async def api_admin_settings(admin=Depends(auth.require_admin)):
    return {"settings": app_settings.public_values()}


@app.post("/api/admin/settings/test")
async def api_admin_test_settings(body: SettingsIn,
                                  admin=Depends(auth.require_admin)):
    """Test the unsaved form values against configured external services."""
    values = _settings_candidate(body)
    errors, checks = await _test_settings_connections(values, require_all=True)
    return {"ok": not errors, "checks": checks, "errors": errors}


@app.put("/api/admin/settings")
async def api_admin_update_settings(body: SettingsIn,
                                    admin=Depends(auth.require_admin)):
    values = _settings_candidate(body)
    locked = [key for key in values if os.environ.get(key, "").strip()]
    if locked:
        raise HTTPException(status_code=409,
                            detail=f"Set by environment and cannot be changed here: {', '.join(locked)}")
    errors = await _validate_settings(values, require_all=True)
    if errors:
        return JSONResponse({"detail": "Check the highlighted settings",
                             "errors": errors}, status_code=422)
    app_settings.save(values)
    await plex.refresh_library()
    return {"ok": True, "settings": app_settings.public_values()}


@app.get("/api/admin/backup")
async def api_admin_backup(admin=Depends(auth.require_admin)):
    """Download one portable archive containing all persistent application data."""
    try:
        payload, filename = backups.create_archive()
    except backups.BackupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("Admin %s created a portable backup", admin["id"])
    return StreamingResponse(
        iter([payload]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/admin/backup/restore")
async def api_admin_restore_backup(
    backup_file: UploadFile = File(...),
    confirmation: str = Form(...),
    admin=Depends(auth.require_admin),
):
    """Replace live data with a verified backup and force every user to re-login."""
    if confirmation.strip() != backups.RESTORE_CONFIRMATION:
        raise HTTPException(status_code=400, detail='Type "RESTORE" to confirm')
    payload = await backup_file.read(backups.MAX_ARCHIVE_BYTES + 1)
    await backup_file.close()
    if len(payload) > backups.MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="The backup file is too large")
    try:
        result = backups.restore_archive(payload)
        app_settings.load_into_config()
    except backups.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Backup restore failed after validation")
        raise HTTPException(status_code=500, detail="Restore failed; existing data was kept") from exc
    log.warning(
        "Admin %s restored a backup; all sessions were revoked; safety copy: %s",
        admin["id"], result["safety_backup"],
    )
    # Refresh optional enrichment against the restored settings without delaying
    # the restore response or making core recovery depend on Plex availability.
    asyncio.create_task(plex.refresh_library())
    return {"ok": True, **result}

@app.post("/api/admin/security/logout-all")
async def api_admin_logout_all(admin=Depends(auth.require_admin)):
    """Revoke every server-side session, forcing all members (including the
    caller) to sign in again. Useful after rotating a credential."""
    revoked = auth.revoke_all_sessions()
    log.warning("Admin %s revoked all sessions (%d removed)", admin["id"], revoked)
    return {"ok": True, "revoked": revoked}


@app.get("/api/admin/members")
async def api_admin_members(admin=Depends(auth.require_admin)):
    conn = db.connect()
    try:
        return {"members": service.admin_members(conn), "me": admin}
    finally:
        conn.close()


@app.post("/api/admin/merge")
async def api_admin_merge(body: MergeIn, admin=Depends(auth.require_admin)):
    conn = db.connect()
    try:
        result = service.merge_members(conn, body.from_id, body.into_id)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/api/admin/members/{member_id}/admin")
async def api_admin_set_admin(member_id: int, body: AdminFlagIn, admin=Depends(auth.require_admin)):
    conn = db.connect()
    try:
        service.set_member_admin(conn, member_id, body.is_admin)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.delete("/api/admin/movies/{movie_id}")
async def api_admin_delete_movie(movie_id: int, admin=Depends(auth.require_admin)):
    conn = db.connect()
    try:
        if not service.delete_movie(conn, movie_id):
            raise HTTPException(status_code=404, detail="Movie not found")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/admin/refresh_library")
async def api_admin_refresh_library(admin=Depends(auth.require_admin)):
    await plex.refresh_library()
    return {"ok": True}


@app.get("/api/admin/diagnostics")
async def api_admin_diagnostics(admin=Depends(auth.require_admin)):
    """Operational diagnostics for admins: app/schema version, database health,
    backup status, and which integrations are enabled. Never returns secret
    values — only booleans, counts, and names."""
    conn = db.connect()
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("members", "movies", "ratings", "votes", "prior_views")}
    finally:
        conn.close()

    backups_dir = config.DATA_DIR / "backups"
    backup_files = sorted(backups_dir.glob("filmclub-*.db")) if backups_dir.exists() else []

    return {
        "app_version": config.APP_VERSION,
        "schema_version": migrations.current_version(config.DB_PATH),
        "schema_latest": migrations.latest_version(),
        "database": {
            "integrity": integrity,
            "foreign_key_violations": fk_violations,
            "counts": counts,
        },
        "backups": {
            "count": len(backup_files),
            "latest": backup_files[-1].name if backup_files else None,
        },
        "integrations": {
            "tmdb": bool(config.TMDB_API_KEY),
            "plex": bool(config.PLEX_URL and config.PLEX_TOKEN and config.PLEX_MACHINE_ID),
            "plex_rating_webhook": bool(config.PLEX_WEBHOOK_SECRET),
            "seerr": bool(getattr(config, "SEERR_URL", "") and getattr(config, "SEERR_API_KEY", "")),
        },
    }


# --- SPA + static ----------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    """Liveness: the process can answer HTTP. Non-sensitive, unauthenticated."""
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    """Readiness: the database is reachable and migrated to the latest schema,
    and the signing secret is durable. Reports only names/status — never secret
    values. Returns 503 until the app can actually serve its purpose."""
    checks: dict = {"version": config.APP_VERSION}
    ready = True

    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        current = migrations.current_version(config.DB_PATH)
        latest = migrations.latest_version()
        checks["database"] = "ok"
        checks["schema_version"] = current
        checks["schema_latest"] = latest
        if current != latest:
            ready = False
            checks["schema"] = "behind"
    except Exception:  # noqa: BLE001 — readiness must never leak internals
        ready = False
        checks["database"] = "error"

    # Durable session secret is required for sessions to survive a restart.
    if not config.SESSION_SECRET:
        ready = False
        checks["session_secret"] = "ephemeral"

    # Report (but don't leak) which required env vars are still unset.
    missing = config.missing_required()
    if missing:
        checks["missing_config"] = missing

    return JSONResponse(
        {"ready": ready, **checks},
        status_code=200 if ready else 503,
    )
