"""Tests for encrypted UI-managed application settings."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import config, db, settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-settings-")
        self.old_db = config.DB_PATH
        self.old_values = {key: getattr(config, key) for key in settings.FIELDS}
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db
        for key, value in self.old_values.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    def test_secret_is_encrypted_and_public_api_never_returns_it(self):
        settings.save({"TMDB_API_KEY": "top-secret-key"})
        conn = sqlite3.connect(config.DB_PATH)
        stored, encrypted = conn.execute(
            "SELECT value, encrypted FROM app_settings WHERE key='TMDB_API_KEY'"
        ).fetchone()
        conn.close()
        self.assertNotEqual(stored, "top-secret-key")
        self.assertEqual(encrypted, 1)
        public = settings.public_values()["TMDB_API_KEY"]
        self.assertEqual(public["value"], "")
        self.assertTrue(public["configured"])

    def test_saved_values_apply_at_runtime(self):
        settings.save({"PLEX_URL": "http://plex.local:32400/",
                       "PLEX_REFRESH_INTERVAL": 900})
        self.assertEqual(config.PLEX_URL, "http://plex.local:32400")
        self.assertEqual(config.PLEX_REFRESH_INTERVAL, 900)


if __name__ == "__main__":
    unittest.main()
