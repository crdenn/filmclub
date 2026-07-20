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

    def test_first_local_owner_is_admin_owner_and_single(self):
        self.conn.execute("DELETE FROM members")
        self.conn.commit()
        owner_id = accounts.create_first_owner("ClubOwner", "owner-password")
        owner = db.query_one(
            self.conn, "SELECT plex_id, is_admin, is_owner FROM members WHERE id = ?", (owner_id,)
        )
        self.assertTrue(owner["plex_id"].startswith("local:"))
        self.assertEqual((owner["is_admin"], owner["is_owner"]), (1, 1))
        self.assertEqual(accounts.authenticate_local("clubowner", "owner-password"), owner_id)
        with self.assertRaises(accounts.AccountError):
            accounts.create_first_owner("OtherOwner", "another-password")

    def test_linked_plex_login_resolves_to_existing_local_member(self):
        invite = accounts.create_invite(self.admin_id)
        member_id = accounts.redeem_invite(invite["code"], "LocalName", "password123")
        accounts.link_plex_identity(
            member_id,
            plex_id="uuid-linked",
            username="PlexName",
            email="member@example.test",
            thumb=None,
            plex_account_id="42",
            plex_token="plex-member-token",
        )
        providers = accounts.identity_providers(member_id)
        self.assertEqual(providers, ["local", "plex"])

        logged_in = auth.upsert_member(
            "uuid-linked", "PlexName2", "new@example.test", None,
            plex_account_id="42", plex_token="refreshed-token",
        )
        self.assertEqual(logged_in["id"], member_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM members").fetchone()[0], 2
        )

    def test_link_rejects_plex_identity_owned_by_another_member(self):
        invite = accounts.create_invite(self.admin_id)
        local_id = accounts.redeem_invite(invite["code"], "LocalName", "password123")
        with self.assertRaises(accounts.AccountError):
            accounts.link_plex_identity(
                local_id,
                plex_id="uuid-admin",
                username="Admin",
                email=None,
                thumb=None,
                plex_account_id="1",
                plex_token="token",
            )

    def test_password_reset_is_hashed_single_use_and_revokes_sessions(self):
        invite = accounts.create_invite(self.admin_id)
        member_id = accounts.redeem_invite(invite["code"], "ResetMe", "old-password")
        old_session = auth.create_session(member_id)
        reset = accounts.create_password_reset(self.admin_id, member_id)
        stored = db.query_one(
            self.conn, "SELECT token_hash FROM password_resets WHERE member_id = ?", (member_id,)
        )
        self.assertNotEqual(stored["token_hash"], reset["token"])
        self.assertNotIn(reset["token"], stored["token_hash"])
        self.assertTrue(accounts.password_reset_status(reset["token"])["valid"])

        self.assertEqual(
            accounts.redeem_password_reset(reset["token"], "new-password"), member_id
        )
        self.assertIsNone(auth.resolve_session(old_session))
        self.assertIsNone(accounts.authenticate_local("ResetMe", "old-password"))
        self.assertEqual(accounts.authenticate_local("ResetMe", "new-password"), member_id)
        self.assertFalse(accounts.password_reset_status(reset["token"])["valid"])
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_password_reset(reset["token"], "third-password")

    def test_password_reset_requires_local_identity_and_honors_expiry(self):
        with self.assertRaises(accounts.AccountError):
            accounts.create_password_reset(self.admin_id, self.admin_id)
        invite = accounts.create_invite(self.admin_id)
        member_id = accounts.redeem_invite(invite["code"], "ExpireMe", "old-password")
        reset = accounts.create_password_reset(self.admin_id, member_id)
        self.conn.execute(
            "UPDATE password_resets SET expires_at = datetime('now', '-1 hour') "
            "WHERE member_id = ?",
            (member_id,),
        )
        self.conn.commit()
        self.assertFalse(accounts.password_reset_status(reset["token"])["valid"])
        with self.assertRaises(accounts.AccountError):
            accounts.redeem_password_reset(reset["token"], "new-password")


if __name__ == "__main__":
    unittest.main()
