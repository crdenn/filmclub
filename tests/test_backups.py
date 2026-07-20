"""Portable backup/restore coverage, including validation and secret re-keying."""
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-backups-bootstrap-"))
os.environ.setdefault("SESSION_SECRET", "backup-test-bootstrap-key")

from app import auth, backups, config, db, main, settings, token_crypto


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-backups-")
        self.old_data = config.DATA_DIR
        self.old_db = config.DB_PATH
        self.old_key = config.DATA_KEY
        self.old_values = {key: getattr(config, key) for key in settings.FIELDS}
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = config.DATA_DIR / "filmclub.db"
        config.DATA_KEY = "source-data-key"
        config.PLEX_TOKEN = "effective-environment-token"
        db.init_db()

        conn = db.connect()
        try:
            member = conn.execute(
                """INSERT INTO members
                   (plex_id, plex_token_encrypted, username, color, is_admin, is_owner)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("plex-owner", token_crypto.encrypt_plex_token("member-token"),
                 "Original owner", "#112233"),
            )
            self.member_id = member.lastrowid
            movie = conn.execute(
                "INSERT INTO movies (tmdb_id, title, status, suggested_by) VALUES (?, ?, ?, ?)",
                (123, "Original film", "suggested", self.member_id),
            )
            self.movie_id = movie.lastrowid
            conn.execute(
                "INSERT INTO sessions (token_hash, member_id, expires_at) "
                "VALUES ('old-session', ?, datetime('now', '+1 day'))",
                (self.member_id,),
            )
            conn.commit()
        finally:
            conn.close()
        settings.save({"TMDB_API_KEY": "saved-tmdb-key"}, complete=True)

    def tearDown(self):
        config.DATA_DIR = self.old_data
        config.DB_PATH = self.old_db
        config.DATA_KEY = self.old_key
        for key, value in self.old_values.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    def test_archive_restores_all_rows_rekeys_secrets_and_revokes_sessions(self):
        payload, filename = backups.create_archive()
        self.assertTrue(filename.endswith(".filmclub-backup"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), backups.EXPECTED_ENTRIES)

        conn = db.connect()
        try:
            conn.execute("UPDATE movies SET title = 'Changed after backup' WHERE id = ?",
                         (self.movie_id,))
            conn.execute("UPDATE members SET username = 'Changed owner' WHERE id = ?",
                         (self.member_id,))
            conn.commit()
        finally:
            conn.close()

        # A restore into a different install uses its current key, rather than
        # requiring the source server's SESSION_SECRET/master.key afterwards.
        config.DATA_KEY = "destination-data-key"
        result = backups.restore_archive(payload)

        conn = db.connect()
        try:
            movie = conn.execute("SELECT title FROM movies WHERE id = ?", (self.movie_id,)).fetchone()
            member = conn.execute(
                "SELECT username, plex_token_encrypted FROM members WHERE id = ?",
                (self.member_id,),
            ).fetchone()
            saved_setting = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'TMDB_API_KEY'"
            ).fetchone()[0]
            effective_setting = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'PLEX_TOKEN'"
            ).fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(movie[0], "Original film")
        self.assertEqual(member[0], "Original owner")
        self.assertEqual(token_crypto.decrypt_plex_token(member[1]), "member-token")
        self.assertEqual(settings._decrypt(saved_setting), "saved-tmdb-key")
        self.assertEqual(settings._decrypt(effective_setting), "effective-environment-token")
        self.assertEqual(session_count, 0)
        self.assertTrue(result["sessions_revoked"])
        self.assertTrue((config.DATA_DIR / "backups" / result["safety_backup"]).exists())

    def test_corrupt_archive_is_rejected_without_touching_live_database(self):
        payload, _ = backups.create_archive()
        safety_before = set((config.DATA_DIR / "backups").glob("*.db"))
        source = zipfile.ZipFile(io.BytesIO(payload))
        damaged = io.BytesIO()
        with source, zipfile.ZipFile(damaged, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                data = b"not a database" if name == backups.DATABASE_NAME else source.read(name)
                target.writestr(name, data)

        with self.assertRaisesRegex(backups.BackupError, "checksum"):
            backups.restore_archive(damaged.getvalue())
        conn = db.connect()
        try:
            title = conn.execute("SELECT title FROM movies WHERE id = ?", (self.movie_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(title, "Original film")
        self.assertEqual(set((config.DATA_DIR / "backups").glob("*.db")), safety_before)

    def test_admin_backup_routes_require_admin_dependency(self):
        for path in ("/api/admin/backup", "/api/admin/backup/restore"):
            route = next(route for route in main.app.routes if getattr(route, "path", None) == path)
            calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(auth.require_admin, calls)

    def test_admin_http_download_and_confirmed_multipart_restore(self):
        main.app.dependency_overrides[auth.require_admin] = lambda: {
            "id": self.member_id, "is_admin": True,
        }
        try:
            client = TestClient(main.app)
            downloaded = client.get("/api/admin/backup")
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.headers["cache-control"], "no-store")
            self.assertIn(".filmclub-backup", downloaded.headers["content-disposition"])

            rejected = client.post(
                "/api/admin/backup/restore",
                data={"confirmation": "restore"},
                files={"backup_file": ("backup.filmclub-backup", downloaded.content)},
            )
            self.assertEqual(rejected.status_code, 400)

            restored = client.post(
                "/api/admin/backup/restore",
                data={"confirmation": backups.RESTORE_CONFIRMATION},
                files={"backup_file": ("backup.filmclub-backup", downloaded.content)},
            )
            self.assertEqual(restored.status_code, 200)
            self.assertTrue(restored.json()["sessions_revoked"])
        finally:
            main.app.dependency_overrides.pop(auth.require_admin, None)


if __name__ == "__main__":
    unittest.main()
