"""Tests for the Spin wheel: which films can be picked, and how it degrades.

Plex is never contacted — the library cache is a module-level dict, so a fake
snapshot is all the pool logic needs.
"""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "spin-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-spin-boot-"))

from app import config, db, events, main, plex, service  # noqa: E402


def fake_library(**overrides) -> dict:
    """A usable library snapshot, shaped exactly like plex._library."""
    snapshot = {
        "tmdb": set(), "imdb": set(),
        "rk_tmdb": {}, "rk_imdb": {},
        "rt_tmdb": {}, "rt_imdb": {},
        "machine_id": "server-uuid", "last_refresh": 0.0, "ok": True,
    }
    snapshot.update(overrides)
    return snapshot


class SpinPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-spin-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.alice = self._add_member("alice", "Alice")
        self.bob = self._add_member("bob", "Bob")
        self._library_backup = copy.deepcopy(plex._library)

    def tearDown(self):
        plex._library.clear()
        plex._library.update(self._library_backup)
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _add_member(self, plex_id: str, username: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES (?, ?, '#123456')",
            (plex_id, username))
        self.conn.commit()
        return cur.lastrowid

    def _set_library(self, **overrides) -> None:
        plex._library.clear()
        plex._library.update(fake_library(**overrides))

    def test_tracked_films_are_excluded_whatever_their_status(self):
        self._set_library(tmdb={1, 2, 3, 4})
        self.conn.execute("INSERT INTO movies (tmdb_id, title, status) VALUES (2, 'Watched', 'watched')")
        self.conn.execute("INSERT INTO movies (tmdb_id, title, status) VALUES (3, 'Backlog', 'suggested')")
        self.conn.commit()
        # A film the club has already watched is no more spinnable than one
        # sitting on the backlog: both are already on the list.
        self.assertEqual(service.spin_pool(self.conn, self.alice), [1, 4])

    def test_film_tracked_only_by_imdb_id_is_still_excluded(self):
        # movies.tmdb_id is nullable, so exclusion has to resolve through the
        # library cache's shared ratingKey to catch these older rows.
        self._set_library(tmdb={5, 6}, rk_tmdb={5: "k9", 6: "k4"}, rk_imdb={"tt0001": "k9"})
        self.conn.execute("INSERT INTO movies (title, imdb_id) VALUES ('Legacy', 'tt0001')")
        self.conn.commit()
        self.assertEqual(service.spin_pool(self.conn, self.alice), [6])

    def test_saved_films_are_excluded_for_that_member_only(self):
        self._set_library(tmdb={7, 8})
        service.save_film(self.conn, self.alice, {"tmdb_id": 7, "title": "Alice's Maybe"})
        self.assertEqual(service.spin_pool(self.conn, self.alice), [8])
        self.assertEqual(service.spin_pool(self.conn, self.bob), [7, 8])

    def test_pool_is_empty_when_the_library_cache_is_empty(self):
        self._set_library(tmdb=set())
        self.assertEqual(service.spin_pool(self.conn, self.alice), [])


class SpinEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-spin-api-")
        self.old_db_path = config.DB_PATH
        self.old_dev = config.DEV_BYPASS_USER
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.DEV_BYPASS_USER = "Alice"
        db.init_db()
        self._library_backup = copy.deepcopy(plex._library)
        self.client = TestClient(main.app)

    def tearDown(self):
        plex._library.clear()
        plex._library.update(self._library_backup)
        config.DB_PATH = self.old_db_path
        config.DEV_BYPASS_USER = self.old_dev
        self.tmp.cleanup()

    def _set_library(self, **overrides) -> None:
        plex._library.clear()
        plex._library.update(fake_library(**overrides))

    def test_unconfigured_plex_is_a_state_not_an_error(self):
        self._set_library(ok=False, tmdb={1})
        res = self.client.get("/api/spin")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "unavailable")
        self.assertIsNone(res.json()["movie"])

    def test_exhausted_pool_reports_empty(self):
        self._set_library(tmdb=set())
        res = self.client.get("/api/spin")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "empty")

    def test_a_dead_tmdb_id_is_retried_rather_than_failing_the_spin(self):
        self._set_library(tmdb={11, 12}, rk_tmdb={11: "a", 12: "b"})
        details = AsyncMock(side_effect=[
            RuntimeError("404 from TMDB"),
            {"tmdb_id": 12, "title": "Second Try", "imdb_id": "tt0002", "genres": []},
        ])
        with patch.object(main.tmdb, "details", new=details):
            res = self.client.get("/api/spin")
        body = res.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["movie"]["title"], "Second Try")
        # `library` rides along inline so the client's Plex/RT helpers work on
        # this payload exactly as they do on every other movie.
        self.assertTrue(body["movie"]["library"]["in_library"])
        self.assertEqual(details.await_count, 2)

    def test_spinning_does_not_notify_other_clients(self):
        self._set_library(tmdb=set())
        with patch.object(events, "broadcast") as broadcast:
            self.client.get("/api/spin")
        broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
