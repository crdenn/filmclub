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

from . import db
from .colors import color_for
from .passwords import hash_password, verify_password
from .token_crypto import encrypt_plex_token

log = logging.getLogger("filmclub.accounts")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 8
DEFAULT_INVITE_TTL_HOURS = 72
DEFAULT_RESET_TTL_HOURS = 1

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


def _validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AccountError(f"Password must be at least {MIN_PASSWORD_LEN} characters")


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
    return {"id": cur.lastrowid, "code": code, "expires_at": row["expires_at"]}


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


def create_first_owner(username: str, password: str) -> int:
    """Create the first member as a local owner on a fresh database.

    The setup-code check belongs to the HTTP route. This domain operation still
    takes an immediate transaction and refuses to run once any member exists, so
    concurrent setup attempts cannot create two owners.
    """
    uid = _validate_credentials(username, password)
    password_hash = hash_password(password)
    plex_id = "local:" + secrets.token_hex(16)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.query_one(conn, "SELECT id FROM members LIMIT 1"):
            raise AccountError("The first owner has already been created")
        cur = conn.execute(
            "INSERT INTO members (plex_id, username, color, is_admin, is_owner) "
            "VALUES (?, ?, ?, 1, 1)",
            (plex_id, uid, color_for(plex_id)),
        )
        member_id = cur.lastrowid
        conn.execute(
            "INSERT INTO identities (member_id, provider, provider_uid, password_hash) "
            "VALUES (?, 'local', ?, ?)",
            (member_id, uid.lower(), password_hash),
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
    log.info("First local owner created for member %d", member_id)
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


def identity_providers(member_id: int) -> list[str]:
    """Return a member's login providers without any credential material."""
    conn = db.connect()
    try:
        rows = db.query_all(
            conn,
            "SELECT provider FROM identities WHERE member_id = ? ORDER BY provider",
            (member_id,),
        )
    finally:
        conn.close()
    return [row["provider"] for row in rows]


def link_plex_identity(member_id: int, *, plex_id: str, username: str,
                       email: str | None, thumb: str | None,
                       plex_account_id: str | None, plex_token: str) -> dict:
    """Attach or refresh a Plex login on an existing member.

    A Plex identity may belong to only one member. Attempting to link an
    identity that already resolves elsewhere is rejected rather than silently
    merging accounts or moving club history.
    """
    encrypted_token = encrypt_plex_token(plex_token)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        member = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        if not member:
            raise AccountError("Member not found")
        existing = db.query_one(
            conn,
            "SELECT member_id FROM identities WHERE provider = 'plex' AND provider_uid = ?",
            (plex_id,),
        )
        if existing and existing["member_id"] != member_id:
            raise AccountError("That Plex account is already linked to another member")
        legacy = db.query_one(
            conn,
            "SELECT id FROM members WHERE plex_id = ? AND id != ?",
            (plex_id, member_id),
        )
        if legacy:
            raise AccountError("That Plex account is already linked to another member")
        if not existing:
            conn.execute(
                "INSERT INTO identities (member_id, provider, provider_uid) "
                "VALUES (?, 'plex', ?)",
                (member_id, plex_id),
            )
        conn.execute(
            """UPDATE members SET username = ?, email = ?, thumb = ?,
               plex_account_id = ?, plex_token_encrypted = ? WHERE id = ?""",
            (username, email, thumb, plex_account_id, encrypted_token, member_id),
        )
        row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        conn.commit()
    except AccountError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info("Plex identity linked to member %d", member_id)
    return db.member_public(row)


# --- admin-issued password resets -----------------------------------------

def create_password_reset(created_by: int, member_id: int,
                          ttl_hours: int = DEFAULT_RESET_TTL_HOURS) -> dict:
    """Create a one-time password-reset token for a local member."""
    token = secrets.token_urlsafe(32)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        local = db.query_one(
            conn,
            "SELECT 1 FROM identities WHERE member_id = ? AND provider = 'local'",
            (member_id,),
        )
        if not local:
            raise AccountError("Member does not have a local account")
        # Issuing a new reset supersedes any older unused links for this member.
        conn.execute(
            "UPDATE password_resets SET used_at = datetime('now') "
            "WHERE member_id = ? AND used_at IS NULL",
            (member_id,),
        )
        cur = conn.execute(
            "INSERT INTO password_resets (token_hash, member_id, created_by, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (_hash_code(token), member_id, created_by, f"+{int(ttl_hours)} hours"),
        )
        row = db.query_one(
            conn, "SELECT expires_at FROM password_resets WHERE id = ?", (cur.lastrowid,)
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
    log.info("Password reset issued for member %d", member_id)
    return {"token": token, "expires_at": row["expires_at"]}


def password_reset_status(token: str) -> dict:
    """Return whether a reset token is currently usable."""
    conn = db.connect()
    try:
        row = db.query_one(
            conn,
            """SELECT m.username FROM password_resets r
               JOIN members m ON m.id = r.member_id
               WHERE r.token_hash = ? AND r.used_at IS NULL
                 AND r.expires_at > datetime('now')""",
            (_hash_code(token or ""),),
        )
    finally:
        conn.close()
    return {"valid": row is not None, "username": row["username"] if row else None}


def redeem_password_reset(token: str, password: str) -> int:
    """Consume a reset token, change the password, and revoke old sessions."""
    _validate_password(password)
    password_hash = hash_password(password)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        reset = db.query_one(
            conn,
            "SELECT id, member_id FROM password_resets WHERE token_hash = ? "
            "AND used_at IS NULL AND expires_at > datetime('now')",
            (_hash_code(token or ""),),
        )
        if not reset:
            raise AccountError("This reset link is invalid, already used, or expired")
        cur = conn.execute(
            "UPDATE identities SET password_hash = ? "
            "WHERE member_id = ? AND provider = 'local'",
            (password_hash, reset["member_id"]),
        )
        if cur.rowcount != 1:
            raise AccountError("Local account not found")
        conn.execute(
            "UPDATE password_resets SET used_at = datetime('now') WHERE id = ?",
            (reset["id"],),
        )
        conn.execute("DELETE FROM sessions WHERE member_id = ?", (reset["member_id"],))
        conn.commit()
    except AccountError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info("Password reset completed for member %d", reset["member_id"])
    return reset["member_id"]
