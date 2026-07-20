"""Regression tests for non-destructive movie lifecycle reversals."""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "lifecycle-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import auth, config, db, service  # noqa: E402
from app.main import app  # noqa: E402


class LifecycleReversalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-lifecycle-")
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

    def _movie_with_history(self, status: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO movies
               (tmdb_id, title, year, director, language, overview, genres,
                suggested_by, status, watched_at, seen_before_snapshot, imdb_id)
               VALUES (99, 'Test Film', 2001, 'Director', 'English', 'Overview',
                       '["Drama"]', ?, ?, '2026-07-21', '{"1": false}', 'tt99')""",
            (self.alice_id, status),
        )
        movie_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO ratings (movie_id, member_id, score, seen_before, note) VALUES (?, ?, 4.5, 0, 'Keep me')",
            (movie_id, self.alice_id),
        )
        self.conn.execute(
            "INSERT INTO prior_views (movie_id, member_id, seen) VALUES (?, ?, 0)",
            (movie_id, self.bob_id),
        )
        self.conn.execute(
            "INSERT INTO votes (movie_id, member_id) VALUES (?, ?)",
            (movie_id, self.bob_id),
        )
        self.conn.commit()
        return movie_id

    def _snapshot(self, movie_id: int) -> dict:
        movie = dict(db.query_one(
            self.conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)
        ))
        movie.pop("status")
        return {
            "movie": movie,
            "ratings": [dict(r) for r in db.query_all(
                self.conn, "SELECT * FROM ratings WHERE movie_id = ?", (movie_id,)
            )],
            "prior_views": [dict(r) for r in db.query_all(
                self.conn, "SELECT * FROM prior_views WHERE movie_id = ?", (movie_id,)
            )],
            "votes": [dict(r) for r in db.query_all(
                self.conn, "SELECT * FROM votes WHERE movie_id = ?", (movie_id,)
            )],
        }

    def _assert_only_status_changed(self, movie_id: int, before: dict,
                                    expected_status: str) -> None:
        self.assertEqual(self._snapshot(movie_id), before)
        row = db.query_one(self.conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
        self.assertEqual(row["status"], expected_status)

    def test_admin_return_to_this_week_preserves_all_data(self):
        movie_id = self._movie_with_history("watched")
        before = self._snapshot(movie_id)

        self.assertTrue(service.return_to_this_week(self.conn, movie_id))

        self._assert_only_status_changed(movie_id, before, "scheduled")

    def test_unwatch_to_backlog_preserves_all_data(self):
        movie_id = self._movie_with_history("watched")
        before = self._snapshot(movie_id)

        self.assertTrue(service.unmark_watched(self.conn, movie_id))

        self._assert_only_status_changed(movie_id, before, "suggested")

    def test_unschedule_to_backlog_preserves_all_data(self):
        movie_id = self._movie_with_history("scheduled")
        before = self._snapshot(movie_id)

        self.assertTrue(service.unschedule_movie(self.conn, movie_id))

        self._assert_only_status_changed(movie_id, before, "suggested")

    def test_return_to_this_week_route_requires_admin_dependency(self):
        route = next(r for r in app.routes
                     if getattr(r, "path", None) == "/api/movies/{movie_id}/return-to-this-week")
        dependencies = [d.call for d in route.dependant.dependencies]

        self.assertIn(auth.require_admin, dependencies)
        with self.assertRaises(HTTPException) as raised:
            auth.require_admin({"is_admin": False})
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
