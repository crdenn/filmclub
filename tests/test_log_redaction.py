"""Tests that secrets are scrubbed from log output."""
import logging
import unittest

from app.logsafe import RedactingFilter, redact


class RedactTests(unittest.TestCase):
    def test_tmdb_api_key_in_url_is_redacted(self):
        s = ("Client error '401 Unauthorized' for url "
             "'https://api.themoviedb.org/3/movie/13?api_key=SECRETKEY123&append_to_response=credits'")
        out = redact(s)
        self.assertNotIn("SECRETKEY123", out)
        self.assertIn("api_key=REDACTED", out)
        # non-secret query params are preserved
        self.assertIn("append_to_response=credits", out)

    def test_plex_and_generic_token_query_params(self):
        self.assertNotIn("abc123", redact("https://plex/library?X-Plex-Token=abc123"))
        self.assertNotIn("zzz", redact("call?token=zzz&x=1"))
        self.assertIn("x=1", redact("call?token=zzz&x=1"))

    def test_webhook_path_secret_is_redacted(self):
        out = redact("GET /api/plex/webhook/supersecretvalue 200 OK")
        self.assertNotIn("supersecretvalue", out)
        self.assertIn("/api/plex/webhook/REDACTED", out)

    def test_no_false_positive_on_ordinary_text(self):
        self.assertEqual(redact("Language backfill failed for movie 13"),
                         "Language backfill failed for movie 13")

    def test_filter_rewrites_log_record_in_place(self):
        rec = logging.LogRecord(
            "filmclub", logging.WARNING, __file__, 1,
            "failed: %s", ("url?api_key=LEAKME",), None,
        )
        RedactingFilter().filter(rec)
        self.assertNotIn("LEAKME", rec.getMessage())
        self.assertIn("api_key=REDACTED", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
