"""Tests for the JSON authoring tool behind `python -m app.collection_tool`.

The load-bearing rule is that a TMDB failure must never cost an author their
writing: an incomplete fetch cancels a prune rather than deleting films the
payload still lists.
"""
import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "collection-tool-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-tool-bootstrap-"))

from app import collection_tool  # noqa: E402
from app import collections as coll  # noqa: E402
from app import config, db  # noqa: E402


def _details(tmdb_id: int, title: str, year: int = 1999) -> dict:
    return {
        "tmdb_id": tmdb_id, "title": title, "year": year, "runtime": 100,
        "director": "A Director", "imdb_id": f"tt{tmdb_id:07d}",
        "backdrop_url": "https://image.tmdb.org/t/p/w1280/x.jpg",
    }


CATALOGUE = {
    1: _details(1, "First Film", 1970),
    2: _details(2, "Second Film", 1980),
    3: _details(3, "Third Film", 1990),
}


class CollectionToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-tool-")
        self._old_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()

        async def fake_details(tmdb_id):
            if tmdb_id not in CATALOGUE:
                raise RuntimeError("no such film")
            return CATALOGUE[tmdb_id]

        self.tmdb = patch.object(collection_tool.tmdb, "details",
                                 new=AsyncMock(side_effect=fake_details))
        self.tmdb.start()

    def tearDown(self):
        self.tmdb.stop()
        self.conn.close()
        config.DB_PATH = self._old_db
        self.tmp.cleanup()

    def _apply(self, spec, *, dry_run=False):
        return asyncio.run(collection_tool._apply(spec, dry_run=dry_run))

    def _entries(self, slug):
        collection = coll.get_by_slug(self.conn, slug)
        return coll.entries_for(self.conn, collection["id"])

    def test_apply_creates_a_published_collection_with_blurbs(self):
        self._apply({
            "slug": "westerns", "title": "Westerns", "intro": "An intro.",
            "published": True, "origin": "generated",
            "films": [{"tmdb": 1, "blurb": "About the first."},
                      {"tmdb": 2, "blurb": "About the second."}],
        })
        collection = coll.get_by_slug(self.conn, "westerns")
        self.assertEqual(collection["title"], "Westerns")
        self.assertEqual(collection["intro"], "An intro.")
        self.assertTrue(collection["published"])
        self.assertEqual(collection["origin"], "generated")

        entries = self._entries("westerns")
        self.assertEqual([e["title"] for e in entries], ["First Film", "Second Film"])
        self.assertEqual(entries[0]["blurb"], "About the first.")

    def test_apply_is_idempotent(self):
        spec = {"slug": "westerns", "title": "Westerns", "published": True,
                "films": [{"tmdb": 1, "blurb": "A blurb."}]}
        self._apply(spec)
        before = self._entries("westerns")[0]
        self._apply(spec)
        after = self._entries("westerns")[0]
        self.assertEqual(len(self._entries("westerns")), 1)
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(after["blurb"], "A blurb.")

    def test_apply_rewrites_a_blurb_on_an_existing_entry(self):
        self._apply({"slug": "westerns", "films": [{"tmdb": 1, "blurb": "First draft."}]})
        self._apply({"slug": "westerns", "films": [{"tmdb": 1, "blurb": "Second draft."}]})
        self.assertEqual(self._entries("westerns")[0]["blurb"], "Second draft.")

    def test_a_partial_payload_does_not_blank_an_omitted_intro(self):
        self._apply({"slug": "westerns", "title": "Westerns", "intro": "Keep me.",
                     "films": [{"tmdb": 1}]})
        self._apply({"slug": "westerns", "films": [{"tmdb": 1, "blurb": "New."}]})
        self.assertEqual(coll.get_by_slug(self.conn, "westerns")["intro"], "Keep me.")

    def test_films_absent_from_the_payload_survive_without_prune(self):
        self._apply({"slug": "westerns",
                     "films": [{"tmdb": 1}, {"tmdb": 2}]})
        self._apply({"slug": "westerns", "films": [{"tmdb": 1}]})
        self.assertEqual(len(self._entries("westerns")), 2)

    def test_prune_removes_films_the_payload_dropped(self):
        self._apply({"slug": "westerns", "films": [{"tmdb": 1}, {"tmdb": 2}]})
        self._apply({"slug": "westerns", "prune": True, "films": [{"tmdb": 1}]})
        self.assertEqual([e["title"] for e in self._entries("westerns")], ["First Film"])

    def test_a_tmdb_failure_cancels_the_prune_rather_than_deleting_writing(self):
        """The rule that matters: a transient TMDB outage must not be read as
        'the author removed these films'."""
        self._apply({"slug": "westerns",
                     "films": [{"tmdb": 1, "blurb": "Keep."}, {"tmdb": 2, "blurb": "Keep too."}]})
        # tmdb id 99 is not in the catalogue, so its fetch raises.
        self._apply({"slug": "westerns", "prune": True,
                     "films": [{"tmdb": 99}]})
        entries = self._entries("westerns")
        self.assertEqual([e["title"] for e in entries], ["First Film", "Second Film"])
        self.assertEqual(entries[0]["blurb"], "Keep.")

    def test_a_partial_edit_does_not_reorder_the_films_it_omits(self):
        """The common edit is "here are the two blurbs I rewrote". Treating
        that payload's length as the new running order would shuffle the rest."""
        self._apply({"slug": "westerns", "reorder": True,
                     "films": [{"tmdb": 1}, {"tmdb": 2}, {"tmdb": 3}]})
        self.assertEqual([e["title"] for e in self._entries("westerns")],
                         ["First Film", "Second Film", "Third Film"])

        # Rewrite only the third film's blurb.
        self._apply({"slug": "westerns", "films": [{"tmdb": 3, "blurb": "Rewritten."}]})
        entries = self._entries("westerns")
        self.assertEqual([e["title"] for e in entries],
                         ["First Film", "Second Film", "Third Film"])
        self.assertEqual(entries[2]["blurb"], "Rewritten.")

    def test_reorder_applies_the_payload_order(self):
        self._apply({"slug": "westerns", "reorder": True,
                     "films": [{"tmdb": 1}, {"tmdb": 2}]})
        self._apply({"slug": "westerns", "reorder": True,
                     "films": [{"tmdb": 2}, {"tmdb": 1}]})
        self.assertEqual([e["title"] for e in self._entries("westerns")],
                         ["Second Film", "First Film"])

    def test_dry_run_reports_without_writing(self):
        self._apply({"slug": "westerns", "title": "Westerns",
                     "films": [{"tmdb": 1}]}, dry_run=True)
        self.assertIsNone(coll.get_by_slug(self.conn, "westerns"))

    def test_dry_run_on_an_existing_collection_changes_nothing(self):
        self._apply({"slug": "westerns", "intro": "Original.", "films": [{"tmdb": 1}]})
        self._apply({"slug": "westerns", "intro": "Rewritten.", "prune": True,
                     "films": [{"tmdb": 2}]}, dry_run=True)
        collection = coll.get_by_slug(self.conn, "westerns")
        self.assertEqual(collection["intro"], "Original.")
        self.assertEqual([e["title"] for e in self._entries("westerns")], ["First Film"])

    def test_dump_round_trips_into_apply(self):
        self._apply({"slug": "westerns", "title": "Westerns", "intro": "An intro.",
                     "published": True,
                     "films": [{"tmdb": 1, "blurb": "One."}, {"tmdb": 2, "blurb": "Two."}]})
        out = io.StringIO()
        with patch("sys.stdout", out):
            collection_tool._dump("westerns")
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["slug"], "westerns")
        self.assertEqual([f["tmdb"] for f in payload["films"]], [1, 2])
        self.assertEqual(payload["films"][0]["blurb"], "One.")

        # Re-applying what dump produced is a no-op, not a mutation.
        self._apply(payload)
        self.assertEqual([e["blurb"] for e in self._entries("westerns")], ["One.", "Two."])

    def test_payload_validation_rejects_a_missing_slug_or_films(self):
        with self.assertRaises(SystemExit):
            collection_tool._load(io.StringIO('{"films": []}'))
        with self.assertRaises(SystemExit):
            collection_tool._load(io.StringIO('{"slug": "x"}'))
        with self.assertRaises(SystemExit):
            collection_tool._load(io.StringIO('{"slug": "x", "films": [{"blurb": "no id"}]}'))

    def test_load_accepts_a_single_payload_or_an_array(self):
        one = collection_tool._load(io.StringIO('{"slug": "a", "films": []}'))
        many = collection_tool._load(
            io.StringIO('[{"slug": "a", "films": []}, {"slug": "b", "films": []}]'))
        self.assertEqual([s["slug"] for s in one], ["a"])
        self.assertEqual([s["slug"] for s in many], ["a", "b"])

    def test_a_batch_applies_every_collection_in_it(self):
        specs = collection_tool._load(io.StringIO(json.dumps([
            {"slug": "one", "title": "One", "films": [{"tmdb": 1, "blurb": "A."}]},
            {"slug": "two", "title": "Two", "films": [{"tmdb": 2, "blurb": "B."}]},
        ])))
        for spec in specs:
            self._apply(spec)
        self.assertEqual(self._entries("one")[0]["blurb"], "A.")
        self.assertEqual(self._entries("two")[0]["blurb"], "B.")


if __name__ == "__main__":
    unittest.main()
