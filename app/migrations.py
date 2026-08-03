"""Ordered, transactional schema migrations with a pre-migration backup.

Each migration is a ``(version, name, up)`` triple. Pending migrations are
applied in version order; every migration runs inside its own explicit
transaction *together with* the row that records it in ``schema_migrations``,
so a failure rolls the whole step back and leaves the version unrecorded.

Before any pending migration runs, a timestamped online backup of the database
is written under ``<data dir>/backups`` (never overwriting an existing file), so
the documented rollback is simply: restore that copy and run the previous image.

This is intentionally tiny (one small club, a few hundred rows) and has no
down-migrations: the backup is the rollback path. New migrations are appended
with the next integer version and must never be renumbered or reordered.
"""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("filmclub.migrations")


# --------------------------------------------------------------------------
# Migration steps. Each `up(conn)` receives a connection in autocommit mode;
# the runner wraps the call in an explicit BEGIN/COMMIT.
# --------------------------------------------------------------------------

def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _m1_baseline(conn: sqlite3.Connection) -> None:
    """Baseline = the schema as of the pre-framework release.

    Idempotent: brings an older existing database up to the current column set,
    and is a no-op on a fresh database created from ``schema.sql`` (which already
    declares every column). This preserves the exact additive steps that the old
    ``db._migrate()`` performed, so upgrading an in-place production database is a
    behavioural no-op beyond recording the baseline version.
    """
    _add_column_if_missing(conn, "members", "is_admin",
                           "is_admin INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "members", "display_name", "display_name TEXT")
    _add_column_if_missing(conn, "members", "plex_account_id", "plex_account_id TEXT")
    _add_column_if_missing(conn, "members", "plex_token_encrypted", "plex_token_encrypted TEXT")
    _add_column_if_missing(conn, "members", "plex_rating_sync_enabled",
                           "plex_rating_sync_enabled INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "movies", "language", "language TEXT")
    _add_column_if_missing(conn, "movies", "seerr_status", "seerr_status TEXT")


def _m2_app_settings(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "members", "is_owner",
                           "is_owner INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
               key        TEXT PRIMARY KEY,
               value      TEXT NOT NULL,
               encrypted  INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_single_owner "
        "ON members(is_owner) WHERE is_owner = 1"
    )


def _m3_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               token_hash   TEXT NOT NULL UNIQUE,
               member_id    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
               created_at   TEXT NOT NULL DEFAULT (datetime('now')),
               last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
               expires_at   TEXT NOT NULL
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions(member_id)")


def _m4_identities(conn: sqlite3.Connection) -> None:
    # Login methods mapped to member rows. One member may have both a 'plex' and
    # a 'local' identity (account linking). provider_uid is the Plex uuid or the
    # lowercased local username; password_hash is set for local identities only.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS identities (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               member_id     INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
               provider      TEXT NOT NULL,
               provider_uid  TEXT NOT NULL,
               password_hash TEXT,
               created_at    TEXT NOT NULL DEFAULT (datetime('now')),
               UNIQUE (provider, provider_uid)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_identities_member ON identities(member_id)")
    # Grandfather every existing member as an approved Plex identity, preserving
    # their member id and all related data.
    conn.execute(
        """INSERT OR IGNORE INTO identities (member_id, provider, provider_uid)
           SELECT id, 'plex', plex_id FROM members WHERE plex_id IS NOT NULL"""
    )
    # Single-use, expiring invitations. Only the SHA-256 hash of the code is kept.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS invites (
               id                 INTEGER PRIMARY KEY AUTOINCREMENT,
               code_hash          TEXT NOT NULL UNIQUE,
               created_by         INTEGER REFERENCES members(id) ON DELETE SET NULL,
               email              TEXT,
               expires_at         TEXT NOT NULL,
               redeemed_at        TEXT,
               redeemed_member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
               created_at         TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )


def _m5_password_resets(conn: sqlite3.Connection) -> None:
    # Admin-issued, expiring, single-use reset links. As with invites and
    # sessions, only a SHA-256 digest is persisted; plaintext is returned once.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS password_resets (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               token_hash TEXT NOT NULL UNIQUE,
               member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
               created_by INTEGER REFERENCES members(id) ON DELETE SET NULL,
               expires_at TEXT NOT NULL,
               used_at    TEXT,
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_resets_member "
        "ON password_resets(member_id)"
    )


def _m6_movie_content_rating(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "movies", "content_rating", "content_rating TEXT")


def _m7_member_theme(conn: sqlite3.Connection) -> None:
    # Per-member visual mode. 'system' follows the operating system preference,
    # which is what an existing member should get until they choose otherwise.
    _add_column_if_missing(conn, "members", "theme",
                           "theme TEXT NOT NULL DEFAULT 'system'")


def _m8_movie_pitch(conn: sqlite3.Connection) -> None:
    # Suggester's optional "elevator pitch", shown only on a film's detail page.
    # Existing films simply have no pitch (NULL).
    _add_column_if_missing(conn, "movies", "pitch", "pitch TEXT")


def _m9_member_discord_id(conn: sqlite3.Connection) -> None:
    # Admin-entered Discord user id, used to @mention members in the weekly
    # reminder digest. Existing members simply have none (NULL) until an
    # admin fills it in.
    _add_column_if_missing(conn, "members", "discord_user_id", "discord_user_id TEXT")


def _m10_collections(conn: sqlite3.Connection) -> None:
    # Curated collections. Purely additive: no existing table is touched, so an
    # existing database gains two empty tables and behaves exactly as before.
    # Entries are keyed on tmdb_id, never a Plex ratingKey — see schema.sql.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS collections (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               slug          TEXT UNIQUE NOT NULL,
               title         TEXT NOT NULL,
               kind          TEXT NOT NULL DEFAULT 'picked',
               intro         TEXT,
               director_name    TEXT,
               director_tmdb_id INTEGER,
               director_intro   TEXT,
               published     INTEGER NOT NULL DEFAULT 0,
               created_at    TEXT NOT NULL DEFAULT (datetime('now')),
               updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS collection_entries (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
               tmdb_id       INTEGER NOT NULL,
               imdb_id       TEXT,
               title         TEXT NOT NULL,
               year          INTEGER,
               runtime       INTEGER,
               director      TEXT,
               still_url     TEXT,
               blurb         TEXT,
               position      INTEGER NOT NULL DEFAULT 0,
               created_at    TEXT NOT NULL DEFAULT (datetime('now')),
               updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
               UNIQUE (collection_id, tmdb_id)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_collection_entries_collection
               ON collection_entries(collection_id, position)"""
    )


def _m11_director_scaffold(conn: sqlite3.Connection) -> None:
    # Snapshotted TMDB scaffolding for a director collection, so a public page
    # load never depends on TMDB. The filmography is deliberately not stored:
    # it is only needed for the admin coverage view and is fetched on demand.
    _add_column_if_missing(conn, "collections", "director_portrait_url",
                           "director_portrait_url TEXT")
    _add_column_if_missing(conn, "collections", "director_born", "director_born TEXT")
    _add_column_if_missing(conn, "collections", "director_died", "director_died TEXT")


def _m12_collection_origin(conn: sqlite3.Connection) -> None:
    # Whether a collection is the owner's own writing or was assembled for them.
    # Defaults to 'authored' so an existing collection keeps its full editing
    # surface; marking the generated ones is a data decision, not a schema one.
    _add_column_if_missing(conn, "collections", "origin",
                           "origin TEXT NOT NULL DEFAULT 'authored'")


def _m13_collection_curators(conn: sqlite3.Connection) -> None:
    # Lets the owner grant a member collection-management rights without full
    # admin access. Independent of is_admin; an admin's authority already
    # covers every collection and never depends on this flag.
    _add_column_if_missing(conn, "members", "can_curate_collections",
                           "can_curate_collections INTEGER NOT NULL DEFAULT 0")
    # Per-collection ownership, needed now that more than one person can
    # author a collection: attribution and edit rights both key off this.
    _add_column_if_missing(conn, "collections", "created_by",
                           "created_by INTEGER REFERENCES members(id) ON DELETE SET NULL")
    # Every 'authored' collection created before this column existed was, in
    # practice, written by the site owner — there was no other way to make
    # one. Backfilling keeps their attribution and edit rights unchanged
    # rather than silently becoming ownerless.
    conn.execute(
        """UPDATE collections SET created_by = (SELECT id FROM members WHERE is_owner = 1 LIMIT 1)
               WHERE origin = 'authored' AND created_by IS NULL"""
    )


# Ordered list of migrations. Append new ones with the next integer version.
MIGRATIONS: list[tuple[int, str, "callable"]] = [
    (1, "baseline", _m1_baseline),
    (2, "app-settings", _m2_app_settings),
    (3, "sessions", _m3_sessions),
    (4, "identities", _m4_identities),
    (5, "password-resets", _m5_password_resets),
    (6, "movie-content-rating", _m6_movie_content_rating),
    (7, "member-theme", _m7_member_theme),
    (8, "movie-pitch", _m8_movie_pitch),
    (9, "member-discord-id", _m9_member_discord_id),
    (10, "collections", _m10_collections),
    (11, "director-scaffold", _m11_director_scaffold),
    (12, "collection-origin", _m12_collection_origin),
    (13, "collection-curators", _m13_collection_curators),
]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_table(conn)
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def _backup(db_path: Path, target_version: int) -> Path | None:
    """Write a timestamped online backup next to the database, under ``backups/``.

    Returns the backup path, or None when there is no database file yet (a fresh
    install has nothing worth preserving). Never overwrites an existing file.
    """
    if not db_path.exists():
        return None
    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups / f"filmclub-{ts}-pre-v{target_version}.db"
    n = 1
    while dest.exists():
        dest = backups / f"filmclub-{ts}-pre-v{target_version}-{n}.db"
        n += 1

    src = sqlite3.connect(db_path, timeout=30)
    bck = sqlite3.connect(dest)
    try:
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass  # best effort; the online backup still captures committed state
        src.backup(bck)
    finally:
        bck.close()
        src.close()
    log.info("Wrote pre-migration backup: %s", dest)
    return dest


def run(db_path: Path) -> None:
    """Apply pending migrations to the database at ``db_path``.

    Safe to call on every startup; does nothing when the database is current
    (so a normal restart takes no backup and no schema work).
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit txn control
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        applied = _applied_versions(conn)
        pending = sorted(m for m in MIGRATIONS if m[0] not in applied)
        if not pending:
            return
        target = pending[-1][0]
        _backup(db_path, target)
        for version, name, up in pending:
            log.info("Applying migration v%d (%s)", version, name)
            conn.execute("BEGIN")
            try:
                up(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                log.exception("Migration v%d (%s) failed; rolled back", version, name)
                raise
    finally:
        conn.close()


def current_version(db_path: Path) -> int:
    """Highest applied migration version, or 0 if none / no database yet."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0
    finally:
        conn.close()


def latest_version() -> int:
    """Highest version this codebase knows how to migrate to."""
    return max((m[0] for m in MIGRATIONS), default=0)
