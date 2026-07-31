"""Focused regression tests for the Discord weekly reminder digest."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "discord-reminder-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import config, db, discord, service, settings  # noqa: E402


class _FixedDatetime:
    """Stand-in for the `datetime` class used by app.discord — fixes `now()`
    to a chosen instant while still delegating `strptime` to the real
    implementation (used by `_fmt_date`)."""
    _now = datetime(2026, 8, 10, 8, 0)  # a Monday, 08:00
    strptime = datetime.strptime

    @classmethod
    def now(cls):
        return cls._now


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
        self.old_weekday = config.DISCORD_REMINDER_WEEKDAY
        self.old_hour = config.DISCORD_REMINDER_HOUR
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.DISCORD_WEBHOOK_URL = "https://discord.test/api/webhooks/123/abc"
        db.init_db()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        config.DISCORD_WEBHOOK_URL = self.old_webhook_url
        config.DISCORD_REMINDER_WEEKDAY = self.old_weekday
        config.DISCORD_REMINDER_HOUR = self.old_hour
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

    def test_format_message_includes_backlog_and_watched_links(self):
        payload = discord._format_message({"scheduled": [], "gaps": []})

        self.assertIn(f"[Open Backlog]({config.APP_URL}/#/backlog)", payload["content"])
        self.assertIn(f"[Open Watched]({config.APP_URL}/#/watched)", payload["content"])

    # --- _format_date_changed_message ----------------------------------------

    def test_format_date_changed_message_content(self):
        payload = discord._format_date_changed_message("The Thing", 1982, "2026-08-13")

        self.assertEqual(
            payload["content"],
            "\U0001F4C5 **Discussion date changed** — we're meeting to discuss "
            "*The Thing* (1982) on **Thursday, Aug 13**.",
        )
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": []})

    def test_format_date_changed_message_no_year(self):
        payload = discord._format_date_changed_message("Untitled", None, "2026-08-13")
        self.assertIn("*Untitled* on", payload["content"])

    # --- notify_date_changed --------------------------------------------------

    def test_notify_date_changed_posts_formatted_message(self):
        movie_id = self.add_movie(title="The Thing", status="scheduled",
                                  watched_at="2026-08-13", year=1982)
        _Client.response = _Response(204)
        with patch("app.discord.httpx.AsyncClient", _Client):
            result = asyncio.run(discord.notify_date_changed(movie_id))

        self.assertEqual(result["status"], "sent")
        url, kwargs = _Client.last_post
        self.assertIn("The Thing", kwargs["json"]["content"])
        self.assertIn("Thursday, Aug 13", kwargs["json"]["content"])

    def test_notify_date_changed_missing_movie(self):
        result = asyncio.run(discord.notify_date_changed(999999))
        self.assertEqual(result["status"], "failed")

    def test_notify_date_changed_disabled_when_unconfigured(self):
        config.DISCORD_WEBHOOK_URL = ""
        result = asyncio.run(discord.notify_date_changed(1))
        self.assertEqual(result["status"], "disabled")

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

    # --- _maybe_send: configurable weekday/hour gating ------------------------

    def test_maybe_send_fires_on_configured_weekday_and_hour(self):
        # _FixedDatetime._now is a Monday at 08:00.
        config.DISCORD_REMINDER_WEEKDAY = 0  # Monday
        config.DISCORD_REMINDER_HOUR = 8
        with patch("app.discord.datetime", _FixedDatetime), \
             patch("app.discord._post", new=AsyncMock(return_value={"status": "sent"})) as post:
            asyncio.run(discord._maybe_send())

        post.assert_called_once()
        self.assertTrue(discord._already_sent_this_week(self.conn))

    def test_maybe_send_skips_wrong_weekday(self):
        config.DISCORD_REMINDER_WEEKDAY = 2  # Wednesday; fixed "now" is a Monday
        config.DISCORD_REMINDER_HOUR = 8
        with patch("app.discord.datetime", _FixedDatetime), \
             patch("app.discord._post", new=AsyncMock(return_value={"status": "sent"})) as post:
            asyncio.run(discord._maybe_send())
        post.assert_not_called()

    def test_maybe_send_skips_before_configured_hour(self):
        config.DISCORD_REMINDER_WEEKDAY = 0  # Monday matches the fixed "now"
        config.DISCORD_REMINDER_HOUR = 9  # fixed "now" is 08:00 — too early
        with patch("app.discord.datetime", _FixedDatetime), \
             patch("app.discord._post", new=AsyncMock(return_value={"status": "sent"})) as post:
            asyncio.run(discord._maybe_send())
        post.assert_not_called()

    # --- send_digest_now: the manual "test digest" path -----------------------

    def test_send_digest_now_sends_without_marking_the_week_sent(self):
        with patch("app.discord.httpx.AsyncClient", _Client):
            _Client.response = _Response(204)
            result = asyncio.run(discord.send_digest_now())

        self.assertEqual(result["status"], "sent")
        self.assertFalse(discord._already_sent_this_week(self.conn))

    def test_send_digest_now_disabled_when_unconfigured(self):
        config.DISCORD_WEBHOOK_URL = ""
        result = asyncio.run(discord.send_digest_now())
        self.assertEqual(result["status"], "disabled")

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
