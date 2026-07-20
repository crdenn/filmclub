"""Authenticated encryption for per-member Plex tokens.

The encryption key is derived from the stable SESSION_SECRET. Rotating that
secret intentionally makes existing stored tokens unreadable; members can
restore sync by signing in again, which replaces the encrypted value.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("filmclub.token_crypto")


def _fernet() -> Fernet | None:
    if not config.SESSION_SECRET:
        return None
    digest = hashlib.sha256(
        f"filmclub-plex-token:{config.SESSION_SECRET}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_plex_token(token: str | None) -> str | None:
    """Encrypt a Plex token, or return None when persistence is unavailable."""
    if not token:
        return None
    fernet = _fernet()
    if not fernet:
        log.warning("SESSION_SECRET is unset; refusing to persist a Plex user token")
        return None
    return fernet.encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_plex_token(encrypted: str | None) -> str | None:
    """Decrypt a stored token. Invalid/old-key values safely behave as absent."""
    if not encrypted:
        return None
    fernet = _fernet()
    if not fernet:
        return None
    try:
        return fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        log.warning("Could not decrypt a stored Plex token; member must sign in again")
        return None
