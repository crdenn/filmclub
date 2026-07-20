"""Session handling and the current-user dependency.

Sessions are a signed cookie (itsdangerous). The cookie stores only the local
member id and durable Plex uuid. A separately encrypted copy of the Plex token
is retained server-side for per-user rating synchronization.
"""
import logging

from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from itsdangerous.url_safe import URLSafeTimedSerializer

from . import config, db
from .colors import color_for
from .token_crypto import decrypt_plex_token, encrypt_plex_token

log = logging.getLogger("filmclub.auth")

_serializer = URLSafeTimedSerializer(config.EFFECTIVE_SESSION_SECRET, salt="filmclub-session")


def make_session_cookie(member_id: int, plex_uuid: str) -> str:
    return _serializer.dumps({"mid": member_id, "uuid": plex_uuid})


def read_session_cookie(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


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
        existing = db.query_one(conn, "SELECT * FROM members WHERE plex_id = ?", (plex_id,))
        if existing:
            if force_admin:
                db.execute(
                    conn,
                    """UPDATE members SET username = ?, email = ?, thumb = ?,
                       plex_account_id = COALESCE(?, plex_account_id),
                       plex_token_encrypted = COALESCE(?, plex_token_encrypted),
                       is_admin = 1 WHERE plex_id = ?""",
                    (username, email, thumb, plex_account_id, encrypted_token, plex_id),
                )
            else:
                db.execute(
                    conn,
                    """UPDATE members SET username = ?, email = ?, thumb = ?,
                       plex_account_id = COALESCE(?, plex_account_id),
                       plex_token_encrypted = COALESCE(?, plex_token_encrypted)
                       WHERE plex_id = ?""",
                    (username, email, thumb, plex_account_id, encrypted_token, plex_id),
                )
            row = db.query_one(conn, "SELECT * FROM members WHERE plex_id = ?", (plex_id,))
        else:
            db.execute(
                conn,
                """INSERT INTO members
                   (plex_id, plex_account_id, plex_token_encrypted, username,
                    email, thumb, color, is_admin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (plex_id, plex_account_id, encrypted_token, username, email,
                 thumb, color_for(plex_id), force_admin or 0),
            )
            row = db.query_one(conn, "SELECT * FROM members WHERE plex_id = ?", (plex_id,))
        return db.member_public(row)
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

    payload = read_session_cookie(filmclub_session)
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")

    conn = db.connect()
    try:
        row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (payload.get("mid"),))
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Session member not found")
    # Sessions created before per-member Plex rating sync was introduced carry
    # identity only, so there is no user credential we can recover from them.
    # Require the normal Plex login once; its callback retains the token while
    # upserting this same member row, preserving all Film Club data.
    if not decrypt_plex_token(row["plex_token_encrypted"]):
        raise HTTPException(status_code=401, detail="Plex reauthentication required")
    return _with_effective_admin(db.member_public(row))


def _with_effective_admin(member: dict) -> dict:
    """Admin is true if the DB flag is set OR the account is on the env
    allowlist — so the owner is admin immediately, even before a re-login."""
    if member and member.get("plex_id") in config.ADMIN_PLEX_IDS:
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
    finally:
        conn.close()
    result["plex_rating_sync_connected"] = bool(
        row and decrypt_plex_token(row["plex_token_encrypted"])
    )
    result["plex_rating_sync_enabled"] = bool(
        row and row["plex_rating_sync_enabled"]
    )
    return result


def require_admin(member: dict = Depends(current_member)) -> dict:
    """FastAPI dependency: allow only admins through, else 403."""
    if not member.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return member
