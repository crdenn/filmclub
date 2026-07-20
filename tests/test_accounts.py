"""Tests for invite-only local accounts, identities, and password hashing."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "accounts-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-accounts-boot-"))

from app import accounts, auth, config, db, passwords  # noqa: E402
from app.colors import color_for  # noqa: E402


class PasswordTests(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        h = passwords.hash_password("correct horse battery staple")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(passwords.verify_password("correct horse battery staple", h))
        self.assertFalse(passwords.verify_password("wrong password", h))

    def test_salt_makes_hashes_unique(self):
        self.assertNotEqual(passwords.hash_password("same"), passwords.hash_password("same"))

    def test_verify_tolerates_garbage(self):
        self.assertFalse(passwords.verify_password("x", None))
        self.assertFalse(passwords.verify_password("x", "not-a-valid-hash"))


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-accounts-")
        self.old_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.admin_id = self._add_plex_member("uuid-admin", "Admin")

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _add_plex_member(self, plex_id, username):
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES (?, ?, ?)",
            (plex_id, username, color_for(plex_id)),
        )
        self.conn.execute(
            "INSERT INTO identities (member_id, provider, provider_uid) VALUES (?, 'plex', ?)",
            (cur.lastrowid, plex_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_invite_redeem_login_flow(self):
        invite = accounts.create_invite(self.admin_id)
        self.assertTrue(accounts.invite_status(invite["code"])["valid"])

        member_id = accounts.redeem_invite(invite["code"], "NewUser", "hunter2hunter2")
        # Member exists with a synthetic local plex_id and a local identity.
        row = db.query_one(self.conn, "SELECT plex_id, username FROM members WHERE id = ?", (member_id,))
        self.assertTrue(row["plex_id"].startswith("local:"))
        self.assertEqual(row["username"], "NewUser")
        self.assertTrue(accounts.has_local_identity(member_id))

        # Password is hashed at rest, never stored in the clear.
        ident = db.query_one(self.conn, "SELECT password_hash FROM identities "
                             "WHERE member_id = ? AND provider = 'local'", (member_id,))
        self.assertNotIn("hunter2hunter2", ident["password_hash"])

        # Login works, is case-insensitive on username, and rejects bad passwords.
        self.assertEqual(accounts.authenticate_local("newuser", "hunter2hunter2"), member_id)
        self.assertIsNone(accounts.authenticate_local("NewUser", "wrong"))
        self.assertIsNone(accounts.authenticate_local("ghost", "whatever"))

    def test_invite_is_single_use(self):
        invite = accounts.create_invite(self.admin_id)
        accounts.redeem_invite(invite["code"], "First", "password123")
        self.assertFalse(accounts.invite_status(invite["code"])["valid"])
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_invite(invite["code"], "Second", "password123")

    def test_expired_invite_rejected(self):
        invite = accounts.create_invite(self.admin_id)
        self.conn.execute("UPDATE invites SET expires_at = datetime('now', '-1 hour')")
        self.conn.commit()
        self.assertFalse(accounts.invite_status(invite["code"])["valid"])
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_invite(invite["code"], "TooLate", "password123")

    def test_duplicate_username_rejected(self):
        i1 = accounts.create_invite(self.admin_id)
        i2 = accounts.create_invite(self.admin_id)
        accounts.redeem_invite(i1["code"], "Dupe", "password123")
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_invite(i2["code"], "dupe", "password123")  # case-insensitive

    def test_weak_credentials_rejected(self):
        invite = accounts.create_invite(self.admin_id)
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_invite(invite["code"], "ok_name", "short")
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_invite(invite["code"], "no", "password123")  # username too short
        # invite survives failed attempts
        self.assertTrue(accounts.invite_status(invite["code"])["valid"])

    def test_local_member_authenticates_without_plex_token(self):
        invite = accounts.create_invite(self.admin_id)
        member_id = accounts.redeem_invite(invite["code"], "LocalOnly", "password123")
        token = auth.create_session(member_id)
        member = auth.current_member(token)  # must not demand a Plex token
        self.assertEqual(member["id"], member_id)

    def test_migration_backfilled_plex_identity(self):
        row = db.query_one(self.conn, "SELECT provider, provider_uid FROM identities "
                          "WHERE member_id = ?", (self.admin_id,))
        self.assertEqual(row["provider"], "plex")
        self.assertEqual(row["provider_uid"], "uuid-admin")


if __name__ == "__main__":
    unittest.main()
