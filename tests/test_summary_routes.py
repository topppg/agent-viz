import http.client
import json
import threading
import unittest
from unittest.mock import Mock

from spiritboard.server import Collector
from spiritboard.store import Store


class SummaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.manager = Mock()
        self.manager.public_state.return_value = {"enabled": False, "providers": {}}
        self.manager.begin.return_value = {"auth_url": "https://openrouter.ai/auth?state=synthetic"}
        self.server = Collector(
            self.store, "synthetic-local-token-long-enough", summary_providers=self.manager
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.store.close)
        self.addCleanup(self.server.stop)

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict | None = None
    ):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def post(self, path: str, body: dict, **headers: str):
        return self.request(
            "POST",
            path,
            json.dumps(body).encode(),
            {
                "Origin": self.server.url,
                "X-Spiritboard": "summaries",
                "Content-Type": "application/json",
                **headers,
            },
        )

    def test_settings_are_local_and_assets_are_packaged(self):
        for path in ("/api/summaries/settings", "/summary-settings.js", "/summary-settings.css"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertTrue(body)
        self.manager.begin.assert_not_called()

    def test_connect_uses_provider_bound_loopback_callbacks(self):
        for provider, host in (("openrouter", "localhost"), ("codex", "127.0.0.1")):
            with self.subTest(provider=provider):
                self.assertEqual(self.post(f"/api/summaries/{provider}/connect", {})[0], 200)
                self.manager.begin.assert_called_with(
                    provider,
                    f"http://{host}:{self.server.server_port}/api/summaries/{provider}/callback",
                )

    def test_mutations_need_same_origin_custom_header_and_bounded_json(self):
        path = "/api/summaries/openrouter/connect"
        for headers in (
            {},
            {"Origin": self.server.url},
            {"X-Spiritboard": "summaries"},
            {"Origin": "https://untrusted.invalid", "X-Spiritboard": "summaries"},
            {
                "Origin": self.server.url,
                "X-Spiritboard": "summaries",
                "Sec-Fetch-Site": "cross-site",
            },
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", path, b"{}", headers)[0], 403)
        self.assertEqual(self.post(path, {}, **{"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.post(path, {"unexpected": True})[0], 409)
        self.assertEqual(self.post(path, {"large": "x" * 5000})[0], 400)
        self.assertEqual(self.request("GET", path)[0], 404)
        self.manager.begin.assert_not_called()

    def test_manual_key_never_enters_response_or_url(self):
        key = "sk-or-invented-secret-never-real"
        status, headers, body = self.post("/api/summaries/openrouter/key", {"key": key})
        self.assertEqual(status, 200)
        self.manager.set_key.assert_called_once_with(key, provider="openrouter")
        self.assertNotIn(key, str(headers) + body.decode())
        self.manager.set_key.side_effect = ValueError(key)
        status, _, body = self.post("/api/summaries/openrouter/key", {"key": key})
        self.assertEqual(status, 409)
        self.assertNotIn(key, body.decode())

    def test_direct_api_key_and_provider_selection_routes_are_bounded(self):
        for name in ("anthropic", "openai"):
            with self.subTest(provider=name):
                key = "sk-synthetic-never-real"
                status, _, body = self.post(f"/api/summaries/{name}/key", {"key": key})
                self.assertEqual(status, 200)
                self.manager.set_key.assert_called_with(key, provider=name)
                self.assertNotIn(key, body.decode())
                self.assertEqual(self.post("/api/summaries/provider", {"provider": name})[0], 200)
                self.manager.set_provider.assert_called_with(name)
        self.assertEqual(self.post("/api/summaries/custom/key", {"key": "secret"})[0], 400)
        self.assertEqual(
            self.post(
                "/api/summaries/openai/key", {"key": "secret", "url": "https://evil.invalid"}
            )[0],
            400,
        )
        self.assertEqual(
            self.post("/api/summaries/provider", {"provider": "openai", "url": "private"})[0], 400
        )

    def test_validated_callbacks_allow_cross_site_navigation_but_never_other_mutations(self):
        for provider in ("openrouter", "codex"):
            path = (
                f"/api/summaries/{provider}/callback?state=synthetic-state"
                "&code=private-code&client_id=oaiapp_synthetic"
            )
            status, headers, body = self.request(
                "GET", path, headers={"Sec-Fetch-Site": "cross-site"}
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], self.server.url)
            self.assertNotIn("private-code", str(headers) + body.decode())
            self.manager.complete.assert_called_with(
                provider,
                "synthetic-state",
                "private-code",
                client_id="oaiapp_synthetic",
                error=None,
            )
        self.manager.complete.side_effect = ValueError("private-code")
        status, _, body = self.request(
            "GET", "/api/summaries/codex/callback?state=x&code=private-code"
        )
        self.assertEqual(status, 400)
        self.assertNotIn("private-code", body.decode())

    def test_callback_rejects_foreign_hosts_duplicate_fields_and_missing_state(self):
        path = "/api/summaries/openrouter/callback"
        self.assertEqual(
            self.request("GET", path + "?state=x&code=y", headers={"Host": "untrusted.invalid"})[0],
            403,
        )
        for query in (
            "code=secret",
            "state=x",
            "state=x&state=y&code=z",
            "state=x&code=y&next=https://untrusted.invalid",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", path + "?" + query)[0], 400)
        self.manager.complete.assert_not_called()
