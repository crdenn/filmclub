"""Focused regression tests for the Discord weekly reminder digest."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SESSION_SECRET", "discord-reminder-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import config, db, discord, service  # noqa: E402


class _Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _Client:
    last_post = None
    response = _Response(200)

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        type(self).last_post = (url, kwargs)
        return type(self).response


class _RaisingClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        raise ConnectionError("network unreachable")


class DiscordReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-discord-")
        self.old_db_path = config.DB_PATH
        self.old_webhook_url = config.DISCORD_WEBHOOK_URL
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.DISCORD_WEBHOOK_URL = "https://discord.test/api/webhooks/123/abc"
        db.init_db()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        config.DISCORD_WEBHOOK_URL = self.old_webhook_url
        self.tmp.cleanup()

    def add_member(self, plex_id="uuid-1", username="Alice", discord_user_id=None):
        cur = self.conn.execute(
            """INSERT INTO members (plex_id, username, color, discord_user_id)
               VALUES (?, ?, '#123456', ?)""",
            (plex_id, username, discord_user_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_movie(self, title="Test Film", status="suggested", watched_at=None, year=None):
        cur = self.conn.execute(
            "INSERT INTO movies (title, status, watched_at, year) VALUES (?, ?, ?, ?)",
            (title, status, watched_at, year),
        )
        self.conn.commit()
        return cur.lastrowid

    # --- is_configured -------------------------------------------------

    def test_is_configured_reflects_webhook_url(self):
        self.assertTrue(discord.is_configured())
        config.DISCORD_WEBHOOK_URL = ""
        self.assertFalse(discord.is_configured())

    # --- todo_details ----------------------------------------------------

    def test_todo_details_caps_and_reports_overflow(self):
        member_id = self.add_member()
        for i in range(7):
            self.add_movie(title=f"Backlog {i}")

        detail = service.todo_details(self.conn, member_id, cap=5)

        self.assertEqual(detail["backlog"]["count"], 7)
        self.assertEqual(len(detail["backlog"]["titles"]), 5)
        self.assertEqual(detail["backlog"]["overflow"], 2)
        self.assertEqual(detail["watched"], {"count": 0, "titles": [], "overflow": 0})

    def test_todo_details_marked_films_drop_off(self):
        member_id = self.add_member()
        movie_id = self.add_movie(title="Marked")
        service.set_prior_view(self.conn, movie_id, member_id, seen=True)

        detail = service.todo_details(self.conn, member_id)

        self.assertEqual(detail["backlog"], {"count": 0, "titles": [], "overflow": 0})

    # --- build_digest ------------------------------------------------------

    def test_build_digest_includes_scheduled_film(self):
        self.add_member()
        self.add_movie(title="Scheduled Film", status="scheduled",
                       watched_at="2026-08-04", year=1999)

        digest = discord.build_digest(self.conn)

        self.assertEqual(len(digest["scheduled"]), 1)
        self.assertEqual(digest["scheduled"][0]["title"], "Scheduled Film")

    def test_build_digest_no_scheduled_film(self):
        self.add_member()

        digest = discord.build_digest(self.conn)

        self.assertEqual(digest["scheduled"], [])

    def test_build_digest_gaps_only_lists_members_with_outstanding_items(self):
        caught_up = self.add_member(plex_id="uuid-caughtup", username="CaughtUp")
        behind = self.add_member(plex_id="uuid-behind", username="Behind",
                                 discord_user_id="999888777")
        movie_id = self.add_movie(title="Needs a verdict")
        service.set_prior_view(self.conn, movie_id, caught_up, seen=True)
        # `behind` never marks it -> shows up as a gap.

        digest = discord.build_digest(self.conn)

        gap_member_ids = {g["member"]["id"] for g in digest["gaps"]}
        self.assertIn(behind, gap_member_ids)
        self.assertNotIn(caught_up, gap_member_ids)
        behind_gap = next(g for g in digest["gaps"] if g["member"]["id"] == behind)
        self.assertEqual(behind_gap["discord_user_id"], "999888777")

    # --- _format_message -----------------------------------------------------

    def test_format_message_mentions_and_plain_fallback(self):
        digest = {
            "scheduled": [],
            "gaps": [
                {"member": {"id": 1, "username": "alice"}, "discord_user_id": "111222333",
                 "backlog": {"count": 1, "titles": ["Movie A"], "overflow": 0},
                 "watched": {"count": 0, "titles": [], "overflow": 0}},
                {"member": {"id": 2, "username": "bob"}, "discord_user_id": None,
                 "backlog": {"count": 0, "titles": [], "overflow": 0},
                 "watched": {"count": 1, "titles": ["Movie B"], "overflow": 0}},
            ],
        }

        payload = discord._format_message(digest)

        self.assertIn("<@111222333>", payload["content"])
        self.assertIn("bob", payload["content"])
        self.assertNotIn("<@None>", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": ["111222333"]})

    def test_format_message_no_scheduled_film_prompts_a_pick(self):
        digest = {"scheduled": [], "gaps": []}

        payload = discord._format_message(digest)

        self.assertIn("No film picked yet", payload["content"])
        self.assertIn("Everyone's caught up.", payload["content"])
        self.assertEqual(payload["allowed_mentions"]["users"], [])

    # --- weekly send tracking --------------------------------------------------

    def test_mark_sent_then_already_sent_this_week(self):
        self.assertFalse(discord._already_sent_this_week(self.conn))
        discord._mark_sent(self.conn)
        self.assertTrue(discord._already_sent_this_week(self.conn))

    def test_stale_week_reads_back_as_not_sent(self):
        discord._mark_sent(self.conn)
        self.conn.execute(
            "UPDATE app_settings SET value = '2020-W01' WHERE key = ?",
            (discord._LAST_SENT_KEY,),
        )
        self.conn.commit()

        self.assertFalse(discord._already_sent_this_week(self.conn))

    # --- _post ---------------------------------------------------------------

    def test_post_success(self):
        _Client.response = _Response(204)
        with patch("app.discord.httpx.AsyncClient", _Client):
            result = asyncio.run(discord._post({"content": "hi"}))
        self.assertEqual(result["status"], "sent")

    def test_post_http_failure(self):
        _Client.response = _Response(500, "server error")
        with patch("app.discord.httpx.AsyncClient", _Client):
            result = asyncio.run(discord._post({"content": "hi"}))
        self.assertEqual(result["status"], "failed")

    def test_post_network_failure_never_raises(self):
        with patch("app.discord.httpx.AsyncClient", _RaisingClient):
            result = asyncio.run(discord._post({"content": "hi"}))
        self.assertEqual(result["status"], "failed")

    def test_post_disabled_when_unconfigured(self):
        config.DISCORD_WEBHOOK_URL = ""
        result = asyncio.run(discord._post({"content": "hi"}))
        self.assertEqual(result["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
