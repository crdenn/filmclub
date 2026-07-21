"""Session handling and the current-user dependency.

Sessions are revocable and server-side. The cookie carries only an opaque random
token; the database stores only its SHA-256 hash and an expiry, so a database
read never yields a usable session and deleting the row revokes it immediately
(logout, admin-wide invalidation). A separately encrypted copy of the Plex token
is retained server-side for per-user rating synchronization.
"""
import hashlib
import logging
import secrets
import sqlite3

from fastapi import Cookie, Depends, HTTPException

from . import config, db, settings
from .colors import available_color
from .token_crypto import decrypt_plex_token, encrypt_plex_token

log = logging.getLogger("filmclub.auth")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(member_id: int) -> str:
    """Create a server-side session and return its opaque cookie token."""
    token = secrets.token_urlsafe(32)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash, member_id, expires_at) "
            "VALUES (?, ?, datetime('now', ?))",
            (_hash_token(token), member_id, f"+{int(config.SESSION_MAX_AGE)} seconds"),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_session(token: str | None) -> sqlite3.Row | None:
    """Return the joined member row for a live session token, or None.

    Refreshes ``last_seen_at`` on a hit; expired tokens resolve to None.
    """
    if not token:
        return None
    conn = db.connect()
    try:
        row = db.query_one(
            conn,
            "SELECT s.id AS session_id, m.* FROM sessions s "
            "JOIN members m ON m.id = s.member_id "
            "WHERE s.token_hash = ? AND s.expires_at > datetime('now')",
            (_hash_token(token),),
        )
        if row is not None:
            db.execute(conn, "UPDATE sessions SET last_seen_at = datetime('now') "
                             "WHERE id = ?", (row["session_id"],))
        return row
    finally:
        conn.close()


def revoke_session(token: str | None) -> None:
    """Revoke a single session (logout)."""
    if not token:
        return
    conn = db.connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
        conn.commit()
    finally:
        conn.close()


def revoke_all_sessions() -> int:
    """Revoke every session (admin-wide invalidation). Returns the count removed."""
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM sessions")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def purge_expired_sessions() -> None:
    """Housekeeping: drop expired session rows. Safe to call on startup."""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        conn.commit()
    finally:
        conn.close()


def upsert_member(plex_id: str, username: str, email: str | None, thumb: str | None,
                  plex_account_id: str | None = None,
                  plex_token: str | None = None) -> dict:
    """Create-or-update a member from a Plex identity. Returns the member dict.

    Colour is assigned deterministically from plex_id and only set on insert so
    it never churns. Username/email/thumb are refreshed on each login so the
    avatar stays current.
    """
    # Accounts on the declarative allowlist are always flagged admin on login.
    force_admin = 1 if plex_id in config.ADMIN_PLEX_IDS else None
    encrypted_token = encrypt_plex_token(plex_token)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Identities are the login boundary. The members.plex_id fallback keeps
        # a partially-upgraded legacy database recoverable and is immediately
        # repaired by inserting the missing identity row below.
        existing = db.query_one(
            conn,
            """SELECT m.* FROM identities i
               JOIN members m ON m.id = i.member_id
               WHERE i.provider = 'plex' AND i.provider_uid = ?""",
            (plex_id,),
        )
        if not existing:
            existing = db.query_one(conn, "SELECT * FROM members WHERE plex_id = ?", (plex_id,))
        if existing:
            conn.execute(
                """INSERT OR IGNORE INTO identities (member_id, provider, provider_uid)
                   VALUES (?, 'plex', ?)""",
                (existing["id"], plex_id),
            )
            conn.execute(
                """UPDATE members SET username = ?, email = ?, thumb = ?,
                   plex_account_id = COALESCE(?, plex_account_id),
                   plex_token_encrypted = COALESCE(?, plex_token_encrypted),
                   is_admin = CASE WHEN ? = 1 THEN 1 ELSE is_admin END
                   WHERE id = ?""",
                (username, email, thumb, plex_account_id, encrypted_token,
                 force_admin or 0, existing["id"]),
            )
            member_id = existing["id"]
        else:
            owner = settings.is_setup_complete() and not bool(db.query_one(
                conn, "SELECT id FROM members WHERE is_owner = 1 LIMIT 1"
            ))
            cur = conn.execute(
                """INSERT INTO members
                   (plex_id, plex_account_id, plex_token_encrypted, username,
                    email, thumb, color, is_admin, is_owner)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (plex_id, plex_account_id, encrypted_token, username, email,
                 thumb, available_color(conn, plex_id), 1 if owner else force_admin or 0,
                 1 if owner else 0),
            )
            member_id = cur.lastrowid
            conn.execute(
                "INSERT INTO identities (member_id, provider, provider_uid) "
                "VALUES (?, 'plex', ?)",
                (member_id, plex_id),
            )
        row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        conn.commit()
        return db.member_public(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dev_bypass_member() -> dict | None:
    """If DEV_BYPASS_USER is set, return (creating if needed) that member."""
    if not config.DEV_BYPASS_USER:
        return None
    plex_id = f"dev:{config.DEV_BYPASS_USER}"
    return upsert_member(plex_id, config.DEV_BYPASS_USER, None, None)


def current_member(filmclub_session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: resolve the logged-in member or 401.

    DEV_BYPASS_USER short-circuits auth entirely for local development.
    """
    if config.DEV_BYPASS_USER:
        m = dev_bypass_member()
        if m:
            return _with_effective_admin(m)

    row = resolve_session(filmclub_session)
    if not row:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # A Plex-origin member with no decryptable token is a legacy identity-only
    # session; require the normal Plex login once to (re)populate their rating
    # credential (which preserves all Film Club data). Local accounts have no
    # Plex token and are exempt.
    if _is_plex_member(row["plex_id"]) and not decrypt_plex_token(row["plex_token_encrypted"]):
        raise HTTPException(status_code=401, detail="Plex reauthentication required")
    return _with_effective_admin(db.member_public(row))


def _is_plex_member(plex_id: str) -> bool:
    """True for a real Plex identity, False for a local account ('local:...') or
    the development bypass ('dev:...')."""
    return not (plex_id.startswith("local:") or plex_id.startswith("dev:"))


def _with_effective_admin(member: dict) -> dict:
    """Admin is true if the DB flag is set OR the account is on the env
    allowlist — so the owner is admin immediately, even before a re-login."""
    if member and (member.get("is_owner") or member.get("plex_id") in config.ADMIN_PLEX_IDS):
        member["is_admin"] = True
    return member


def with_connection_status(member: dict) -> dict:
    """Add a safe current-user-only Plex sync status flag.

    Public member payloads deliberately omit this. The encrypted token and its
    value never leave the server; the profile UI only needs to know whether a
    usable credential exists.
    """
    result = dict(member)
    conn = db.connect()
    try:
        row = db.query_one(
            conn,
            "SELECT plex_token_encrypted, plex_rating_sync_enabled "
            "FROM members WHERE id = ?",
            (member["id"],),
        )
        providers = {
            r["provider"] for r in db.query_all(
                conn, "SELECT provider FROM identities WHERE member_id = ?", (member["id"],)
            )
        }
    finally:
        conn.close()
    result["plex_rating_sync_connected"] = bool(
        row and decrypt_plex_token(row["plex_token_encrypted"])
    )
    result["plex_rating_sync_enabled"] = bool(
        row and row["plex_rating_sync_enabled"]
    )
    result["identity_providers"] = sorted(providers)
    result["plex_available"] = bool(
        config.PLEX_URL and config.PLEX_TOKEN and config.PLEX_MACHINE_ID
    )
    return result


def require_admin(member: dict = Depends(current_member)) -> dict:
    """FastAPI dependency: allow only admins through, else 403."""
    if not member.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return member
