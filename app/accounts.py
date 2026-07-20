"""Invite-only local accounts and identity management.

Local accounts are gated by single-use, expiring invites created by an admin.
There is no open registration. A local account is a normal member row (with a
synthetic ``local:<random>`` plex_id so existing Plex-keyed code keeps working)
plus a ``local`` row in ``identities`` holding the scrypt password hash.
"""
import hashlib
import logging
import re
import secrets

from . import config, db
from .colors import color_for
from .passwords import hash_password, verify_password

log = logging.getLogger("filmclub.accounts")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 8
DEFAULT_INVITE_TTL_HOURS = 72

# A fixed hash to verify against when a username is unknown, so login timing does
# not reveal whether an account exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class AccountError(ValueError):
    """A user-correctable problem (bad invite, taken username, weak password)."""


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _validate_credentials(username: str, password: str) -> str:
    uid = (username or "").strip()
    if not USERNAME_RE.match(uid):
        raise AccountError("Username must be 3-32 characters: letters, numbers, . _ or -")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AccountError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    return uid


# --- invites ---------------------------------------------------------------

def create_invite(created_by: int | None, ttl_hours: int = DEFAULT_INVITE_TTL_HOURS,
                  email: str | None = None) -> dict:
    """Create an invite and return its one-time code plus metadata."""
    code = secrets.token_urlsafe(24)
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO invites (code_hash, created_by, email, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (_hash_code(code), created_by, (email or "").strip() or None,
             f"+{int(ttl_hours)} hours"),
        )
        row = db.query_one(conn, "SELECT expires_at FROM invites WHERE id = ?", (cur.lastrowid,))
        conn.commit()
    finally:
        conn.close()
    return {"code": code, "expires_at": row["expires_at"]}


def list_invites() -> list[dict]:
    """All invites with a derived status; the code itself is never returned."""
    conn = db.connect()
    try:
        rows = db.query_all(
            conn,
            """SELECT i.id, i.email, i.created_at, i.expires_at, i.redeemed_at,
                      m.username AS redeemed_by,
                      CASE WHEN i.redeemed_at IS NOT NULL THEN 'redeemed'
                           WHEN i.expires_at <= datetime('now') THEN 'expired'
                           ELSE 'pending' END AS status
               FROM invites i
               LEFT JOIN members m ON m.id = i.redeemed_member_id
               ORDER BY i.created_at DESC""",
        )
    finally:
        conn.close()
    return [dict(r) for r in rows]


def invite_status(code: str) -> dict:
    """Whether an invite code is currently redeemable (for the redeem page)."""
    conn = db.connect()
    try:
        row = db.query_one(
            conn,
            "SELECT email FROM invites WHERE code_hash = ? AND redeemed_at IS NULL "
            "AND expires_at > datetime('now')",
            (_hash_code(code or ""),),
        )
    finally:
        conn.close()
    return {"valid": row is not None, "email": row["email"] if row else None}


def redeem_invite(code: str, username: str, password: str) -> int:
    """Consume an invite and create the local member. Returns the new member id.

    Atomic and single-use: the invite is re-checked and marked redeemed inside
    one immediate transaction, so concurrent redemptions cannot both succeed.
    """
    uid = _validate_credentials(username, password)
    code_hash = _hash_code(code or "")
    password_hash = hash_password(password)  # outside the txn (CPU-bound)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        invite = db.query_one(
            conn,
            "SELECT id FROM invites WHERE code_hash = ? AND redeemed_at IS NULL "
            "AND expires_at > datetime('now')",
            (code_hash,),
        )
        if not invite:
            raise AccountError("This invite is invalid, already used, or expired")
        taken = db.query_one(
            conn,
            "SELECT 1 FROM identities WHERE provider = 'local' AND provider_uid = ?",
            (uid.lower(),),
        )
        if taken:
            raise AccountError("That username is taken")
        plex_id = "local:" + secrets.token_hex(16)
        cur = conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES (?, ?, ?)",
            (plex_id, uid, color_for(plex_id)),
        )
        member_id = cur.lastrowid
        conn.execute(
            "INSERT INTO identities (member_id, provider, provider_uid, password_hash) "
            "VALUES (?, 'local', ?, ?)",
            (member_id, uid.lower(), password_hash),
        )
        conn.execute(
            "UPDATE invites SET redeemed_at = datetime('now'), redeemed_member_id = ? "
            "WHERE id = ?",
            (member_id, invite["id"]),
        )
        conn.commit()
    except AccountError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info("Local account created for member %d via invite", member_id)
    return member_id


# --- local login -----------------------------------------------------------

def authenticate_local(username: str, password: str) -> int | None:
    """Return the member id for valid local credentials, else None."""
    uid = (username or "").strip().lower()
    conn = db.connect()
    try:
        row = db.query_one(
            conn,
            "SELECT member_id, password_hash FROM identities "
            "WHERE provider = 'local' AND provider_uid = ?",
            (uid,),
        )
    finally:
        conn.close()
    if not row:
        verify_password(password, _DUMMY_HASH)  # equalize timing for unknown users
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row["member_id"]


def has_local_identity(member_id: int) -> bool:
    conn = db.connect()
    try:
        return db.query_one(
            conn,
            "SELECT 1 FROM identities WHERE member_id = ? AND provider = 'local'",
            (member_id,),
        ) is not None
    finally:
        conn.close()
