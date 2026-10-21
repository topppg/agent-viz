from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from spiritboard.openrouter_auth import OpenRouterConnection, ProviderAuthError
from spiritboard.summary_providers import SummaryProviders

_KEY = "sk-or-v1-synthetic-key-not-a-real-credential"
_CALLBACK = "http://localhost:8767/api/summaries/openrouter/callback"


class FakeCodex:
    def __init__(self, *, connected: bool = False):
        self.connected = connected
        self.pending = False
        self.started = 0
        self.closed = 0
        self.calls: list[tuple[dict, float]] = []

    def start(self) -> None:
        self.started += 1

    def public_state(self) -> dict[str, object]:
        return {
            "available": True,
            "connected": self.connected,
            "pending": self.pending,
            "error": None,
            "account_id": "must-not-leak",
        }

    def begin_login(self, _callback_url: str) -> dict[str, object]:
        self.pending = True
        return {"auth_url": "https://auth.example.test/synthetic", "secret": "must-not-leak"}

    def complete(self, _state: str, _code: str, **_extra: object) -> None:
        self.pending = False
        self.connected = True

    def request(self, payload: dict, timeout: float) -> dict:
        self.calls.append((payload, timeout))
        return {"id": "synthetic-subscription-result"}

    def disconnect(self) -> None:
        self.pending = False
        self.connected = False

    def close(self) -> None:
        self.pending = False
        self.closed += 1


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.codex = FakeCodex()
        factory = patch("spiritboard.summary_providers._codex_connection", return_value=self.codex)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.providers = SummaryProviders(self.directory)

    def test_default_requires_explicit_connection_and_consent(self) -> None:
        self.assertEqual(self.providers.routes(), [])
        self.assertFalse(self.providers.public_state()["enabled"])
        with self.assertRaisesRegex(ProviderAuthError, "Connect a provider"):
            self.providers.set_enabled(True)
        with self.assertRaises(ProviderAuthError):
            self.providers.request("openrouter", {}, 1)
        self.factory.assert_not_called()

    def test_start_is_once_and_public_state_only_has_safe_cached_fields(self) -> None:
        self.providers.start()
        self.providers.start()
        self.assertEqual(self.codex.started, 1)
        with patch.object(self.codex, "request", side_effect=AssertionError("no request")):
            state = self.providers.public_state()
        self.assertNotIn("must-not-leak", json.dumps(state))
        self.assertEqual(state["policy"], "openrouter-first")
        self.assertEqual(state["providers"]["codex"]["effort"], "medium")
        self.assertEqual(state["providers"]["codex"]["model"], "gpt-6-luna")
        self.assertEqual(state["providers"]["openrouter"]["model"], "deepseek/deepseek-v4-flash")

    def test_private_manual_key_enables_and_restores_preferences(self) -> None:
        self.providers.set_key(_KEY)
        self.assertEqual(self.providers.routes(), ["openrouter"])
        preferences = self.directory / "summary-settings.json"
        self.assertEqual(json.loads(preferences.read_text()), {"version": 1, "enabled": True})
        self.assertNotIn(_KEY, preferences.read_text())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(preferences.stat().st_mode), 0o600)
        restored = SummaryProviders(self.directory)
        self.assertEqual(restored.routes(), ["openrouter"])
        self.assertNotIn(_KEY, json.dumps(restored.public_state()))

    def test_session_key_remains_memory_only_and_disconnect_clears_it(self) -> None:
        session = SummaryProviders(self.directory, session_key=_KEY, enabled=True)
        self.assertEqual(session.routes(), ["openrouter"])
        self.assertFalse((self.directory / "auth/openrouter.json").exists())
        self.assertFalse((self.directory / "summary-settings.json").exists())
        self.assertEqual(SummaryProviders(self.directory).routes(), [])
        session.disconnect("openrouter")
        self.assertEqual(session.routes(), [])
        self.assertFalse(session.public_state()["providers"]["openrouter"]["connected"])

    def test_openrouter_first_with_explicit_requests_and_billing_kind(self) -> None:
        self.codex.connected = True
        self.providers.start()
        self.providers.set_key(_KEY)
        self.assertEqual(self.providers.routes(), ["openrouter", "codex"])
        payload = {"messages": [{"role": "user", "content": "synthetic goal"}]}
        with patch("spiritboard.summaries._request", return_value={"id": "synthetic-api"}) as call:
            self.assertEqual(
                self.providers.request("openrouter", payload, 2), {"id": "synthetic-api"}
            )
            call.assert_called_once_with(_KEY, payload, 2)
        self.assertEqual(
            self.providers.request("codex", payload, 3), {"id": "synthetic-subscription-result"}
        )
        self.assertEqual(self.codex.calls, [(payload, 3)])
        self.assertEqual(self.providers.billing_kind("openrouter"), "api")
        self.assertEqual(self.providers.billing_kind("codex"), "subscription")

    def test_pause_invalidates_revision_and_survives_restart(self) -> None:
        self.providers.set_key(_KEY)
        revision = self.providers.revision
        self.assertTrue(self.providers.allowed("openrouter", revision))
        self.providers.record_success("openrouter")
        self.assertEqual(self.providers.public_state()["active_provider"], "openrouter")
        self.providers.set_enabled(False)
        self.assertFalse(self.providers.allowed("openrouter", revision))
        self.assertEqual(self.providers.routes(), [])
        self.assertIsNone(self.providers.public_state()["active_provider"])
        self.assertFalse(SummaryProviders(self.directory).public_state()["enabled"])
        self.assertEqual(OpenRouterConnection(self.directory).key(), _KEY)
        self.providers.set_enabled(True)
        self.assertEqual(self.providers.routes(), ["openrouter"])

    def test_disconnect_keeps_other_provider_available(self) -> None:
        self.codex.connected = True
        self.providers.start()
        self.providers.set_key(_KEY)
        self.providers.disconnect("openrouter")
        self.assertEqual(self.providers.routes(), ["codex"])
        self.providers.disconnect("codex")
        self.assertEqual(self.providers.routes(), [])
        self.assertFalse(self.providers.public_state()["enabled"])
        self.assertFalse(
            json.loads((self.directory / "summary-settings.json").read_text())["enabled"]
        )

    def test_codex_connection_enables_and_result_filters_secrets(self) -> None:
        result = self.providers.begin("codex", "http://localhost:8767/callback")
        self.assertEqual(result, {"auth_url": "https://auth.example.test/synthetic"})
        self.assertTrue(self.providers.public_state()["providers"]["codex"]["pending"])
        self.providers.complete("codex", "synthetic-state", "synthetic-code")
        self.assertTrue(self.providers.public_state()["enabled"])
        self.assertEqual(self.providers.routes(), ["codex"])

    def test_openrouter_callback_enables_only_after_exchange(self) -> None:
        result = self.providers.begin("openrouter", _CALLBACK)
        state = parse_qs(urlsplit(result["auth_url"]).query)["state"][0]
        self.assertFalse(self.providers.public_state()["enabled"])
        self.assertTrue(self.providers.public_state()["providers"]["openrouter"]["pending"])
        with patch("spiritboard.openrouter_auth._exchange", return_value=_KEY):
            self.providers.complete("openrouter", state, "synthetic-code")
        self.assertEqual(self.providers.routes(), ["openrouter"])
        self.assertEqual(SummaryProviders(self.directory).routes(), ["openrouter"])

    def test_pausing_before_callback_prevents_exchange(self) -> None:
        self.providers.begin("codex", "http://localhost:8767/callback")
        self.providers.set_enabled(False)
        with patch.object(self.codex, "complete") as complete:
            with self.assertRaisesRegex(ProviderAuthError, "settings changed"):
                self.providers.complete("codex", "synthetic-state", "synthetic-code")
            complete.assert_not_called()
        self.assertFalse(self.providers.public_state()["enabled"])

    def test_pausing_inflight_callback_cannot_reenable(self) -> None:
        self.providers.begin("codex", "http://localhost:8767/callback")
        ready, release = threading.Event(), threading.Event()
        failures: list[str] = []

        def complete(_state: str, _code: str, **_extra: object) -> None:
            ready.set()
            release.wait(2)
            self.codex.connected = True

        def callback() -> None:
            try:
                self.providers.complete("codex", "synthetic-state", "synthetic-code")
            except ProviderAuthError as error:
                failures.append(str(error))

        with patch.object(self.codex, "complete", side_effect=complete):
            thread = threading.Thread(target=callback)
            thread.start()
            self.assertTrue(ready.wait(1))
            self.providers.set_enabled(False)
            self.assertFalse(self.providers.public_state()["enabled"])
            release.set()
            thread.join(2)
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.providers.public_state()["enabled"])
        self.assertEqual(self.providers.routes(), [])

    def test_ui_status_and_pause_do_not_wait_for_request_network(self) -> None:
        self.providers.set_key(_KEY)
        ready, release = threading.Event(), threading.Event()

        def request(_key: str, _payload: dict, _timeout: float) -> dict:
            ready.set()
            release.wait(2)
            return {"usage": {"cost": 0.001}}

        with patch("spiritboard.summaries._request", side_effect=request):
            thread = threading.Thread(target=self.providers.request, args=("openrouter", {}, 1))
            thread.start()
            self.assertTrue(ready.wait(1))
            self.assertTrue(self.providers.public_state()["enabled"])
            self.providers.set_enabled(False)
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_disconnect_immediately_blocks_new_requests_while_provider_stops(self) -> None:
        self.codex.connected = True
        self.providers.start()
        self.providers.set_enabled(True)
        ready, release = threading.Event(), threading.Event()

        def disconnect() -> None:
            ready.set()
            release.wait(2)
            self.codex.connected = False

        with patch.object(self.codex, "disconnect", side_effect=disconnect):
            thread = threading.Thread(target=self.providers.disconnect, args=("codex",))
            thread.start()
            self.assertTrue(ready.wait(1))
            self.assertEqual(self.providers.routes(), [])
            self.assertFalse(self.providers.allowed("codex", self.providers.revision))
            self.assertFalse(self.providers.public_state()["providers"]["codex"]["connected"])
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_demo_has_no_credentials_or_external_provider_initialization(self) -> None:
        root = self.directory / "synthetic-demo"
        demo = SummaryProviders(root, demo=True, session_key=_KEY, enabled=True)
        demo.start()
        self.assertFalse(root.exists())
        self.factory.assert_not_called()
        self.assertEqual(demo.routes(), [])
        for mutation in (
            lambda: demo.begin("openrouter", _CALLBACK),
            lambda: demo.begin("codex", _CALLBACK),
            lambda: demo.set_key(_KEY),
            lambda: demo.set_enabled(False),
            lambda: demo.disconnect("openrouter"),
            lambda: demo.complete("openrouter", "synthetic-state", "synthetic-code"),
        ):
            with self.assertRaisesRegex(ProviderAuthError, "synthetic demo"):
                mutation()
        self.assertFalse(root.exists())

    def test_missing_codex_dependency_is_safe(self) -> None:
        with patch("spiritboard.summary_providers._codex_connection", return_value=None):
            self.providers.start()
        self.assertFalse(self.providers.public_state()["providers"]["codex"]["available"])
        with self.assertRaisesRegex(ProviderAuthError, "unavailable"):
            self.providers.begin("codex", "http://localhost:8767/callback")

    def test_failed_codex_start_is_not_reflected(self) -> None:
        with patch.object(self.codex, "start", side_effect=RuntimeError("private credential")):
            self.providers.start()
        state = self.providers.public_state()
        self.assertNotIn("private credential", json.dumps(state))
        self.assertIn("attention", state["providers"]["codex"]["error"])

    def test_close_preserves_saved_keys_and_settings_but_invalidates_requests(self) -> None:
        self.providers.start()
        self.providers.set_key(_KEY)
        revision = self.providers.revision
        self.providers.close()
        self.providers.close()
        self.assertFalse(self.providers.allowed("openrouter", revision))
        self.assertFalse(self.providers.public_state()["enabled"])
        self.assertEqual(self.codex.closed, 1)
        self.assertEqual(OpenRouterConnection(self.directory).key(), _KEY)
        self.assertEqual(SummaryProviders(self.directory).routes(), ["openrouter"])
        with self.assertRaises(ProviderAuthError):
            self.providers.set_enabled(True)

    def test_corrupt_preferences_fail_closed_without_exposing_content(self) -> None:
        saved = self.directory / "summary-settings.json"
        for raw in (b"private credential", b"[]", b'{"version":1,"enabled":"true"}', b"x" * 2049):
            saved.write_bytes(raw)
            saved.chmod(0o600)
            restored = SummaryProviders(self.directory)
            self.assertFalse(restored.public_state()["enabled"])
            self.assertIsNotNone(restored.public_state()["error"])
            self.assertNotIn("private credential", json.dumps(restored.public_state()))

    @unittest.skipUnless(os.name == "posix", "POSIX unsafe preference files")
    def test_unsafe_preferences_do_not_enable_or_replace_special_files(self) -> None:
        saved = self.directory / "summary-settings.json"
        target = self.directory / "synthetic-target"
        target.write_text("untouched")
        target.chmod(0o600)
        saved.symlink_to(target)
        restored = SummaryProviders(self.directory, session_key=_KEY)
        with self.assertRaisesRegex(ProviderAuthError, "safely"):
            restored.set_enabled(True)
        self.assertFalse(restored.public_state()["enabled"])
        self.assertEqual(target.read_text(), "untouched")

    def test_invalid_names_and_enablement_are_safe(self) -> None:
        for name in ("unknown-secret", "", "OPENROUTER"):
            with self.assertRaises(ProviderAuthError):
                self.providers.model(name)
            with self.assertRaises(ProviderAuthError):
                self.providers.begin(name, _CALLBACK)
            with self.assertRaises(ProviderAuthError):
                self.providers.request(name, {}, 1)
        with self.assertRaises(ProviderAuthError):
            self.providers.set_enabled("true")
