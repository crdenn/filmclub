"""Who picked each film, and whose turn it is to pick next."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "pick-order-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import config, db, service  # noqa: E402


class PickOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-picks-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        # Join order: Alice, Bob, Carl, Dana.
        self.alice = self._add_member("alice", "Alice", "2026-01-01")
        self.bob = self._add_member("bob", "Bob", "2026-01-02")
        self.carl = self._add_member("carl", "Carl", "2026-01-03")
        self.dana = self._add_member("dana", "Dana", "2026-01-04")

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _add_member(self, plex_id, username, created_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO members (plex_id, username, color, created_at) VALUES (?, ?, '#123456', ?)",
            (f"local:{plex_id}", username, created_at))
        self.conn.commit()
        return cur.lastrowid

    def _watched(self, title, suggested_by, picked_by, on) -> int:
        cur = self.conn.execute(
            "INSERT INTO movies (title, suggested_by, picked_by, status, watched_at) "
            "VALUES (?, ?, ?, 'watched', ?)", (title, suggested_by, picked_by, on))
        self.conn.commit()
        return cur.lastrowid

    def _backlog(self, title, suggested_by) -> int:
        cur = self.conn.execute(
            "INSERT INTO movies (title, suggested_by, status) VALUES (?, ?, 'suggested')",
            (title, suggested_by))
        self.conn.commit()
        return cur.lastrowid

    def _order(self) -> list[int]:
        return [e["member"]["id"] for e in service.pick_order(self.conn)]

    def test_never_picked_go_first_in_join_order(self):
        self._watched("A", self.bob, self.alice, "2026-10-01")
        self.assertEqual(self._order(), [self.bob, self.carl, self.dana, self.alice])

    def test_longest_since_last_pick_is_next(self):
        self._watched("1", self.alice, self.alice, "2026-10-01")
        self._watched("2", self.alice, self.bob, "2026-10-08")
        self._watched("3", self.alice, self.carl, "2026-10-15")
        self._watched("4", self.alice, self.dana, "2026-10-22")
        self._watched("5", self.alice, self.alice, "2026-10-29")
        self.assertEqual(self._order(), [self.bob, self.carl, self.dana, self.alice])
        first = service.pick_order(self.conn)[0]
        self.assertEqual(first["last_picked_on"], "2026-10-08")
        self.assertEqual(first["last_movie"]["title"], "2")

    def test_skipping_a_turn_keeps_it(self):
        for i, who in enumerate((self.alice, self.bob, self.carl, self.dana)):
            self._watched(f"r{i}", who, who, f"2026-09-0{i + 1}")
        # Alice is up but can't make it; Bob picks out of turn.
        self._watched("skip", self.alice, self.bob, "2026-10-01")
        self.assertEqual(self._order()[0], self.alice)

    def test_picker_is_independent_of_suggester(self):
        self._watched("Bob's suggestion", self.bob, self.alice, "2026-10-01")
        order = self._order()
        self.assertEqual(order[-1], self.alice)
        self.assertEqual(order[0], self.bob)

    def test_unknown_pickers_and_backlog_films_do_not_count(self):
        self._watched("Unknown", self.alice, None, "2026-10-01")
        unscheduled = self._backlog("Sent back", self.carl)
        self.conn.execute("UPDATE movies SET picked_by = ? WHERE id = ?", (self.alice, unscheduled))
        self.conn.commit()
        self.assertEqual(self._order(), [self.alice, self.bob, self.carl, self.dana])

    def test_deactivated_members_leave_the_order(self):
        service.set_member_active(self.conn, self.alice, False, self.bob)
        self.assertNotIn(self.alice, self._order())

    def test_schedule_records_chosen_picker_and_consumes_override(self):
        film = self._backlog("Film", self.bob)
        service.set_next_picker_override(self.conn, self.dana)
        self.assertEqual(self._order()[0], self.dana)
        self.assertTrue(service.pick_order(self.conn)[0]["override"])
        self.assertTrue(service.schedule_movie(self.conn, film, self.carl))
        row = db.query_one(self.conn, "SELECT picked_by, picked_at FROM movies WHERE id = ?", (film,))
        self.assertEqual(row["picked_by"], self.carl)
        self.assertIsNotNone(row["picked_at"])
        self.assertIsNone(service.next_picker_override(self.conn))
        self.assertEqual(self._order()[-1], self.carl)

    def test_schedule_defaults_to_whoever_is_up_next(self):
        self._watched("1", self.alice, self.alice, "2026-10-01")
        film = self._backlog("Film", self.alice)
        service.schedule_movie(self.conn, film)
        row = db.query_one(self.conn, "SELECT picked_by FROM movies WHERE id = ?", (film,))
        self.assertEqual(row["picked_by"], self.bob)

    def test_override_for_a_deactivated_member_is_ignored(self):
        service.set_next_picker_override(self.conn, self.dana)
        service.set_member_active(self.conn, self.dana, False, self.alice)
        self.assertIsNone(service.next_picker_override(self.conn))
        self.assertFalse(any(e["override"] for e in service.pick_order(self.conn)))

    def test_set_picker_only_on_scheduled_or_watched(self):
        film = self._watched("Old", self.alice, None, "2026-10-01")
        self.assertTrue(service.set_picker(self.conn, film, self.carl))
        history = service.pick_history(self.conn)
        self.assertEqual(history[0]["picker"]["id"], self.carl)
        self.assertEqual(history[0]["suggester"]["id"], self.alice)
        self.assertFalse(service.set_picker(self.conn, self._backlog("New", self.bob), self.bob))

    def test_unschedule_keeps_picker(self):
        film = self._backlog("Film", self.bob)
        service.schedule_movie(self.conn, film, self.carl)
        service.unschedule_movie(self.conn, film)
        row = db.query_one(self.conn, "SELECT picked_by FROM movies WHERE id = ?", (film,))
        self.assertEqual(row["picked_by"], self.carl)
        self.assertEqual(self._order()[-1], self.dana)  # but it no longer counts

    def test_payloads_carry_picker(self):
        film = self._backlog("Film", self.bob)
        service.schedule_movie(self.conn, film, self.alice)
        week = service.this_week(self.conn, self.alice)[0]
        self.assertEqual(week["picker"]["id"], self.alice)
        self.assertEqual(week["suggester"]["id"], self.bob)
        self.assertEqual(service.movie_detail(self.conn, film, self.bob)["picker"]["id"], self.alice)
        self.assertEqual(service.member_profile(self.conn, self.alice, self.alice)["stats"]["picked"], 1)

    def test_profile_lists_picks_and_turn_without_scores(self):
        mine = self._watched("Alice Picked", self.bob, self.alice, "2026-03-01")
        self._watched("Bob Picked", self.alice, self.bob, "2026-03-08")
        service.upsert_rating(self.conn, mine, self.bob, 2.0, False, None)
        prof = service.member_profile(self.conn, self.alice, self.bob)
        self.assertEqual([p["id"] for p in prof["picks"]], [mine])
        self.assertEqual(prof["picks"][0]["suggester"]["id"], self.bob)
        self.assertEqual(prof["picks"][0]["date"], "2026-03-01")
        # Carl and Dana have never picked, so they go first; Alice picked first.
        self.assertEqual(prof["turn"], {"position": 3, "of": 4})
        suggested = next(s for s in prof["suggestions"] if s["title"] == "Bob Picked")
        self.assertEqual(suggested["picker"]["id"], self.bob)
        self.assertEqual(prof["picks"][0]["avg_rating"], 2.0)
        self.assertEqual(prof["stats"]["picked_mean_received"], 2.0)


if __name__ == "__main__":
    unittest.main()
