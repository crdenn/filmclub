"""Encrypted, database-backed runtime configuration and first-run state."""
import base64
import hashlib
import hmac
import logging
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from . import config, db

log = logging.getLogger("filmclub.settings")

FIELDS = {
    "TMDB_API_KEY": {"secret": True, "required": True},
    "PLEX_URL": {"secret": False, "required": True},
    "PLEX_TOKEN": {"secret": True, "required": True},
    "PLEX_MACHINE_ID": {"secret": False, "required": True},
    "APP_URL": {"secret": False, "required": True},
    "PLEX_WEBHOOK_SECRET": {"secret": True, "required": False},
    "PLEX_REFRESH_INTERVAL": {"secret": False, "required": False},
    "SEERR_URL": {"secret": False, "required": False},
    "SEERR_API_KEY": {"secret": True, "required": False},
    "SEERR_TIMEOUT": {"secret": False, "required": False},
}
SETUP_COMPLETE = "_setup_complete"


def _fernet() -> Fernet:
    digest = hashlib.sha256(
        f"filmclub-app-settings:{config.SESSION_SECRET}".encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("Stored application setting could not be decrypted") from exc


def _rows() -> dict[str, tuple[str, bool]]:
    conn = db.connect()
    try:
        return {r["key"]: (r["value"], bool(r["encrypted"]))
                for r in conn.execute("SELECT key, value, encrypted FROM app_settings")}
    finally:
        conn.close()


def is_setup_complete() -> bool:
    return _rows().get(SETUP_COMPLETE, ("", False))[0] == "1"


def load_into_config() -> None:
    """Apply stored values unless an explicit environment value overrides one."""
    for key, (stored, encrypted) in _rows().items():
        if key not in FIELDS or os.environ.get(key, "").strip():
            continue
        value = _decrypt(stored) if encrypted else stored
        if key in {"PLEX_REFRESH_INTERVAL"}:
            value = int(value)
        elif key in {"SEERR_TIMEOUT"}:
            value = float(value)
        elif key in {"PLEX_URL", "SEERR_URL", "APP_URL"}:
            value = value.rstrip("/")
        setattr(config, key, value)


def save(values: dict[str, str | int | float | None], *, complete: bool = False) -> None:
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        for key, meta in FIELDS.items():
            if key not in values:
                continue
            raw = values[key]
            value = "" if raw is None else str(raw).strip()
            stored = _encrypt(value) if meta["secret"] and value else value
            conn.execute(
                """INSERT INTO app_settings (key, value, encrypted, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                     encrypted=excluded.encrypted, updated_at=excluded.updated_at""",
                (key, stored, 1 if meta["secret"] and value else 0),
            )
        if complete:
            conn.execute(
                """INSERT INTO app_settings (key, value, encrypted) VALUES (?, '1', 0)
                   ON CONFLICT(key) DO UPDATE SET value='1', updated_at=datetime('now')""",
                (SETUP_COMPLETE,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    load_into_config()


def public_values(*, reveal_nonsecrets: bool = True) -> dict:
    rows = _rows()
    result = {}
    for key, meta in FIELDS.items():
        env_value = os.environ.get(key, "").strip()
        stored = rows.get(key)
        effective = getattr(config, key, "")
        result[key] = {
            "value": "" if meta["secret"] or not reveal_nonsecrets else str(effective or ""),
            "configured": bool(effective),
            "secret": meta["secret"],
            "required": meta["required"],
            "source": "environment" if env_value else "database" if stored else "default",
            "locked": bool(env_value),
        }
    return result


def setup_code() -> str:
    path = config.DATA_DIR / "setup_code"
    if path.exists():
        return path.read_text().strip()
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    path.write_text(code)
    path.chmod(0o600)
    return code


def verify_setup_code(candidate: str) -> bool:
    return hmac.compare_digest(setup_code(), (candidate or "").strip().upper())


def remove_setup_code() -> None:
    path = config.DATA_DIR / "setup_code"
    if path.exists():
        path.unlink()
