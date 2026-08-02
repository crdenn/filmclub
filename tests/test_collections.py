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


if __name__ == "__main__":
    unittest.main()
