"""Focused tests for language and Rotten Tomatoes metadata enrichment."""
import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "metadata-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-metadata-bootstrap-"))

from app import config, db, plex, service, tmdb  # noqa: E402


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-metadata-")
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_original_language_uses_tmdb_english_name(self):
        movie = {
            "original_language": "ko",
            "spoken_languages": [
                {"iso_639_1": "ko", "english_name": "Korean", "name": "한국어"},
            ],
        }
        self.assertEqual(tmdb.language_name(movie), "Korean")
        self.assertEqual(tmdb.language_name({"original_language": "xx"}), "XX")

    def test_rotten_tomatoes_scores_are_normalized_from_plex(self):
        result = plex.rotten_tomatoes_from_metadata({
            "rating": 9.7,
            "ratingImage": "rottentomatoes://image.rating.ripe",
            "audienceRating": 8.6,
            "audienceRatingImage": "rottentomatoes://image.rating.upright",
        })
        self.assertEqual(result, {
            "critic": 97,
            "critic_state": "ripe",
            "audience": 86,
            "audience_state": "upright",
        })
        self.assertIsNone(plex.rotten_tomatoes_from_metadata({
            "rating": 8.4, "ratingImage": "imdb://image.rating"
        }))

    def test_library_match_carries_rotten_tomatoes_without_new_secrets(self):
        before = copy.deepcopy(plex._library)
        try:
            plex._library.update(
                ok=True,
                tmdb={99},
                imdb=set(),
                rk_tmdb={99: "7"},
                rk_imdb={},
                rt_tmdb={99: {"critic": 94, "audience": 79}},
                rt_imdb={},
                machine_id="server-uuid",
            )
            match = plex.library_match(99, None)
            self.assertEqual(match["rotten_tomatoes"], {"critic": 94, "audience": 79})
            self.assertNotIn("token", repr(match).lower())
        finally:
            plex._library.clear()
            plex._library.update(before)

    def test_new_suggestion_stores_language(self):
        member_id = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES ('dev:a', 'A', '#123456')"
        ).lastrowid
        self.conn.commit()
        movie_id = service.add_suggestion(self.conn, {
            "tmdb_id": 99,
            "title": "Test Film",
            "language": "French",
            "genres": [],
        }, member_id)
        row = db.query_one(self.conn, "SELECT language FROM movies WHERE id = ?", (movie_id,))
        self.assertEqual(row["language"], "French")


if __name__ == "__main__":
    unittest.main()
