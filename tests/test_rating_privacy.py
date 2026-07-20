"""Regression tests for hiding scheduled-film ratings until archive."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "rating-privacy-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import config, db, service, stats  # noqa: E402


class RatingPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-rating-privacy-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.alice_id = self._add_member("alice", "Alice", "#123456")
        self.bob_id = self._add_member("bob", "Bob", "#654321")

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _add_member(self, plex_id: str, username: str, color: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES (?, ?, ?)",
            (plex_id, username, color),
        )
        self.conn.commit()
        return cur.lastrowid

    def _add_movie(self, title: str, status: str, suggester_id: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO movies (title, status, suggested_by, seen_before_snapshot)
               VALUES (?, ?, ?, '{}')""",
            (title, status, suggester_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_scheduled_detail_returns_only_requesting_members_rating(self):
        movie_id = self._add_movie("Private Pick", "scheduled", self.alice_id)
        service.upsert_rating(self.conn, movie_id, self.alice_id, 4.5, False, "Alice secret")
        service.upsert_rating(self.conn, movie_id, self.bob_id, 1.0, True, "Bob secret")

        alice_view = service.movie_detail(self.conn, movie_id, self.alice_id)
        bob_view = service.movie_detail(self.conn, movie_id, self.bob_id)

        self.assertFalse(alice_view["ratings_public"])
        self.assertEqual([r["member_id"] for r in alice_view["ratings"]], [self.alice_id])
        self.assertEqual([r["member_id"] for r in bob_view["ratings"]], [self.bob_id])
        self.assertIsNone(alice_view["avg_rating"])
        self.assertEqual(alice_view["rating_count"], 2)
        self.assertNotIn("Bob secret", repr(alice_view))
        self.assertNotIn("Alice secret", repr(bob_view))

    def test_watched_detail_reveals_all_ratings(self):
        movie_id = self._add_movie("Revealed Pick", "watched", self.alice_id)
        service.upsert_rating(self.conn, movie_id, self.alice_id, 4.5, False, None)
        service.upsert_rating(self.conn, movie_id, self.bob_id, 1.0, True, None)

        detail = service.movie_detail(self.conn, movie_id, self.alice_id)

        self.assertTrue(detail["ratings_public"])
        self.assertEqual({r["member_id"] for r in detail["ratings"]},
                         {self.alice_id, self.bob_id})
        self.assertEqual(detail["avg_rating"], 2.75)

    def test_public_profile_hides_scheduled_rating_but_owner_can_see_it(self):
        scheduled_id = self._add_movie("Private Pick", "scheduled", self.alice_id)
        watched_id = self._add_movie("Public Pick", "watched", self.alice_id)
        service.upsert_rating(self.conn, scheduled_id, self.alice_id, 4.5, False, "private")
        service.upsert_rating(self.conn, scheduled_id, self.bob_id, 1.0, False, None)
        service.upsert_rating(self.conn, watched_id, self.alice_id, 3.0, False, "public")

        public = service.member_profile(self.conn, self.alice_id, self.bob_id)
        owner = service.member_profile(self.conn, self.alice_id, self.alice_id)
        scheduled_suggestion = next(s for s in public["suggestions"]
                                    if s["id"] == scheduled_id)

        self.assertEqual([r["movie"]["id"] for r in public["ratings"]], [watched_id])
        self.assertEqual({r["movie"]["id"] for r in owner["ratings"]},
                         {scheduled_id, watched_id})
        self.assertEqual(public["stats"]["ratings_count"], 1)
        self.assertEqual(owner["stats"]["ratings_count"], 2)
        self.assertIsNone(scheduled_suggestion["avg_rating"])
        self.assertEqual(scheduled_suggestion["rating_count"], 0)
        self.assertNotIn("private", repr(public))

    def test_shared_overviews_and_stats_do_not_leak_scheduled_scores(self):
        scheduled_id = self._add_movie("Private Pick", "scheduled", self.alice_id)
        watched_id = self._add_movie("Public Pick", "watched", self.alice_id)
        service.upsert_rating(self.conn, scheduled_id, self.alice_id, 5.0, False, None)
        service.upsert_rating(self.conn, scheduled_id, self.bob_id, 5.0, False, None)
        service.upsert_rating(self.conn, watched_id, self.alice_id, 2.0, False, None)

        this_week = service.this_week(self.conn)[0]
        report = stats.compute(self.conn)

        self.assertNotIn("avg_rating", this_week)
        self.assertNotIn("rating_count", this_week)
        self.assertEqual(report["totals"]["ratings"], 1)
        self.assertEqual(report["totals"]["group_mean"], 2.0)


if __name__ == "__main__":
    unittest.main()
