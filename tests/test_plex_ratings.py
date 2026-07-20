"""Focused regression tests for bidirectional Plex rating synchronization."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "plex-rating-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-test-bootstrap-"))

from app import auth, config, db, plex_ratings, service  # noqa: E402
from app.token_crypto import decrypt_plex_token, encrypt_plex_token  # noqa: E402


class _Response:
    def raise_for_status(self):
        return None


class _Client:
    last_put = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def put(self, url, **kwargs):
        type(self).last_put = (url, kwargs)
        return _Response()


class PlexRatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-plex-ratings-")
        self.old_db_path = config.DB_PATH
        self.old_plex_url = config.PLEX_URL
        self.old_machine_id = config.PLEX_MACHINE_ID
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        config.PLEX_URL = "http://plex.test:32400"
        config.PLEX_MACHINE_ID = "server-uuid"
        db.init_db()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        config.PLEX_URL = self.old_plex_url
        config.PLEX_MACHINE_ID = self.old_machine_id
        self.tmp.cleanup()

    def add_member(self, plex_id="uuid-1", account_id="42", username="Alice",
                   token="member-token", sync_enabled=True):
        cur = self.conn.execute(
            """INSERT INTO members
               (plex_id, plex_account_id, plex_token_encrypted,
                plex_rating_sync_enabled, username, color)
               VALUES (?, ?, ?, ?, ?, '#123456')""",
            (plex_id, account_id, encrypt_plex_token(token),
             1 if sync_enabled else 0, username),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_movie(self, status="watched", tmdb_id=99):
        cur = self.conn.execute(
            """INSERT INTO movies (tmdb_id, title, status, seen_before_snapshot)
               VALUES (?, 'Test Film', ?, '{}')""",
            (tmdb_id, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_token_is_encrypted_and_round_trips(self):
        encrypted = encrypt_plex_token("super-secret-token")
        self.assertIsNotNone(encrypted)
        self.assertNotIn("super-secret-token", encrypted)
        self.assertEqual(decrypt_plex_token(encrypted), "super-secret-token")

    def test_profile_connection_status_exposes_only_a_boolean(self):
        member_id = self.add_member(token="member-token")
        row = db.query_one(self.conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        public = db.member_public(row)
        result = auth.with_connection_status(public)

        self.assertTrue(result["plex_rating_sync_connected"])
        self.assertTrue(result["plex_rating_sync_enabled"])
        self.assertNotIn("plex_token_encrypted", result)
        self.assertNotIn("member-token", repr(result))

    def test_member_can_pause_and_resume_rating_sync(self):
        member_id = self.add_member()

        service.set_plex_rating_sync_enabled(self.conn, member_id, False)
        row = db.query_one(self.conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        paused = auth.with_connection_status(db.member_public(row))
        service.set_plex_rating_sync_enabled(self.conn, member_id, True)
        row = db.query_one(self.conn, "SELECT * FROM members WHERE id = ?", (member_id,))
        resumed = auth.with_connection_status(db.member_public(row))

        self.assertTrue(paused["plex_rating_sync_connected"])
        self.assertFalse(paused["plex_rating_sync_enabled"])
        self.assertTrue(resumed["plex_rating_sync_enabled"])

    def test_connected_member_session_remains_authenticated(self):
        member_id = self.add_member(plex_id="uuid-connected", token="member-token")
        cookie = auth.create_session(member_id)

        member = auth.current_member(cookie)

        self.assertEqual(member["id"], member_id)

    def test_legacy_identity_only_session_requires_normal_plex_login(self):
        member_id = self.add_member(plex_id="uuid-legacy", token=None)
        cookie = auth.create_session(member_id)

        with self.assertRaises(HTTPException) as raised:
            auth.current_member(cookie)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Plex reauthentication required")

    def test_normal_login_connects_legacy_member_without_replacing_profile(self):
        member_id = self.add_member(plex_id="uuid-returning", token=None)

        member = auth.upsert_member(
            plex_id="uuid-returning",
            username="Alice Updated",
            email="alice@example.test",
            thumb=None,
            plex_account_id="42",
            plex_token="fresh-user-token",
        )
        row = db.query_one(self.conn, "SELECT * FROM members WHERE id = ?", (member_id,))

        self.assertEqual(member["id"], member_id)
        self.assertEqual(row["username"], "Alice Updated")
        self.assertEqual(decrypt_plex_token(row["plex_token_encrypted"]), "fresh-user-token")

    def test_watched_items_include_requesting_members_rating(self):
        alice_id = self.add_member()
        bob_id = self.add_member(plex_id="uuid-2", account_id="43", username="Bob")
        movie_id = self.add_movie()
        service.upsert_rating(self.conn, movie_id, alice_id, 4.5, False, None)

        alice_item = service.watched(self.conn, alice_id)[0]
        bob_item = service.watched(self.conn, bob_id)[0]

        self.assertTrue(alice_item["my_rated"])
        self.assertEqual(alice_item["my_score"], 4.5)
        self.assertFalse(bob_item["my_rated"])
        self.assertIsNone(bob_item["my_score"])

    def test_outbound_rating_uses_member_token_and_doubles_score(self):
        member_id = self.add_member()
        movie_id = self.add_movie()
        movie = dict(db.query_one(self.conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)))
        with patch("app.plex_ratings.plex.library_rating_key", return_value="rk-7"), \
             patch("app.plex_ratings.httpx.AsyncClient", _Client):
            result = asyncio.run(plex_ratings.push_rating(movie, member_id, 3.5))

        self.assertEqual(result, {"status": "synced"})
        url, request = _Client.last_put
        self.assertEqual(url, "http://plex.test:32400/:/rate")
        self.assertEqual(request["params"]["key"], "rk-7")
        self.assertEqual(request["params"]["rating"], 7.0)
        self.assertEqual(request["headers"]["X-Plex-Token"], "member-token")

    def test_outbound_without_relogin_is_non_blocking(self):
        member_id = self.add_member(token=None)
        movie_id = self.add_movie()
        movie = dict(db.query_one(self.conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)))
        result = asyncio.run(plex_ratings.push_rating(movie, member_id, 4.0))
        self.assertEqual(result, {"status": "not_connected"})

    def test_outbound_sync_respects_member_opt_out(self):
        member_id = self.add_member(sync_enabled=False)
        movie_id = self.add_movie()
        movie = dict(db.query_one(self.conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)))

        result = asyncio.run(plex_ratings.push_rating(movie, member_id, 4.0))

        self.assertEqual(result, {"status": "disabled_by_user"})

    def test_webhook_updates_score_and_preserves_filmclub_context(self):
        member_id = self.add_member()
        movie_id = self.add_movie()
        service.upsert_rating(self.conn, movie_id, member_id, 2.5, True, "Keep this note")
        payload = {
            "event": "media.rate",
            "Server": {"uuid": "server-uuid"},
            "Account": {"id": 42, "title": "Alice"},
            "Metadata": {
                "type": "movie", "ratingKey": "7", "userRating": 9,
                "Guid": [{"id": "tmdb://99"}],
            },
        }

        first = asyncio.run(plex_ratings.apply_webhook(self.conn, payload))
        second = asyncio.run(plex_ratings.apply_webhook(self.conn, payload))
        row = db.query_one(
            self.conn, "SELECT * FROM ratings WHERE movie_id = ? AND member_id = ?",
            (movie_id, member_id),
        )
        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(row["score"], 4.5)
        self.assertEqual(row["seen_before"], 1)
        self.assertEqual(row["note"], "Keep this note")

    def test_webhook_ignores_wrong_server_and_backlog_movies(self):
        self.add_member()
        self.add_movie(status="suggested")
        base = {
            "event": "media.rate",
            "Account": {"id": 42},
            "Metadata": {
                "type": "movie", "userRating": 8,
                "Guid": [{"id": "tmdb://99"}],
            },
        }
        wrong = {**base, "Server": {"uuid": "other-server"}}
        right = {**base, "Server": {"uuid": "server-uuid"}}
        self.assertEqual(
            asyncio.run(plex_ratings.apply_webhook(self.conn, wrong))["reason"], "server")
        self.assertEqual(
            asyncio.run(plex_ratings.apply_webhook(self.conn, right))["reason"], "movie_status")
        self.assertEqual(
            db.query_one(self.conn, "SELECT COUNT(*) c FROM ratings")["c"], 0)

    def test_webhook_respects_member_opt_out(self):
        self.add_member(sync_enabled=False)
        self.add_movie(status="watched")
        payload = {
            "event": "media.rate",
            "Server": {"uuid": "server-uuid"},
            "Account": {"id": 42, "title": "Alice"},
            "Metadata": {
                "type": "movie", "ratingKey": "7", "userRating": 9,
                "Guid": [{"id": "tmdb://99"}],
            },
        }

        result = asyncio.run(plex_ratings.apply_webhook(self.conn, payload))

        self.assertEqual(result, {"status": "ignored", "reason": "member_disabled"})
        self.assertEqual(
            db.query_one(self.conn, "SELECT COUNT(*) c FROM ratings")["c"], 0)


if __name__ == "__main__":
    unittest.main()
