"""SQLite access layer.

Thin helpers over sqlite3. One connection per request/thread via a small
factory; SQLite handles our concurrency needs (six users) comfortably with WAL
mode. Rows come back as dict-like sqlite3.Row.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config, migrations

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Ensure the base tables exist, then apply ordered, backed-up migrations.

    ``schema.sql`` (CREATE ... IF NOT EXISTS) seeds a fresh database; the
    versioned migration runner then brings any existing database forward and
    records its state in ``schema_migrations``. Idempotent and safe on every
    startup — a database that is already current does no schema work and takes
    no backup.
    """
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    migrations.run(config.DB_PATH)


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
        "is_owner": bool(d.get("is_owner")),
        "can_curate_collections": bool(d.get("can_curate_collections")),
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
