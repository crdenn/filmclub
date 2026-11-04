"""Regression tests for deactivating (and reactivating) a member who left."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "deactivation-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import auth, config, db, discord, plex_ratings, service, stats  # noqa: E402


class MemberDeactivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-deactivation-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self.owner_id = self._add_member("owner", "Owner", "#111111", is_owner=1)
        self.alice_id = self._add_member("alice", "Alice", "#123456")
        self.bob_id = self._add_member("bob", "Bob", "#654321")
        # Bob suggested a backlog film; Alice seconded it. Only Bob has marked
        # it unseen, so he's the sole reason it's eligible.
        self.backlog_id = self._add_movie("Backlog Film", self.bob_id, "suggested")
        self.conn.execute("INSERT INTO votes (movie_id, member_id) VALUES (?, ?)",
                          (self.backlog_id, self.alice_id))
        self.conn.execute("INSERT INTO votes (movie_id, member_id) VALUES (?, ?)",
                          (self.backlog_id, self.owner_id))
        self.conn.execute("INSERT INTO prior_views (movie_id, member_id, seen) VALUES (?, ?, 0)",
                          (self.backlog_id, self.bob_id))
        self.second_id = self._add_movie("Seconded By Bob", self.alice_id, "suggested")
        self.conn.execute("INSERT INTO votes (movie_id, member_id) VALUES (?, ?)",
                          (self.second_id, self.bob_id))
        # A watched film everyone rated.
        self.watched_id = self._add_movie("Watched Film", self.alice_id, "watched")
        for mid, score in ((self.owner_id, 4.0), (self.alice_id, 4.0), (self.bob_id, 1.0)):
            self.conn.execute(
                "INSERT INTO ratings (movie_id, member_id, score, seen_before) VALUES (?, ?, ?, 0)",
                (self.watched_id, mid, score))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _add_member(self, plex_id, username, color, is_owner=0) -> int:
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color, is_owner, is_admin) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"local:{plex_id}", username, color, is_owner, is_owner))
        self.conn.commit()
        return cur.lastrowid

    def _add_movie(self, title, suggested_by, status) -> int:
        cur = self.conn.execute(
            "INSERT INTO movies (title, suggested_by, status, watched_at) VALUES (?, ?, ?, ?)",
            (title, suggested_by, status, "2026-10-01" if status == "watched" else None))
        return cur.lastrowid

    def _deactivate_bob(self):
        service.set_member_active(self.conn, self.bob_id, False, self.owner_id)

    def test_guards(self):
        with self.assertRaises(ValueError):
            service.set_member_active(self.conn, self.alice_id, False, self.alice_id)
        with self.assertRaises(ValueError):
            service.set_member_active(self.conn, self.owner_id, False, self.alice_id)
        with self.assertRaises(LookupError):
            service.set_member_active(self.conn, 9999, False, self.owner_id)

    def test_sessions_revoked_and_sign_in_refused(self):
        token = auth.create_session(self.bob_id)
        self.assertIsNotNone(auth.resolve_session(token))
        self._deactivate_bob()
        self.assertIsNone(auth.resolve_session(token))
        with self.assertRaises(auth.MemberDeactivated):
            auth.create_session(self.bob_id)
        service.set_member_active(self.conn, self.bob_id, True, self.owner_id)
        self.assertIsNotNone(auth.resolve_session(auth.create_session(self.bob_id)))

    def test_excluded_from_club_counts(self):
        self._deactivate_bob()
        self.assertNotIn(self.bob_id, [m["id"] for m in service.all_members(self.conn)])
        backlog = {m["id"]: m for m in service.backlog(self.conn, self.alice_id)}
        film = backlog[self.backlog_id]
        # Bob was the only confirmed-unseen member, so it's no longer eligible.
        self.assertEqual(film["coverage"]["eligibility"], "unconfirmed")
        self.assertEqual(film["coverage"]["total_members"], 2)
        # His suggestion stays, still credited to him and flagged as former.
        self.assertEqual(film["suggester"]["id"], self.bob_id)
        self.assertFalse(film["suggester"]["active"])
        # His second no longer counts.
        self.assertEqual(backlog[self.second_id]["vote_count"], 0)

    def test_excluded_from_discord_digest(self):
        self._deactivate_bob()
        gaps = discord.build_digest(self.conn)["gaps"]
        self.assertNotIn(self.bob_id, [g["member"]["id"] for g in gaps])

    def test_watched_history_kept(self):
        self._deactivate_bob()
        watched = {m["id"]: m for m in service.watched(self.conn, self.alice_id)}[self.watched_id]
        self.assertEqual(watched["rating_count"], 3)
        self.assertEqual(watched["total_members"], 3)
        self.assertEqual(watched["avg_rating"], 3.0)
        detail = service.movie_detail(self.conn, self.watched_id, self.alice_id)
        self.assertEqual(len(detail["ratings"]), 3)
        self.assertIn(self.bob_id, [m["id"] for m in detail["members"]])

    def test_stats_split(self):
        self._deactivate_bob()
        result = stats.compute(self.conn)
        self.assertEqual(result["totals"]["members"], 2)
        self.assertEqual(result["totals"]["ratings"], 3)
        self.assertEqual(result["totals"]["group_mean"], 3.0)
        self.assertNotIn(self.bob_id, [p["member"]["id"] for p in result["rater_profiles"]])
        self.assertNotIn(str(self.bob_id), "".join(result["agreement_matrix"]["cells"]).split(":"))

    def test_webhook_ignores_former_member(self):
        self.conn.execute("UPDATE members SET plex_account_id = '42' WHERE id = ?", (self.bob_id,))
        self.conn.commit()
        self.assertIsNotNone(plex_ratings._member_for_webhook(self.conn, {"id": 42}))
        self._deactivate_bob()
        self.assertIsNone(plex_ratings._member_for_webhook(self.conn, {"id": 42}))
        self.assertIsNone(plex_ratings._member_for_webhook(self.conn, {"title": "Bob"}))

    def test_reactivation_restores_everything(self):
        before = service.backlog(self.conn, self.alice_id)
        self._deactivate_bob()
        service.set_member_active(self.conn, self.bob_id, True, self.owner_id)
        self.assertEqual(service.backlog(self.conn, self.alice_id), before)
        admin = {m["id"]: m for m in service.admin_members(self.conn)}
        self.assertTrue(admin[self.bob_id]["active"])


if __name__ == "__main__":
    unittest.main()
