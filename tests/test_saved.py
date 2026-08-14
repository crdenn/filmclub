"""Tests for the private saved-films shortlist.

The guarantee under test is privacy: a member's shortlist is theirs alone, and
saving to it is invisible to everyone else — including over the live event
stream, which would otherwise repaint every open client.
"""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "saved-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-saved-boot-"))

from app import config, db, events, main, plex, service  # noqa: E402

FILM = {
    "tmdb_id": 42, "imdb_id": "tt0042", "title": "The Maybe",
    "year": 1999, "runtime": 101, "director": "A Director",
    "language": "French", "content_rating": "PG-13",
    "overview": "A film one might watch.", "genres": ["Drama", "Comedy"],
    "poster_url": "https://example.invalid/p.jpg", "backdrop_url": None,
}


class SavedFilmsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-saved-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.alice = self._add_member("alice", "Alice")
        self.bob = self._add_member("bob", "Bob")
        self._library_backup = copy.deepcopy(plex._library)
        plex._library.update(ok=False)     # no Plex in these tests

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

    def test_saving_the_same_film_twice_keeps_one_row(self):
        service.save_film(self.conn, self.alice, FILM)
        service.save_film(self.conn, self.alice, FILM)
        self.assertEqual(len(service.saved_films(self.conn, self.alice)), 1)

    def test_a_shortlist_is_visible_only_to_its_owner(self):
        service.save_film(self.conn, self.alice, FILM)
        self.assertEqual([f["title"] for f in service.saved_films(self.conn, self.alice)],
                         ["The Maybe"])
        self.assertEqual(service.saved_films(self.conn, self.bob), [])

    def test_snapshot_round_trips_the_full_metadata(self):
        service.save_film(self.conn, self.alice, FILM)
        film = service.saved_films(self.conn, self.alice)[0]
        for field in ("title", "year", "runtime", "director", "language",
                      "content_rating", "overview", "imdb_id"):
            self.assertEqual(film[field], FILM[field], field)
        self.assertEqual(film["genres"], ["Drama", "Comedy"])

    def test_tracked_marks_films_the_club_has_since_taken_up(self):
        service.save_film(self.conn, self.alice, FILM)
        self.assertFalse(service.saved_films(self.conn, self.alice)[0]["tracked"])
        self.conn.execute("INSERT INTO movies (tmdb_id, title) VALUES (42, 'The Maybe')")
        self.conn.commit()
        film = service.saved_films(self.conn, self.alice)[0]
        # Flagged, not removed: the film shouldn't vanish from your own list
        # because it got suggested to the club.
        self.assertTrue(film["tracked"])

    def test_unsave_reports_whether_anything_was_removed(self):
        service.save_film(self.conn, self.alice, FILM)
        self.assertFalse(service.unsave_film(self.conn, self.bob, 42))
        self.assertTrue(service.unsave_film(self.conn, self.alice, 42))
        self.assertEqual(service.saved_films(self.conn, self.alice), [])


class SavedFilmsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-saved-api-")
        self.old_db_path = config.DB_PATH
        self.old_dev = config.DEV_BYPASS_USER
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.DEV_BYPASS_USER = "Alice"
        db.init_db()
        self._library_backup = copy.deepcopy(plex._library)
        plex._library.update(ok=False)
        self.details = patch.object(main.tmdb, "details", new=AsyncMock(return_value=FILM))
        self.details.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.details.stop()
        plex._library.clear()
        plex._library.update(self._library_backup)
        config.DB_PATH = self.old_db_path
        config.DEV_BYPASS_USER = self.old_dev
        self.tmp.cleanup()

    def test_save_list_and_remove_round_trip(self):
        self.assertEqual(self.client.post("/api/saved", json={"tmdb_id": 42}).status_code, 200)
        # Saving twice is a no-op rather than a conflict.
        self.assertEqual(self.client.post("/api/saved", json={"tmdb_id": 42}).status_code, 200)
        items = self.client.get("/api/saved").json()["items"]
        self.assertEqual([f["tmdb_id"] for f in items], [42])
        self.assertEqual(self.client.delete("/api/saved/42").status_code, 200)
        self.assertEqual(self.client.get("/api/saved").json()["items"], [])

    def test_removing_a_film_that_was_never_saved_is_a_404(self):
        self.assertEqual(self.client.delete("/api/saved/999").status_code, 404)

    def test_another_member_never_sees_the_shortlist(self):
        self.client.post("/api/saved", json={"tmdb_id": 42})
        config.DEV_BYPASS_USER = "Bob"
        self.assertEqual(self.client.get("/api/saved").json()["items"], [])
        # And Bob can't reach into it either.
        self.assertEqual(self.client.delete("/api/saved/42").status_code, 404)

    def test_saving_does_not_notify_other_clients(self):
        # Private state: nobody else's view changes, so a broadcast here would
        # repaint every open client for nothing.
        with patch.object(events, "broadcast") as broadcast:
            self.client.post("/api/saved", json={"tmdb_id": 42})
            self.client.delete("/api/saved/42")
        broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
