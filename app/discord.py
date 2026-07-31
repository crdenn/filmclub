"""Discord weekly reminder digest.

Two responsibilities:
  1. Content assembly: gather this week's scheduled film and, per member,
     which backlog films still need a seen/unseen answer and which watched
     films still need a rating, then format it as a Discord message.
  2. Scheduling: a background loop that posts the digest once per week, on an
     admin-configurable weekday/hour (server-local time), to an incoming
     webhook.

Optional and entirely additive. With DISCORD_WEBHOOK_URL unset the feature is
inert.

Design rules (mirrors app/seerr.py):
  * Never raise into the startup/loop path. Every network call is wrapped and
    degrades to a logged warning; a Discord outage can never break the app.
  * A send failure does NOT mark the week as sent, so the loop retries on its
    next tick (DISCORD_REMINDER_INTERVAL) rather than silently skipping a week.
  * Member Discord ids are admin-entered (Admin > Users), never self-service,
    and — like `theme` — deliberately absent from `service.all_members()` /
    `db.member_public()`, so they're fetched directly here.
  * `send_digest_now()` is the manual-test path (Admin Settings "Send test
    digest" button): it builds and sends exactly like the scheduled path, but
    never touches the last-sent bookkeeping, so testing on a Wednesday can't
    suppress that week's real Monday-equivalent send.
"""
import asyncio
import logging
from datetime import date, datetime

import httpx

from . import config, db, service

log = logging.getLogger("filmclub.discord")

_LAST_SENT_KEY = "_discord_digest_last_sent_week"
_TODO_CAP = 5


def is_configured() -> bool:
    return bool(config.DISCORD_WEBHOOK_URL)


# --- content assembly -------------------------------------------------------

def build_digest(conn) -> dict:
    """Assemble this week's reminder digest content.

    Always returns a full shape (never None) — a quiet week ("nobody's picked
    a film yet", "everyone's caught up") is itself useful information, not a
    reason to suppress the message.
    """
    scheduled = service.this_week(conn)
    members = service.all_members(conn)
    discord_ids = {r["id"]: r["discord_user_id"] for r in db.query_all(
        conn, "SELECT id, discord_user_id FROM members")}

    gaps = []
    for m in members:
        detail = service.todo_details(conn, m["id"], cap=_TODO_CAP)
        if detail["backlog"]["count"] or detail["watched"]["count"]:
            gaps.append({
                "member": m,
                "discord_user_id": discord_ids.get(m["id"]),
                "backlog": detail["backlog"],
                "watched": detail["watched"],
            })
    return {"scheduled": scheduled, "gaps": gaps}


def _mention(member: dict, discord_user_id: str | None) -> str:
    return f"<@{discord_user_id}>" if discord_user_id else member["username"]


def _fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%A, %b %-d")
    except ValueError:
        return iso


def _gap_line(entry: dict, bucket: str) -> str | None:
    detail = entry[bucket]
    if not detail["count"]:
        return None
    titles = ", ".join(detail["titles"])
    if detail["overflow"]:
        titles += f" (+{detail['overflow']} more)"
    who = _mention(entry["member"], entry["discord_user_id"])
    verb = "hasn't marked" if bucket == "backlog" else "hasn't rated"
    return f"• {who} — {verb}: {titles}"


def _format_message(digest: dict) -> dict:
    """Pure formatting: digest dict -> Discord webhook payload. No I/O."""
    lines = ["**Film Club — this week**"]
    if digest["scheduled"]:
        for mv in digest["scheduled"]:
            when = f" — discussing {_fmt_date(mv['watched_at'])}" if mv.get("watched_at") else ""
            year = f" ({mv['year']})" if mv.get("year") else ""
            lines.append(f"\U0001F3AC *{mv['title']}*{year}{when}")
    else:
        lines.append("No film picked yet for this week — go pick one!")

    backlog_lines = [ln for e in digest["gaps"] if (ln := _gap_line(e, "backlog"))]
    watched_lines = [ln for e in digest["gaps"] if (ln := _gap_line(e, "watched"))]

    lines.append("")
    lines.append("**Backlog check-in** (mark seen/unseen)")
    lines.extend(backlog_lines if backlog_lines else ["Everyone's caught up."])
    lines.append(f"[Open Backlog]({config.APP_URL}/#/backlog)")

    lines.append("")
    lines.append("**Rating check-in** (rate what we watched)")
    lines.extend(watched_lines if watched_lines else ["Everyone's caught up."])
    lines.append(f"[Open Watched]({config.APP_URL}/#/watched)")

    mentioned_ids = sorted({
        e["discord_user_id"] for e in digest["gaps"]
        if e["discord_user_id"] and (e["backlog"]["count"] or e["watched"]["count"])
    })
    return {
        "content": "\n".join(lines),
        "allowed_mentions": {"parse": [], "users": mentioned_ids},
    }


def _format_date_changed_message(title: str, year: int | None, iso_date: str) -> dict:
    """Pure formatting: an immediate, one-off notice that This Week's
    discussion date moved. No I/O."""
    year_str = f" ({year})" if year else ""
    content = (f"\U0001F4C5 **Discussion date changed** — we're meeting to discuss "
               f"*{title}*{year_str} on **{_fmt_date(iso_date)}**.")
    return {"content": content, "allowed_mentions": {"parse": [], "users": []}}


async def notify_date_changed(movie_id: int) -> dict:
    """Post an immediate notice when This Week's discussion date moves.

    Separate from the weekly digest — fires right away, not on the Monday
    schedule. Never raises: a Discord failure must never break the
    date-change request itself.
    """
    if not is_configured():
        return {"status": "disabled", "detail": None}
    try:
        conn = db.connect()
        try:
            row = db.query_one(
                conn, "SELECT title, year, watched_at FROM movies WHERE id = ?", (movie_id,))
        finally:
            conn.close()
        if not row:
            return {"status": "failed", "detail": "movie not found"}
        payload = _format_date_changed_message(row["title"], row["year"], row["watched_at"])
        return await _post(payload)
    except Exception as e:  # noqa: BLE001 — must never break the date-change request
        log.warning("Discord date-changed notice failed: %s", e)
        return {"status": "failed", "detail": str(e)}


# --- sending -----------------------------------------------------------------

async def _post(payload: dict) -> dict:
    if not is_configured():
        return {"status": "disabled", "detail": None}
    try:
        async with httpx.AsyncClient(timeout=config.DISCORD_TIMEOUT) as client:
            r = await client.post(config.DISCORD_WEBHOOK_URL, json=payload)
        if r.status_code in (200, 204):
            return {"status": "sent", "detail": None}
        log.warning("Discord webhook returned %s: %s", r.status_code, r.text[:300])
        return {"status": "failed", "detail": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001 — must never break the reminder loop
        log.warning("Discord webhook post failed: %s", e)
        return {"status": "failed", "detail": str(e)}


# --- weekly scheduling -------------------------------------------------------

def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _already_sent_this_week(conn) -> bool:
    row = db.query_one(conn, "SELECT value FROM app_settings WHERE key = ?", (_LAST_SENT_KEY,))
    return bool(row) and row["value"] == _iso_week(date.today())


def _mark_sent(conn) -> None:
    db.execute(
        conn,
        """INSERT INTO app_settings (key, value, encrypted, updated_at)
           VALUES (?, ?, 0, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (_LAST_SENT_KEY, _iso_week(date.today())),
    )


async def send_digest_now() -> dict:
    """Build and send the digest immediately, bypassing the schedule check —
    the Admin Settings "Send test digest" button. Deliberately does not touch
    the last-sent bookkeeping, so a manual test can never suppress (or
    duplicate) the actual scheduled send for that week."""
    if not is_configured():
        return {"status": "disabled", "detail": None}
    conn = db.connect()
    try:
        digest = build_digest(conn)
    finally:
        conn.close()
    return await _post(_format_message(digest))


async def _maybe_send() -> None:
    now = datetime.now()
    if (not is_configured()
            or now.weekday() != config.DISCORD_REMINDER_WEEKDAY
            or now.hour < config.DISCORD_REMINDER_HOUR):
        return
    conn = db.connect()
    try:
        if _already_sent_this_week(conn):
            return
        digest = build_digest(conn)
        result = await _post(_format_message(digest))
        if result["status"] == "sent":
            _mark_sent(conn)
        else:
            log.warning("Discord digest not sent, will retry next tick: %s", result.get("detail"))
    finally:
        conn.close()


async def reminder_loop() -> None:
    while True:
        try:
            await _maybe_send()
        except Exception as e:  # noqa: BLE001 — the loop must never die
            log.warning("Discord reminder tick failed: %s", e)
        await asyncio.sleep(config.DISCORD_REMINDER_INTERVAL)
