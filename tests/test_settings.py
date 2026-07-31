"""Tests for encrypted UI-managed application settings."""
import json
import sqlite3
import tempfile
import time
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

    def test_public_values_does_not_blank_out_a_legitimate_zero(self):
        # A falsy-but-set value (e.g. weekday/hour 0) must still read back as
        # its real value and as "configured" — `effective or ""` would
        # wrongly treat 0 the same as unset.
        settings.save({"DISCORD_REMINDER_WEEKDAY": 0, "DISCORD_REMINDER_HOUR": 0})
        public = settings.public_values()
        self.assertEqual(public["DISCORD_REMINDER_WEEKDAY"]["value"], "0")
        self.assertTrue(public["DISCORD_REMINDER_WEEKDAY"]["configured"])
        self.assertEqual(public["DISCORD_REMINDER_HOUR"]["value"], "0")
        self.assertTrue(public["DISCORD_REMINDER_HOUR"]["configured"])

    def test_encryption_follows_data_key_not_signing_secret(self):
        old_data_key, old_signing = config.DATA_KEY, config.SESSION_SECRET
        try:
            config.DATA_KEY = "data-key-A"
            settings.save({"TMDB_API_KEY": "s3cr3t-value"})
            self.assertEqual(config.TMDB_API_KEY, "s3cr3t-value")
            # Rotating the cookie-signing secret must not affect decryption.
            config.SESSION_SECRET = "rotated-signing-secret"
            settings.load_into_config()
            self.assertEqual(config.TMDB_API_KEY, "s3cr3t-value")
            # Changing the data key does make the stored secret unreadable.
            config.DATA_KEY = "data-key-B"
            with self.assertRaises(RuntimeError):
                settings.load_into_config()
        finally:
            config.DATA_KEY, config.SESSION_SECRET = old_data_key, old_signing


class SetupCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-setup-")
        self.old_data = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        config.DATA_DIR = self.old_data
        self.tmp.cleanup()

    def _state(self) -> dict:
        return json.loads((Path(self.tmp.name) / "setup_code").read_text())

    def _patch_state(self, **changes) -> None:
        state = self._state()
        state.update(changes)
        (Path(self.tmp.name) / "setup_code").write_text(json.dumps(state))

    def test_code_is_hashed_at_rest_and_verifies(self):
        code, _ = settings.ensure_setup_code()
        self.assertTrue(code)
        raw = (Path(self.tmp.name) / "setup_code").read_text()
        self.assertNotIn(code, raw)  # plaintext never touches disk
        self.assertEqual(settings.verify_setup_code(code), "ok")
        self.assertEqual(settings.verify_setup_code(code.lower()), "ok")  # case-insensitive
        self.assertEqual(settings.verify_setup_code("AAAA-BBBB-CCCC"), "invalid")

    def test_active_code_is_not_regenerated(self):
        code, _ = settings.ensure_setup_code()
        again, _ = settings.ensure_setup_code()
        self.assertIsNone(again)  # existing code kept; plaintext not re-emitted
        self.assertEqual(settings.verify_setup_code(code), "ok")

    def test_expired_code_is_invalid_and_rotates(self):
        code, _ = settings.ensure_setup_code()
        self._patch_state(expires=time.time() - 1)
        self.assertEqual(settings.verify_setup_code(code), "invalid")
        fresh, _ = settings.ensure_setup_code()
        self.assertTrue(fresh)
        self.assertNotEqual(fresh, code)

    def test_rate_limit_locks_after_max_attempts(self):
        settings.ensure_setup_code()
        for _ in range(settings.SETUP_MAX_ATTEMPTS):
            self.assertEqual(settings.verify_setup_code("AAAA-BBBB-CCCC"), "invalid")
        self.assertEqual(settings.verify_setup_code("AAAA-BBBB-CCCC"), "locked")

    def test_attempt_window_resets(self):
        code, _ = settings.ensure_setup_code()
        for _ in range(settings.SETUP_MAX_ATTEMPTS):
            settings.verify_setup_code("AAAA-BBBB-CCCC")
        self.assertEqual(settings.verify_setup_code("AAAA-BBBB-CCCC"), "locked")
        self._patch_state(window_reset=time.time() - 1)  # window elapsed
        self.assertEqual(settings.verify_setup_code(code), "ok")

    def test_remove_setup_code(self):
        settings.ensure_setup_code()
        settings.remove_setup_code()
        self.assertEqual(settings.verify_setup_code("anything"), "invalid")


if __name__ == "__main__":
    unittest.main()
