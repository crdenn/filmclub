"""Focused tests for persisted and externally enriched movie metadata."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SESSION_SECRET", "metadata-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-metadata-bootstrap-"))

from app import config, db, main, plex, service, tmdb  # noqa: E402


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

    def test_us_content_rating_prefers_wide_theatrical_release(self):
        movie = {"release_dates": {"results": [
            {"iso_3166_1": "GB", "release_dates": [
                {"certification": "15", "type": 3, "release_date": "2024-01-01"},
            ]},
            {"iso_3166_1": "US", "release_dates": [
                {"certification": "R", "type": 4, "release_date": "2024-03-01"},
                {"certification": "PG-13", "type": 3, "release_date": "2024-02-01"},
                {"certification": "", "type": 2, "release_date": "2024-01-15"},
            ]},
        ]}}
        self.assertEqual(tmdb.us_content_rating(movie), "PG-13")
        self.assertIsNone(tmdb.us_content_rating({"release_dates": {"results": []}}))

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

    def test_new_suggestion_stores_language_and_content_rating(self):
        member_id = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES ('dev:a', 'A', '#123456')"
        ).lastrowid
        self.conn.commit()
        movie_id = service.add_suggestion(self.conn, {
            "tmdb_id": 99,
            "title": "Test Film",
            "language": "French",
            "content_rating": "PG-13",
            "genres": [],
        }, member_id)
        row = db.query_one(
            self.conn,
            "SELECT language, content_rating FROM movies WHERE id = ?",
            (movie_id,),
        )
        self.assertEqual(row["language"], "French")
        self.assertEqual(row["content_rating"], "PG-13")

    def test_new_suggestion_stores_and_normalises_pitch(self):
        member_id = self.conn.execute(
            "INSERT INTO members (plex_id, username, color) VALUES ('dev:b', 'B', '#123456')"
        ).lastrowid
        self.conn.commit()
        meta = {"tmdb_id": 100, "title": "Pitched", "genres": []}
        # A real pitch is stored verbatim (after trimming); a blank one is NULL.
        with_pitch = service.add_suggestion(self.conn, meta, member_id, "  Watch it. ")
        meta_blank = {"tmdb_id": 101, "title": "Unpitched", "genres": []}
        without_pitch = service.add_suggestion(self.conn, meta_blank, member_id, "   ")
        self.assertEqual(
            db.query_one(self.conn, "SELECT pitch FROM movies WHERE id = ?", (with_pitch,))["pitch"],
            "Watch it.")
        self.assertIsNone(
            db.query_one(self.conn, "SELECT pitch FROM movies WHERE id = ?", (without_pitch,))["pitch"])


class MetadataBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-metadata-backfill-")
        self.old_db_path = config.DB_PATH
        self.old_tmdb_key = config.TMDB_API_KEY
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.TMDB_API_KEY = "test-key"
        db.init_db()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO movies (tmdb_id, title, language) "
                "VALUES (99, 'Older Film', 'French')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        config.TMDB_API_KEY = self.old_tmdb_key
        self.tmp.cleanup()

    async def test_existing_movie_content_rating_is_backfilled_once(self):
        details = AsyncMock(return_value={"language": "French", "content_rating": "R"})
        with patch.object(main.tmdb, "details", new=details):
            await main._backfill_movie_metadata()
            await main._backfill_movie_metadata()

        conn = db.connect()
        try:
            row = db.query_one(
                conn, "SELECT content_rating FROM movies WHERE tmdb_id = 99"
            )
        finally:
            conn.close()
        self.assertEqual(row["content_rating"], "R")
        details.assert_awaited_once_with(99)

    async def test_tmdb_details_requests_and_returns_release_certification(self):
        response = MagicMock()
        response.json.return_value = {
            "id": 99,
            "title": "Test Film",
            "release_date": "2024-02-01",
            "credits": {"crew": []},
            "release_dates": {"results": [{
                "iso_3166_1": "US",
                "release_dates": [{
                    "certification": "PG", "type": 3,
                    "release_date": "2024-02-01",
                }],
            }]},
        }
        client = AsyncMock()
        client.get.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)

        with patch.object(tmdb.httpx, "AsyncClient", return_value=context):
            result = await tmdb.details(99)

        self.assertEqual(result["content_rating"], "PG")
        _, kwargs = client.get.await_args
        self.assertEqual(
            kwargs["params"]["append_to_response"], "credits,release_dates"
        )


if __name__ == "__main__":
    unittest.main()
