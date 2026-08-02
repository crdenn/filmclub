"""Tests for curated-collection assembly and Plex resolution.

The load-bearing rule here is that resolution is tri-state. "Not on the server"
and "we cannot reach the server" must not collapse into each other: the first
hides an entry, the second must not, or an unreachable Plex would blank out an
entire page of writing.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "collections-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-collections-bootstrap-"))

from app import collections as coll  # noqa: E402
from app import config, db, plex  # noqa: E402


def _meta(tmdb_id: int, title: str, **extra) -> dict:
    meta = {
        "tmdb_id": tmdb_id,
        "title": title,
        "year": 1999,
        "runtime": 100,
        "director": "A Director",
        "imdb_id": f"tt{tmdb_id:07d}",
        "backdrop_url": "https://image.tmdb.org/t/p/w1280/x.jpg",
    }
    meta.update(extra)
    return meta


class CollectionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-collections-")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()

        # Stub the Plex library cache rather than the network: these tests are
        # about how resolution states are interpreted, not about Plex itself.
        self._old_library = dict(plex._library)
        plex._library.update(
            tmdb={11}, imdb=set(), rk_tmdb={11: "5001"}, rk_imdb={},
            rt_tmdb={}, rt_imdb={}, machine_id="machine-abc", ok=True,
        )

        self.collection_id = coll.create_collection(
            self.conn, "test-set", "Test Set", intro="Intro prose.", published=True,
        )
        coll.upsert_entry(self.conn, self.collection_id, _meta(11, "On The Server"),
                          blurb="Written.", position=0)
        coll.upsert_entry(self.conn, self.collection_id, _meta(22, "Gone Missing"),
                          blurb="Also written.", position=1)

    def tearDown(self):
        self.conn.close()
        plex._library.clear()
        plex._library.update(self._old_library)
        config.DB_PATH = self._old_db_path
        self.tmp.cleanup()

    def test_missing_entry_is_hidden_publicly_and_listed_for_admin(self):
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        self.assertEqual([e["title"] for e in public["entries"]], ["On The Server"])

        admin = coll.collection_detail(self.conn, "test-set", is_admin=True)
        self.assertEqual(len(admin["entries"]), 2)
        self.assertEqual([e["title"] for e in admin["unresolved"]], ["Gone Missing"])

    def test_resolved_entry_carries_a_per_film_deep_link(self):
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        entry = public["entries"][0]
        self.assertEqual(entry["plex_state"], coll.RESOLVED)
        self.assertIn("machine-abc", entry["plex_link"])
        self.assertIn("5001", entry["plex_link"])

    def test_unreachable_plex_shows_every_entry_without_links(self):
        """The distinction that matters: a cold cache must not hide writing."""
        plex._library["ok"] = False
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        self.assertEqual(len(public["entries"]), 2)
        self.assertEqual({e["plex_state"] for e in public["entries"]}, {coll.UNKNOWN})
        self.assertEqual({e["plex_link"] for e in public["entries"]}, {None})

        admin = coll.collection_detail(self.conn, "test-set", is_admin=True)
        self.assertEqual(admin["unresolved"], [])
        self.assertFalse(admin["plex_ready"])

    def test_unpublished_collection_is_admin_only(self):
        db.execute(self.conn, "UPDATE collections SET published = 0 WHERE id = ?",
                   (self.collection_id,))
        self.assertIsNone(coll.collection_detail(self.conn, "test-set", is_admin=False))
        self.assertIsNotNone(coll.collection_detail(self.conn, "test-set", is_admin=True))

    def test_director_collection_hides_films_without_a_blurb(self):
        db.execute(self.conn, "UPDATE collections SET kind = 'director' WHERE id = ?",
                   (self.collection_id,))
        coll.upsert_entry(self.conn, self.collection_id, _meta(11, "On The Server"),
                          blurb="", position=0)

        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        self.assertEqual(public["entries"], [])

        admin = coll.collection_detail(self.conn, "test-set", is_admin=True)
        self.assertEqual([e["title"] for e in admin["unwritten"]], ["On The Server"])

    def test_picked_collection_keeps_films_without_a_blurb(self):
        """Only director membership is blurb-gated; a picked film was chosen."""
        coll.upsert_entry(self.conn, self.collection_id, _meta(11, "On The Server"),
                          blurb="", position=0)
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        self.assertEqual([e["title"] for e in public["entries"]], ["On The Server"])

    def test_reseeding_refreshes_metadata_but_keeps_an_edited_blurb(self):
        coll.upsert_entry(self.conn, self.collection_id,
                          _meta(11, "Retitled", runtime=142), position=0)
        entries = coll.entries_for(self.conn, self.collection_id)
        entry = next(e for e in entries if e["tmdb_id"] == 11)
        self.assertEqual(entry["title"], "Retitled")
        self.assertEqual(entry["runtime"], 142)
        self.assertEqual(entry["blurb"], "Written.")


    def test_entries_report_whether_the_club_is_also_tracking_the_film(self):
        db.execute(self.conn,
                   "INSERT INTO movies (tmdb_id, title, status) VALUES (?, ?, ?)",
                   (11, "On The Server", "watched"))
        detail = coll.collection_detail(self.conn, "test-set", is_admin=True)
        by_tmdb = {e["tmdb_id"]: e for e in detail["entries"]}
        self.assertEqual(by_tmdb[11]["movie_status"], "watched")
        self.assertIsNotNone(by_tmdb[11]["movie_id"])
        # A film the club has never suggested has no detail page to link to.
        self.assertIsNone(by_tmdb[22]["movie_id"])
        self.assertIsNone(by_tmdb[22]["movie_status"])

    def test_club_state_is_attached_without_a_query_per_row(self):
        """One statement for the page, regardless of how many films are on it."""
        for tmdb_id in range(200, 215):
            coll.upsert_entry(self.conn, self.collection_id,
                              _meta(tmdb_id, f"Film {tmdb_id}"), blurb="x")
        entries = coll.entries_for(self.conn, self.collection_id)
        seen = []
        original = db.query_all

        def counting(conn, sql, params=()):
            seen.append(sql)
            return original(conn, sql, params)

        db.query_all = counting
        try:
            coll.attach_club_state(self.conn, entries)
        finally:
            db.query_all = original
        self.assertEqual(len(seen), 1)

    def test_preview_gives_an_admin_exactly_the_reader_view(self):
        admin = coll.collection_detail(self.conn, "test-set", is_admin=True)
        preview = coll.collection_detail(self.conn, "test-set", is_admin=True, preview=True)
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)

        self.assertEqual([e["title"] for e in preview["entries"]],
                         [e["title"] for e in public["entries"]])
        self.assertEqual(len(admin["entries"]), 2)      # admin still sees the broken one
        self.assertNotIn("unresolved", preview)
        self.assertNotIn("unwritten", preview)

    def test_preview_still_shows_an_unpublished_draft_to_its_author(self):
        """Preview gates the content, not the access — previewing a draft is
        the whole point of previewing."""
        db.execute(self.conn, "UPDATE collections SET published = 0 WHERE id = ?",
                   (self.collection_id,))
        preview = coll.collection_detail(self.conn, "test-set", is_admin=True, preview=True)
        self.assertIsNotNone(preview)
        self.assertFalse(preview["published"])
        # A genuine reader still gets nothing.
        self.assertIsNone(coll.collection_detail(self.conn, "test-set", is_admin=False))

    def test_all_lowercase_titles_are_capitalised_but_deliberate_casing_is_kept(self):
        self.assertEqual(coll.smart_title("david cronenberg"), "David Cronenberg")
        self.assertEqual(coll.smart_title("night drives"), "Night Drives")
        # Anything already carrying an uppercase letter is left exactly alone.
        self.assertEqual(coll.smart_title("eXistenZ"), "eXistenZ")
        self.assertEqual(coll.smart_title("JFK"), "JFK")
        self.assertEqual(coll.smart_title("Night Drives"), "Night Drives")
        self.assertEqual(coll.smart_title(""), "")

    def test_collections_default_to_authored_and_report_editability(self):
        c = coll.get_by_slug(self.conn, "test-set")
        self.assertEqual(c["origin"], "authored")
        self.assertTrue(c["editable"])

    def test_a_generated_collection_reports_itself_read_only(self):
        coll.create_collection(self.conn, "made-for-you", "Made For You",
                               origin="generated")
        c = coll.get_by_slug(self.conn, "made-for-you")
        self.assertEqual(c["origin"], "generated")
        self.assertFalse(c["editable"])

    def test_a_row_predating_the_origin_column_reads_as_authored(self):
        """Existing collections must keep their editing surface, not lose it."""
        self.assertTrue(coll.collection_base({"slug": "x", "published": 1})["editable"])

    def test_slugs_are_url_safe_and_unique(self):
        self.assertEqual(coll.slugify("Films for a Rainy Sunday!"), "films-for-a-rainy-sunday")
        self.assertEqual(coll.slugify("  ***  "), "collection")
        self.assertEqual(coll.slugify("Kurosawa: 1950–1965"), "kurosawa-1950-1965")

        first = coll.unique_slug(self.conn, "Night Drives")
        coll.create_collection(self.conn, first, "Night Drives")
        second = coll.unique_slug(self.conn, "Night Drives")
        self.assertEqual(first, "night-drives")
        self.assertEqual(second, "night-drives-2")

    def test_readding_a_film_refreshes_metadata_without_reordering(self):
        """Re-adding must not silently move a film to the end of the page."""
        coll.upsert_entry(self.conn, self.collection_id, _meta(33, "Third"), position=2)
        coll.upsert_entry(self.conn, self.collection_id, _meta(11, "On The Server", runtime=999))
        order = [e["tmdb_id"] for e in coll.entries_for(self.conn, self.collection_id)]
        self.assertEqual(order, [11, 22, 33])
        first = coll.entries_for(self.conn, self.collection_id)[0]
        self.assertEqual(first["runtime"], 999)

    def test_a_newly_added_film_goes_to_the_end(self):
        coll.upsert_entry(self.conn, self.collection_id, _meta(44, "Newest"))
        order = [e["tmdb_id"] for e in coll.entries_for(self.conn, self.collection_id)]
        self.assertEqual(order[-1], 44)

    def test_editing_a_blurb_stores_it_and_ungates_a_director_entry(self):
        db.execute(self.conn, "UPDATE collections SET kind = 'director' WHERE id = ?",
                   (self.collection_id,))
        entry = next(e for e in coll.entries_for(self.conn, self.collection_id)
                     if e["tmdb_id"] == 11)
        self.assertTrue(coll.update_entry(self.conn, self.collection_id, entry["id"], ""))
        self.assertEqual(coll.collection_detail(self.conn, "test-set", is_admin=False)["entries"], [])

        self.assertTrue(coll.update_entry(self.conn, self.collection_id, entry["id"],
                                          "Now written."))
        public = coll.collection_detail(self.conn, "test-set", is_admin=False)
        self.assertEqual([e["title"] for e in public["entries"]], ["On The Server"])
        self.assertEqual(public["entries"][0]["blurb"], "Now written.")

    def test_editing_an_entry_from_the_wrong_collection_does_nothing(self):
        other = coll.create_collection(self.conn, "other", "Other")
        entry = coll.entries_for(self.conn, self.collection_id)[0]
        self.assertFalse(coll.update_entry(self.conn, other, entry["id"], "nope"))
        unchanged = coll.entries_for(self.conn, self.collection_id)[0]
        self.assertEqual(unchanged["blurb"], "Written.")

    def test_updating_a_collection_patches_only_supplied_fields(self):
        self.assertTrue(coll.update_collection(self.conn, "test-set",
                                               {"intro": "New intro."}))
        c = coll.get_by_slug(self.conn, "test-set")
        self.assertEqual(c["intro"], "New intro.")
        self.assertEqual(c["title"], "Test Set")     # untouched
        self.assertTrue(c["published"])              # untouched

    def test_updating_a_collection_with_no_known_fields_is_a_no_op(self):
        self.assertFalse(coll.update_collection(self.conn, "test-set", {"bogus": 1}))
        self.assertEqual(coll.get_by_slug(self.conn, "test-set")["intro"], "Intro prose.")

    def test_deleting_an_entry_leaves_the_rest_of_the_collection(self):
        entries = coll.entries_for(self.conn, self.collection_id)
        target = next(e for e in entries if e["tmdb_id"] == 22)
        self.assertTrue(coll.delete_entry(self.conn, self.collection_id, target["id"]))
        remaining = coll.entries_for(self.conn, self.collection_id)
        self.assertEqual([e["tmdb_id"] for e in remaining], [11])
        # Already gone: a second delete reports nothing removed rather than raising.
        self.assertFalse(coll.delete_entry(self.conn, self.collection_id, target["id"]))

    def test_deleting_an_entry_from_the_wrong_collection_does_nothing(self):
        other = coll.create_collection(self.conn, "other", "Other")
        entries = coll.entries_for(self.conn, self.collection_id)
        self.assertFalse(coll.delete_entry(self.conn, other, entries[0]["id"]))
        self.assertEqual(len(coll.entries_for(self.conn, self.collection_id)), 2)

    def test_deleting_a_collection_cascades_to_its_entries(self):
        self.assertTrue(coll.delete_collection(self.conn, "test-set"))
        self.assertIsNone(coll.get_by_slug(self.conn, "test-set"))
        left = db.query_one(
            self.conn,
            "SELECT COUNT(*) AS n FROM collection_entries WHERE collection_id = ?",
            (self.collection_id,),
        )
        self.assertEqual(left["n"], 0)
        self.assertFalse(coll.delete_collection(self.conn, "test-set"))


class DirectorCoverageTests(unittest.TestCase):
    """The coverage view is the author's to-do list for a director page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-coverage-")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        self._old_library = dict(plex._library)
        plex._library.update(tmdb={101}, imdb=set(), rk_tmdb={101: "9"}, rk_imdb={},
                             rt_tmdb={}, rt_imdb={}, machine_id="m", ok=True)

        self.cid = coll.create_collection(self.conn, "kurosawa", "Kurosawa",
                                          kind="director", director_name="Akira Kurosawa")
        coll.upsert_entry(self.conn, self.cid, _meta(101, "Ikiru"), blurb="Written.")
        coll.upsert_entry(self.conn, self.cid, _meta(102, "Ran"), blurb="")

        self.filmography = [
            {"tmdb_id": 101, "title": "Ikiru", "year": 1952},
            {"tmdb_id": 102, "title": "Ran", "year": 1985},
            {"tmdb_id": 103, "title": "Dersu Uzala", "year": 1975},
        ]

    def tearDown(self):
        self.conn.close()
        plex._library.clear()
        plex._library.update(self._old_library)
        config.DB_PATH = self._old_db_path
        self.tmp.cleanup()

    def test_each_film_is_classified_and_counted(self):
        cov = coll.coverage(coll.entries_for(self.conn, self.cid), self.filmography)
        self.assertEqual([f["state"] for f in cov["films"]],
                         ["written", "blank", "untouched"])
        self.assertEqual((cov["written"], cov["blank"], cov["total"]), (1, 1, 3))

    def test_plex_presence_is_flagged_per_film(self):
        cov = coll.coverage(coll.entries_for(self.conn, self.cid), self.filmography)
        by_id = {f["tmdb_id"]: f for f in cov["films"]}
        self.assertTrue(by_id[101]["on_plex"])
        self.assertFalse(by_id[103]["on_plex"])

    def test_unknown_library_reports_none_not_false(self):
        """A cold cache must not claim every film is missing from the server."""
        plex._library["ok"] = False
        cov = coll.coverage(coll.entries_for(self.conn, self.cid), self.filmography)
        self.assertTrue(all(f["on_plex"] is None for f in cov["films"]))

    def test_entries_tmdb_does_not_credit_are_kept_as_extra(self):
        coll.upsert_entry(self.conn, self.cid, _meta(999, "Co-directed Oddity"),
                          blurb="Deliberate.")
        cov = coll.coverage(coll.entries_for(self.conn, self.cid), self.filmography)
        self.assertEqual([e["title"] for e in cov["extra"]], ["Co-directed Oddity"])
        self.assertNotIn(999, [f["tmdb_id"] for f in cov["films"]])

    def test_scaffold_snapshot_is_stored(self):
        coll.set_director_scaffold(self.conn, "kurosawa", {
            "tmdb_id": 5026, "name": "Akira Kurosawa",
            "portrait_url": "https://image.tmdb.org/t/p/w300/x.jpg",
            "born": "1910-03-23", "died": "1998-09-06",
        })
        c = coll.get_by_slug(self.conn, "kurosawa")
        self.assertEqual(c["director_tmdb_id"], 5026)
        self.assertEqual(c["director_born"], "1910-03-23")
        self.assertEqual(c["director_died"], "1998-09-06")


if __name__ == "__main__":
    unittest.main()
