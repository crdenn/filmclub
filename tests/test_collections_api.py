"""HTTP-level coverage for collection-curator authorization: a curator may
manage only the collections they created, an admin may manage any, and a
plain member may touch none of it.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "collections-api-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-collections-api-boot-"))

from app import config, db, main, settings


class CollectionsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-collections-api-")
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

        self.owner = TestClient(main.app)
        self.owner.__enter__()
        self.owner.post("/api/setup/owner", json={
            "setup_code": self.setup_code, "username": "Owner", "password": "owner-password",
        })
        self.owner.post("/api/setup", json={
            "setup_code": self.setup_code, "APP_URL": "http://testserver",
            "TMDB_API_KEY": "", "PLEX_URL": "", "PLEX_TOKEN": "", "PLEX_MACHINE_ID": "",
        })

        # A curator and a plain member, each via the invite/register flow.
        self.curator = self._invite_and_register("Curator")
        self.member = self._invite_and_register("Member")
        curator_id = self.curator.get("/api/me").json()["id"]
        self.owner.post(f"/api/admin/members/{curator_id}/curator",
                        json={"can_curate_collections": True})

    def tearDown(self):
        self.owner.__exit__(None, None, None)
        self.curator.__exit__(None, None, None)
        self.member.__exit__(None, None, None)
        self.validation.stop()
        config.DATA_DIR = self.old_data
        config.DB_PATH = self.old_db
        config.DEV_BYPASS_USER = self.old_dev
        for key, value in self.old_values.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    def _invite_and_register(self, name):
        invite = self.owner.post("/api/admin/invites", json={
            "email": f"{name.lower()}@example.test", "ttl_hours": 24,
        }).json()
        client = TestClient(main.app)
        client.__enter__()
        client.post("/auth/local/register", json={
            "code": invite["code"], "username": name, "password": f"{name.lower()}-password",
        })
        return client

    def _create(self, client, title):
        r = client.post("/api/collections", json={"title": title, "kind": "picked"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["slug"]

    def test_a_plain_member_cannot_create_a_collection(self):
        r = self.member.post("/api/collections", json={"title": "Nope", "kind": "picked"})
        self.assertEqual(r.status_code, 403)

    def test_a_curator_can_create_and_manage_their_own_collection(self):
        slug = self._create(self.curator, "Curator's Picks")
        patch_r = self.curator.patch(f"/api/collections/{slug}", json={"title": "Renamed"})
        self.assertEqual(patch_r.status_code, 200)
        delete_r = self.curator.delete(f"/api/collections/{slug}")
        self.assertEqual(delete_r.status_code, 200)

    def test_a_curator_cannot_manage_another_members_collection(self):
        owner_slug = self._create(self.owner, "Owner's Picks")

        patch_r = self.curator.patch(f"/api/collections/{owner_slug}", json={"title": "Hijack"})
        self.assertEqual(patch_r.status_code, 403)

        entry_r = self.curator.post(f"/api/collections/{owner_slug}/entries",
                                    json={"tmdb_id": 1})
        self.assertEqual(entry_r.status_code, 403)

        delete_r = self.curator.delete(f"/api/collections/{owner_slug}")
        self.assertEqual(delete_r.status_code, 403)

        # Untouched by the rejected calls.
        still_there = self.owner.get(f"/api/collections/{owner_slug}").json()
        self.assertEqual(still_there["title"], "Owner's Picks")

    def test_an_admin_can_manage_any_collection_including_a_curators(self):
        curator_slug = self._create(self.curator, "Curator's Picks")
        patch_r = self.owner.patch(f"/api/collections/{curator_slug}",
                                   json={"title": "Admin Renamed It"})
        self.assertEqual(patch_r.status_code, 200)

    def test_index_reports_editable_and_creator_name_per_viewer(self):
        curator_slug = self._create(self.curator, "Curator's Picks")

        as_curator = self.curator.get("/api/collections").json()
        row = next(c for c in as_curator["mine"] if c["slug"] == curator_slug)
        self.assertTrue(row["editable"])
        self.assertEqual(row["creator_name"], "Curator")

        as_member = self.member.get("/api/collections").json()
        # Published by default? No — new collections start unpublished, so a
        # plain reader shouldn't see it in the index at all.
        self.assertFalse(any(c["slug"] == curator_slug for c in as_member["mine"]))

    def test_a_curators_own_unpublished_draft_is_visible_only_to_them(self):
        curator_slug = self._create(self.curator, "Draft Collection")

        as_owner = self.owner.get("/api/collections").json()
        self.assertTrue(any(c["slug"] == curator_slug for c in as_owner["mine"]))

        as_member = self.member.get("/api/collections").json()
        self.assertFalse(any(c["slug"] == curator_slug for c in as_member["mine"]))

    def test_a_curator_cannot_grant_themself_admin_or_curator_rights(self):
        r = self.curator.post("/api/admin/invites", json={"email": "x@example.test"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
