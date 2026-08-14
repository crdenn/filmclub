"""Tests for the Spin page's library sampling and poster proxy.

The proxy is the security-sensitive part: it talks to Plex with the *server*
token, so it must only ever resolve rating keys the library refresh actually
recorded. Anything else would make it a request forwarder pointed at the LAN.
"""
import os
import tempfile
import unittest

os.environ.setdefault("SESSION_SECRET", "spin-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-spin-bootstrap-"))

from app import plex  # noqa: E402


def _movie(rk, title, thumb="/library/metadata/%s/thumb/1"):
    return {
        "rating_key": str(rk),
        "title": title,
        "year": 1999,
        "thumb": thumb % rk if "%s" in thumb else thumb,
        "summary": "A summary.",
        "duration": 7_200_000,
        "content_rating": "15",
        "genres": ["Drama"],
    }


class SpinLibraryTests(unittest.TestCase):
    def setUp(self):
        self._old = dict(plex._library)
        movies = {str(i): _movie(i, f"Film {i}") for i in range(1, 31)}
        plex._library.update(movies=movies, machine_id="machine-1", ok=True)

    def tearDown(self):
        plex._library.clear()
        plex._library.update(self._old)

    # --- sampling ----------------------------------------------------------

    def test_random_movies_returns_requested_count_without_repeats(self):
        picks = plex.random_movies(10)
        self.assertEqual(len(picks), 10)
        keys = [p["rating_key"] for p in picks]
        self.assertEqual(len(set(keys)), 10, "a single draw repeated a film")

    def test_random_movies_caps_at_library_size(self):
        plex._library["movies"] = {"1": _movie(1, "Only Film")}
        self.assertEqual(len(plex.random_movies(10)), 1)

    def test_random_movies_actually_varies(self):
        # Twenty draws of ten from thirty films landing on one identical set
        # would mean the sampling isn't random at all.
        seen = {tuple(sorted(p["rating_key"] for p in plex.random_movies(10)))
                for _ in range(20)}
        self.assertGreater(len(seen), 1)

    def test_random_movies_carries_a_deep_link(self):
        pick = plex.random_movies(1)[0]
        self.assertIn("machine-1", pick["deep_link"])
        self.assertIn(pick["rating_key"], pick["deep_link"])

    def test_random_movies_dedupes_a_film_stored_twice(self):
        # Two editions of one film: different rating keys, same title+year. A
        # draw must never carry both, or the reel shows identical posters.
        plex._library["movies"] = {
            "1": _movie(1, "Heat"),
            "2": dict(_movie(2, "Heat")),      # second copy, different key
            "3": _movie(3, "Other Film"),
        }
        for _ in range(20):
            titles = [p["title"] for p in plex.random_movies(3)]
            self.assertEqual(len(titles), len(set(titles)), f"duplicate in {titles}")

    def test_random_movies_empty_when_library_never_loaded(self):
        plex._library["ok"] = False
        self.assertEqual(plex.random_movies(5), [])

    def test_sampling_does_not_mutate_the_cache(self):
        before = dict(plex._library["movies"]["1"])
        plex.random_movies(10)
        self.assertEqual(plex._library["movies"]["1"], before)
        self.assertNotIn("deep_link", plex._library["movies"]["1"])

    # --- proxy guard -------------------------------------------------------

    def test_thumb_path_resolves_a_known_key(self):
        self.assertEqual(plex.thumb_path("7"), "/library/metadata/7/thumb/1")

    def test_thumb_path_rejects_unknown_key(self):
        self.assertIsNone(plex.thumb_path("99999"))

    def test_thumb_path_rejects_traversal_and_absolute_urls(self):
        for bad in ("../../identity", "/../identity", "http://evil.test/x",
                    "//evil.test/x", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(plex.thumb_path(bad))

    def test_thumb_path_rejects_entry_whose_thumb_is_not_a_path(self):
        # A stored value that isn't a server-relative path must not be fetched,
        # even though its key is legitimate.
        plex._library["movies"]["500"] = _movie(500, "Odd", thumb="http://evil.test/x.jpg")
        self.assertIsNone(plex.thumb_path("500"))

    def test_thumb_path_rejects_entry_with_no_thumb(self):
        plex._library["movies"]["501"] = _movie(501, "No Art", thumb="")
        self.assertIsNone(plex.thumb_path("501"))


if __name__ == "__main__":
    unittest.main()
