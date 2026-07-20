"""SQLite access layer.

Thin helpers over sqlite3. One connection per request/thread via a small
factory; SQLite handles our concurrency needs (six users) comfortably with WAL
mode. Rows come back as dict-like sqlite3.Row.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables if they do not exist, then run migrations. Idempotent."""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema migrations for existing databases. Each step is guarded
    so re-running is safe."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "display_name" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN display_name TEXT")
        conn.commit()
    if "plex_account_id" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN plex_account_id TEXT")
        conn.commit()
    if "plex_token_encrypted" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN plex_token_encrypted TEXT")
        conn.commit()
    if "plex_rating_sync_enabled" not in cols:
        conn.execute(
            "ALTER TABLE members ADD COLUMN plex_rating_sync_enabled "
            "INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()

    movie_cols = {r["name"] for r in conn.execute("PRAGMA table_info(movies)")}
    if "language" not in movie_cols:
        conn.execute("ALTER TABLE movies ADD COLUMN language TEXT")
        conn.commit()
    if "seerr_status" not in movie_cols:
        conn.execute("ALTER TABLE movies ADD COLUMN seerr_status TEXT")
        conn.commit()


# --- small convenience wrappers -------------------------------------------

def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# --- domain serialisers ----------------------------------------------------

def member_public(row: sqlite3.Row | dict) -> dict | None:
    """Shape a member row for the API. Never expose encrypted Plex tokens."""
    if row is None:
        return None
    d = dict(row)
    display = (d.get("display_name") or "").strip() or None
    return {
        "id": d["id"],
        "plex_id": d["plex_id"],
        # `username` is the effective name shown everywhere: the member's chosen
        # display name if set, otherwise their Plex username. The raw Plex name
        # stays available as `plex_username` (the admin panel uses it).
        "username": display or d["username"],
        "display_name": display,
        "plex_username": d["username"],
        "email": d.get("email"),
        "thumb": d.get("thumb"),
        "color": d["color"],
        "is_admin": bool(d.get("is_admin")),
    }


def movie_base(row: sqlite3.Row | dict) -> dict:
    """Shape a movie row: parse the genres JSON, keep field names stable."""
    d = dict(row)
    genres = d.get("genres")
    try:
        d["genres"] = json.loads(genres) if genres else []
    except (json.JSONDecodeError, TypeError):
        d["genres"] = []
    return d
