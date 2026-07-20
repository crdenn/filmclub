"""Tests for revocable server-side sessions."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "sessions-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-sessions-boot-"))

from app import auth, config, db  # noqa: E402
from app.colors import color_for  # noqa: E402


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-sessions-")
        self.old_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.member_id = self._add_member()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _add_member(self, plex_id="uuid-1"):
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES (?, ?, ?)",
            (plex_id, "Alice", color_for(plex_id)),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_create_and_resolve(self):
        token = auth.create_session(self.member_id)
        row = auth.resolve_session(token)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], self.member_id)

    def test_token_is_hashed_at_rest(self):
        token = auth.create_session(self.member_id)
        stored = self.conn.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
        self.assertNotEqual(stored, token)  # raw token never stored
        self.assertEqual(stored, auth._hash_token(token))

    def test_unknown_token_resolves_none(self):
        auth.create_session(self.member_id)
        self.assertIsNone(auth.resolve_session("not-a-real-token"))
        self.assertIsNone(auth.resolve_session(None))

    def test_revoke_single_session(self):
        token = auth.create_session(self.member_id)
        auth.revoke_session(token)
        self.assertIsNone(auth.resolve_session(token))

    def test_revoke_all_sessions(self):
        first = auth.create_session(self.member_id)
        second = auth.create_session(self.member_id)
        removed = auth.revoke_all_sessions()
        self.assertEqual(removed, 2)
        self.assertIsNone(auth.resolve_session(first))
        self.assertIsNone(auth.resolve_session(second))

    def test_expired_session_not_resolved_then_purged(self):
        token = auth.create_session(self.member_id)
        self.conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 day')")
        self.conn.commit()
        self.assertIsNone(auth.resolve_session(token))
        auth.purge_expired_sessions()
        remaining = self.conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        self.assertEqual(remaining, 0)

    def test_deleting_member_cascades_sessions(self):
        auth.create_session(self.member_id)
        self.conn.execute("DELETE FROM members WHERE id = ?", (self.member_id,))
        self.conn.commit()
        remaining = self.conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
