"""HTTP-level coverage for local-account setup, invites, login, and resets."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "account-api-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-account-api-boot-"))

from app import config, db, main, settings


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-account-api-")
        self.old_data = config.DATA_DIR
        self.old_db = config.DB_PATH
        self.old_dev = config.DEV_BYPASS_USER
        self.old_values = {key: getattr(config, key) for key in settings.FIELDS}
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = config.DATA_DIR / "filmclub.db"
        config.DEV_BYPASS_USER = ""
        config.TMDB_API_KEY = ""
        config.PLEX_URL = ""
        config.PLEX_TOKEN = ""
        config.PLEX_MACHINE_ID = ""
        config.APP_URL = "http://testserver"
        db.init_db()
        self.setup_code, _ = settings.ensure_setup_code()
        self.validation = patch.object(main, "_validate_settings", new=AsyncMock(return_value={}))
        self.validation.start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.validation.stop()
        config.DATA_DIR = self.old_data
        config.DB_PATH = self.old_db
        config.DEV_BYPASS_USER = self.old_dev
        for key, value in self.old_values.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    def test_local_only_setup_invite_login_and_password_reset(self):
        status = self.client.get("/api/setup/status").json()
        self.assertTrue(status["required"])
        self.assertTrue(status["owner_required"])
        self.assertFalse(status["plex_enabled"])

        owner = self.client.post("/api/setup/owner", json={
            "setup_code": self.setup_code,
            "username": "Owner",
            "password": "owner-password",
        })
        self.assertEqual(owner.status_code, 200)
        self.assertIn(config.SESSION_COOKIE, owner.cookies)

        configured = self.client.post("/api/setup", json={
            "setup_code": self.setup_code,
            "APP_URL": "http://testserver",
            "TMDB_API_KEY": "tmdb-test-key",
            "PLEX_URL": "",
            "PLEX_TOKEN": "",
            "PLEX_MACHINE_ID": "",
        })
        self.assertEqual(configured.status_code, 200)
        me = self.client.get("/api/me").json()
        self.assertTrue(me["is_owner"])
        self.assertEqual(me["identity_providers"], ["local"])
        self.assertFalse(me["plex_available"])

        config.PLEX_URL = "http://plex.example.test"
        config.PLEX_TOKEN = "server-token"
        config.PLEX_MACHINE_ID = "machine-1"
        with (patch.object(main.plex, "create_pin", new=AsyncMock(return_value={
                  "id": 7, "code": "pin-code",
              })),
              patch.object(main.plex, "auth_url", return_value="https://plex.example.test/auth")):
            started = self.client.get("/auth/plex/link", follow_redirects=False)
        self.assertEqual(started.status_code, 307)
        self.assertEqual(started.headers["location"], "https://plex.example.test/auth")
        with (patch.object(main.plex, "poll_pin", new=AsyncMock(return_value="member-token")),
              patch.object(main.plex, "has_server_access", new=AsyncMock(return_value=True)),
              patch.object(main.plex, "get_user", new=AsyncMock(return_value={
                  "uuid": "owner-plex-uuid", "username": "PlexOwner", "email": None,
                  "thumb": None, "account_id": "42",
              }))):
            linked = self.client.get("/auth/callback", follow_redirects=False)
        self.assertEqual(linked.status_code, 307)
        self.assertEqual(linked.headers["location"], "/#/profile")
        self.assertEqual(self.client.get("/api/me").json()["identity_providers"],
                         ["local", "plex"])

        created = self.client.post("/api/admin/invites", json={
            "email": "member@example.test", "ttl_hours": 24,
        })
        self.assertEqual(created.status_code, 200)
        invite = created.json()
        self.assertIn("#/invite/", invite["invite_url"])

        guest = TestClient(main.app)
        registered = guest.post("/auth/local/register", json={
            "code": invite["code"], "username": "Member", "password": "member-password",
        })
        self.assertEqual(registered.status_code, 200)
        member = guest.get("/api/me").json()
        self.assertEqual(member["username"], "Member")

        reset = self.client.post(
            f"/api/admin/members/{member['id']}/password-reset", json={"ttl_hours": 1}
        )
        self.assertEqual(reset.status_code, 200)
        token = reset.json()["reset_url"].rsplit("/", 1)[-1]
        self.assertTrue(guest.get(f"/auth/local/reset/{token}").json()["valid"])
        changed = guest.post("/auth/local/reset", json={
            "token": token, "password": "replacement-password",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(guest.get(f"/auth/local/reset/{token}").json()["valid"])

        guest.post("/auth/logout")
        self.assertEqual(guest.post("/auth/local/login", json={
            "username": "Member", "password": "member-password",
        }).status_code, 401)
        self.assertEqual(guest.post("/auth/local/login", json={
            "username": "member", "password": "replacement-password",
        }).status_code, 200)


if __name__ == "__main__":
    unittest.main()
