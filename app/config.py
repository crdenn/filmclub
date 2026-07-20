"""Configuration defaults and environment overrides.

Normal application settings are loaded into this module from encrypted database
storage after schema initialization. Explicit environment variables take
precedence. Durable cryptographic and Plex client identifiers self-provision in
the data directory when absent.
"""
import os
import secrets
import uuid
from pathlib import Path

# --- App metadata ----------------------------------------------------------
# Human-readable version, surfaced in /readyz, admin diagnostics, and container
# labels. Overridable at build/run time (e.g. from a CI-injected release tag).
APP_VERSION = os.environ.get("FILMCLUB_VERSION", "0.9.0")

# --- Paths -----------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "filmclub.db"


def _durable_file_secret(filename: str) -> str:
    """Read (or create once) a durable random secret in the data volume."""
    path = DATA_DIR / filename
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _data_key() -> str:
    """Key protecting data at rest: app_settings secrets and per-member Plex
    tokens.

    Sourced from the legacy ``SESSION_SECRET`` env var, else the generated
    ``master.key``, so values encrypted by earlier versions stay readable. Kept
    deliberately separate from the cookie-signing secret below: rotating the
    signing secret must never risk the encrypted data, and rotating this key
    requires re-encrypting what it protects.
    """
    supplied = os.environ.get("SESSION_SECRET", "").strip()
    if supplied:
        return supplied
    return _durable_file_secret("master.key")

# --- Metadata / enrichment -------------------------------------------------
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_MACHINE_ID = os.environ.get("PLEX_MACHINE_ID", "")

# Secret embedded in the Plex webhook URL. When blank, Plex -> Film Club rating
# sync is disabled; Film Club -> Plex can still work for members who re-login.
PLEX_WEBHOOK_SECRET = os.environ.get("PLEX_WEBHOOK_SECRET", "").strip()

# How often (seconds) to refresh the local set of Plex library GUIDs.
PLEX_REFRESH_INTERVAL = int(os.environ.get("PLEX_REFRESH_INTERVAL", "3600"))

# --- Seerr (Overseerr/Jellyseerr) auto-request ------------------------------
# Optional. When a film is added that isn't already on Plex, submit a request to
# a Seerr instance so it gets fetched automatically. If SEERR_URL / SEERR_API_KEY
# are unset the feature is inert and the add flow behaves exactly as before.
SEERR_URL = os.environ.get("SEERR_URL", "").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
SEERR_TIMEOUT = float(os.environ.get("SEERR_TIMEOUT", "10"))

# --- Auth ------------------------------------------------------------------
APP_URL = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")

# Data-at-rest encryption key (app_settings secrets + Plex tokens). See _data_key.
DATA_KEY = _data_key()

# Cookie/session signing secret, independent of DATA_KEY so it can be rotated
# freely: replacing it only forces a re-login and never touches encrypted data.
# Self-provisions to <data>/session.key; delete that file and restart to rotate.
SESSION_SECRET = _durable_file_secret("session.key")
SESSION_COOKIE = "filmclub_session"
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(60 * 60 * 24 * 90)))  # 90 days
PLEX_PRODUCT = os.environ.get("PLEX_PRODUCT", "Film Club Tracker")

# Development convenience: skip Plex OAuth and log in as this username.
DEV_BYPASS_USER = os.environ.get("DEV_BYPASS_USER", "").strip()

# Declarative owner/admin allowlist: comma-separated Plex uuids. Accounts here
# are always admins (can't be locked out, survive a DB reset). Additional admins
# can be granted from the panel and are stored on members.is_admin.
ADMIN_PLEX_IDS = {
    x.strip() for x in os.environ.get("ADMIN_PLEX_IDS", "").split(",") if x.strip()
}


def _resolve_client_id() -> str:
    """Stable client identifier for the Plex OAuth flow.

    Prefer the env var. If it is not set, generate one once and persist it to
    the data volume so it survives restarts (the Plex flow ties pins to a
    client id, so it must not change between login start and callback).
    """
    env_val = os.environ.get("PLEX_CLIENT_ID", "").strip()
    if env_val:
        return env_val
    cid_file = DATA_DIR / "client_id"
    if cid_file.exists():
        return cid_file.read_text().strip()
    generated = str(uuid.uuid4())
    cid_file.write_text(generated)
    return generated


PLEX_CLIENT_ID = _resolve_client_id()


def missing_required() -> list[str]:
    """Return the names of required env vars that are unset.

    Used to warn loudly on startup. We warn rather than hard-fail so the app
    can still boot in a partially-configured / dev state (e.g. DEV_BYPASS_USER
    set, Plex not yet configured).
    """
    required = {
        "TMDB_API_KEY": TMDB_API_KEY,
        "SESSION_SECRET": SESSION_SECRET,
        "PLEX_URL": PLEX_URL,
        "PLEX_TOKEN": PLEX_TOKEN,
        "PLEX_MACHINE_ID": PLEX_MACHINE_ID,
        "APP_URL": APP_URL,
    }
    return [k for k, v in required.items() if not v]


# Both DATA_KEY and SESSION_SECRET are always durable: generated once in (and
# read back from) the bind-mounted data directory, or pinned via env.
EFFECTIVE_SESSION_SECRET = SESSION_SECRET

MEMBER_COUNT_HINT = 6  # club size; only used for friendly copy, not enforced
