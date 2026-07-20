"""Focused coverage for the read-only TMDB movie preview endpoint."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "tmdb-preview-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-tmdb-preview-"))

from app import main  # noqa: E402


class TmdbPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_returns_full_tmdb_details_without_adding(self):
        preview = {
            "tmdb_id": 99,
            "title": "Preview Film",
            "year": 2026,
            "runtime": 112,
            "director": "Example Director",
            "genres": ["Drama"],
            "overview": "A film being previewed.",
        }
        details = AsyncMock(return_value=preview)

        with patch.object(main.tmdb, "details", new=details):
            result = await main.api_tmdb_movie_preview(99, member={"id": 1})

        self.assertEqual(result, preview)
        details.assert_awaited_once_with(99)

    async def test_preview_maps_tmdb_failures_to_a_safe_http_error(self):
        details = AsyncMock(side_effect=RuntimeError("request URL contained a secret"))

        with patch.object(main.tmdb, "details", new=details):
            with self.assertRaises(HTTPException) as raised:
                await main.api_tmdb_movie_preview(99, member={"id": 1})

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            raised.exception.detail,
            "Could not fetch film details from TMDB",
        )


if __name__ == "__main__":
    unittest.main()
