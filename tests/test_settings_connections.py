"""Connection-test coverage for unsaved Admin application settings."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-connections-bootstrap-"))
os.environ.setdefault("SESSION_SECRET", "connection-test-bootstrap-key")

from app import auth, config, main


class _Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class _Client:
    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, **kwargs):
        return self.handler(url, kwargs)


class SettingsConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.values = {
            "APP_URL": "https://filmclub.example.test",
            "TMDB_API_KEY": "tmdb-key",
            "PLEX_URL": "http://plex.example.test:32400",
            "PLEX_TOKEN": "plex-token",
            "PLEX_MACHINE_ID": "machine-1",
            "PLEX_REFRESH_INTERVAL": 3600,
            "PLEX_WEBHOOK_SECRET": "webhook-secret",
            "SEERR_URL": "http://seerr.example.test:5055",
            "SEERR_API_KEY": "seerr-key",
            "SEERR_TIMEOUT": 10,
        }

    async def test_all_configured_connections_pass_and_seerr_key_is_authenticated(self):
        calls = []

        def handler(url, kwargs):
            calls.append((url, kwargs))
            if url.endswith("/identity"):
                return _Response({"MediaContainer": {"machineIdentifier": "machine-1"}})
            if "themoviedb.org" in url:
                self.assertEqual(kwargs["params"]["api_key"], "tmdb-key")
                return _Response()
            if url.endswith("/api/v1/settings/main"):
                self.assertEqual(kwargs["headers"]["X-Api-Key"], "seerr-key")
                return _Response({"applicationTitle": "Seerr"})
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(main.httpx, "AsyncClient",
                          side_effect=lambda **kwargs: _Client(handler)):
            errors, checks = await main._test_settings_connections(
                self.values, require_all=True
            )

        self.assertEqual(errors, {})
        self.assertEqual(
            {check["id"]: check["status"] for check in checks},
            {"app_url": "ok", "tmdb": "ok", "plex": "ok", "seerr": "ok", "discord": "skipped"},
        )
        self.assertEqual(len(calls), 3)

    async def test_connection_failures_map_to_fields_without_exposing_credentials(self):
        values = dict(self.values, APP_URL="not-a-url")

        def handler(url, kwargs):
            if url.endswith("/identity"):
                return _Response({"MediaContainer": {"machineIdentifier": "other-machine"}})
            return _Response(status=401)

        with patch.object(main.httpx, "AsyncClient",
                          side_effect=lambda **kwargs: _Client(handler)):
            errors, checks = await main._test_settings_connections(
                values, require_all=True
            )

        statuses = {check["id"]: check["status"] for check in checks}
        self.assertEqual(statuses, {
            "app_url": "error", "tmdb": "error", "plex": "error", "seerr": "error",
            "discord": "skipped",
        })
        self.assertIn("APP_URL", errors)
        self.assertIn("TMDB_API_KEY", errors)
        self.assertIn("PLEX_MACHINE_ID", errors)
        self.assertIn("SEERR_URL", errors)
        response_text = repr((errors, checks))
        self.assertNotIn("tmdb-key", response_text)
        self.assertNotIn("plex-token", response_text)
        self.assertNotIn("seerr-key", response_text)

    async def test_discord_webhook_check_ok(self):
        values = dict(self.values, DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/123/abc")

        def handler(url, kwargs):
            if url.endswith("/identity"):
                return _Response({"MediaContainer": {"machineIdentifier": "machine-1"}})
            if "themoviedb.org" in url:
                return _Response()
            if url.endswith("/api/v1/settings/main"):
                return _Response({"applicationTitle": "Seerr"})
            if "discord.test" in url:
                return _Response({"id": "123", "name": "Film Club Reminders"})
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(main.httpx, "AsyncClient",
                          side_effect=lambda **kwargs: _Client(handler)):
            errors, checks = await main._test_settings_connections(values, require_all=True)

        self.assertEqual(errors, {})
        self.assertEqual({check["id"]: check["status"] for check in checks}["discord"], "ok")

    async def test_discord_webhook_check_failure_maps_to_field(self):
        values = dict(self.values, DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/123/abc")

        def handler(url, kwargs):
            if url.endswith("/identity"):
                return _Response({"MediaContainer": {"machineIdentifier": "machine-1"}})
            if "themoviedb.org" in url:
                return _Response()
            if url.endswith("/api/v1/settings/main"):
                return _Response({"applicationTitle": "Seerr"})
            if "discord.test" in url:
                return _Response(status=401)
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(main.httpx, "AsyncClient",
                          side_effect=lambda **kwargs: _Client(handler)):
            errors, checks = await main._test_settings_connections(values, require_all=True)

        self.assertEqual({check["id"]: check["status"] for check in checks}["discord"], "error")
        self.assertIn("DISCORD_WEBHOOK_URL", errors)

    def test_blank_secret_fields_keep_current_values_for_testing(self):
        old_tmdb = config.TMDB_API_KEY
        old_plex = config.PLEX_TOKEN
        try:
            config.TMDB_API_KEY = "saved-tmdb"
            config.PLEX_TOKEN = "saved-plex"
            candidate = main._settings_candidate(main.SettingsIn(
                TMDB_API_KEY="", PLEX_TOKEN="", APP_URL="https://filmclub.example.test"
            ))
        finally:
            config.TMDB_API_KEY = old_tmdb
            config.PLEX_TOKEN = old_plex
        self.assertNotIn("TMDB_API_KEY", candidate)
        self.assertNotIn("PLEX_TOKEN", candidate)

    def test_connection_test_route_requires_admin(self):
        route = next(
            route for route in main.app.routes
            if getattr(route, "path", None) == "/api/admin/settings/test"
        )
        calls = {dependency.call for dependency in route.dependant.dependencies}
        self.assertIn(auth.require_admin, calls)

    async def test_connection_test_route_does_not_save_candidate_values(self):
        body = main.SettingsIn(
            APP_URL="https://new.example.test", TMDB_API_KEY="new-key"
        )
        checks = [{
            "id": "app_url", "label": "Application URL",
            "status": "ok", "detail": "URL format is valid",
        }]
        with patch.object(
            main, "_test_settings_connections",
            new=AsyncMock(return_value=({}, checks)),
        ) as test_connections, patch.object(main.app_settings, "save") as save:
            result = await main.api_admin_test_settings(body, admin={"id": 1})

        self.assertEqual(result, {"ok": True, "checks": checks, "errors": {}})
        test_connections.assert_awaited_once_with(
            {"APP_URL": "https://new.example.test", "TMDB_API_KEY": "new-key"},
            require_all=True,
        )
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
