"""Tests for the ordered, transactional migration runner and its backups."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "migration-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-migrations-bootstrap-"))

from app import config, db, migrations  # noqa: E402


def _columns(path, table):
    c = sqlite3.connect(path)
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    finally:
        c.close()


def _backup_files(db_path):
    return sorted((db_path.parent / "backups").glob("filmclub-*.db"))


# Columns the baseline migration must guarantee on an upgraded database.
_MEMBER_COLS = ("is_admin", "display_name", "plex_account_id",
                "plex_token_encrypted", "plex_rating_sync_enabled", "is_owner")
_MOVIE_COLS = ("language", "seerr_status", "content_rating")


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-migrations-")
        self.db_path = Path(self.tmp.name) / "filmclub.db"
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self.tmp.cleanup()

    def _make_old_shaped_db(self):
        """A pre-framework database: original columns only, with real rows."""
        c = sqlite3.connect(self.db_path)
        c.executescript(
            """
            CREATE TABLE members (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plex_id TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL, email TEXT, thumb TEXT, color TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER, title TEXT NOT NULL,
                year INTEGER, poster_url TEXT, backdrop_url TEXT, runtime INTEGER, director TEXT,
                overview TEXT, genres TEXT, suggested_by INTEGER,
                suggested_at TEXT NOT NULL DEFAULT (datetime('now')),
                status TEXT NOT NULL DEFAULT 'suggested', watched_at TEXT, imdb_id TEXT,
                seen_before_snapshot TEXT);
            """
        )
        c.execute("INSERT INTO members (plex_id, username, color) VALUES ('dev:Alice','Alice','#f0803c')")
        c.execute("INSERT INTO movies (title, status) VALUES ('Seven Samurai','suggested')")
        c.commit()
        c.close()

    def test_fresh_db_records_baseline_and_has_all_columns(self):
        db.init_db()
        self.assertEqual(migrations.current_version(self.db_path), migrations.latest_version())
        for col in _MEMBER_COLS:
            self.assertIn(col, _columns(self.db_path, "members"))
        for col in _MOVIE_COLS:
            self.assertIn(col, _columns(self.db_path, "movies"))
        c = sqlite3.connect(self.db_path)
        rows = c.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        c.close()
        self.assertEqual(rows, [(1, "baseline"), (2, "app-settings"),
                                (3, "sessions"), (4, "identities"),
                                (5, "password-resets"),
                                (6, "movie-content-rating")])
        self.assertIn("key", _columns(self.db_path, "app_settings"))
        self.assertIn("token_hash", _columns(self.db_path, "sessions"))
        self.assertIn("provider_uid", _columns(self.db_path, "identities"))
        self.assertIn("token_hash", _columns(self.db_path, "password_resets"))

    def test_upgrades_old_shaped_db_and_preserves_data(self):
        self._make_old_shaped_db()
        migrations.run(self.db_path)

        for col in _MEMBER_COLS:
            self.assertIn(col, _columns(self.db_path, "members"))
        for col in _MOVIE_COLS:
            self.assertIn(col, _columns(self.db_path, "movies"))

        c = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(c.execute("SELECT username FROM members").fetchone()[0], "Alice")
            self.assertEqual(c.execute("SELECT title FROM movies").fetchone()[0], "Seven Samurai")
            # Additive defaults land on the existing row.
            self.assertEqual(c.execute("SELECT is_admin FROM members").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT plex_rating_sync_enabled FROM members").fetchone()[0], 1)
        finally:
            c.close()
        self.assertEqual(migrations.current_version(self.db_path), migrations.latest_version())

    def test_init_db_can_upgrade_before_owner_column_exists(self):
        self._make_old_shaped_db()
        db.init_db()
        self.assertIn("is_owner", _columns(self.db_path, "members"))
        c = sqlite3.connect(self.db_path)
        try:
            indexes = {r[1] for r in c.execute("PRAGMA index_list(members)")}
        finally:
            c.close()
        self.assertIn("idx_members_single_owner", indexes)

    def test_backup_written_before_migrating_existing_db(self):
        self._make_old_shaped_db()
        migrations.run(self.db_path)
        backups = _backup_files(self.db_path)
        self.assertEqual(len(backups), 1)
        # The backup is a valid database holding the pre-migration data.
        b = sqlite3.connect(backups[0])
        try:
            self.assertEqual(b.execute("SELECT username FROM members").fetchone()[0], "Alice")
        finally:
            b.close()

    def test_idempotent_second_run_does_nothing(self):
        self._make_old_shaped_db()
        migrations.run(self.db_path)
        first_backups = _backup_files(self.db_path)
        migrations.run(self.db_path)  # nothing pending
        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        c.close()
        self.assertEqual(count, len(migrations.MIGRATIONS))  # no duplicate rows
        self.assertEqual(_backup_files(self.db_path), first_backups)  # no extra backup

    def test_failed_migration_rolls_back_and_is_not_recorded(self):
        db.init_db()  # at the current version

        def _boom(conn):
            conn.execute("ALTER TABLE movies ADD COLUMN temp_col TEXT")
            raise RuntimeError("boom")

        original = list(migrations.MIGRATIONS)
        migrations.MIGRATIONS.append((999, "boom", _boom))
        try:
            with self.assertRaises(RuntimeError):
                migrations.run(self.db_path)
        finally:
            migrations.MIGRATIONS[:] = original

        # The failed version is not recorded and its DDL was rolled back.
        self.assertEqual(migrations.current_version(self.db_path), migrations.latest_version())
        self.assertNotIn("temp_col", _columns(self.db_path, "movies"))


if __name__ == "__main__":
    unittest.main()
