from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from spiritboard.openrouter_auth import OpenRouterConnection, ProviderAuthError

_KEY = "sk-or-v1-synthetic-key-not-a-real-credential"
_CALLBACK = "http://localhost:8767/api/summaries/openrouter/callback"


class OpenRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.connection = OpenRouterConnection(self.directory)

    def begin(self) -> dict[str, list[str]]:
        result = self.connection.begin(_CALLBACK)
        self.assertEqual(result["expires_in"], 600)
        return parse_qs(urlsplit(str(result["auth_url"])).query)

    def response(self, body: bytes, *, status: int = 200, length: str | None = None):
        response = MagicMock()
        response.status = status
        response.getheader.return_value = length
        response.read1.side_effect = [body, b""]
        connection = MagicMock()
        connection.getresponse.return_value = response
        connection.sock = None
        return connection

    def test_begin_uses_fixed_origin_pkce_state_and_callback(self) -> None:
        query = self.begin()
        self.assertEqual(query["callback_url"], [_CALLBACK])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertGreaterEqual(len(query["state"][0]), 43)
        self.assertEqual(len(query["code_challenge"][0]), 43)
        second = self.begin()
        self.assertNotEqual(query["state"], second["state"])
        self.assertNotEqual(query["code_challenge"], second["code_challenge"])
        self.assertEqual(
            self.connection.public_state(),
            {"available": True, "connected": False, "pending": True, "error": None},
        )

    def test_callback_validation_never_allows_remote_or_reflected_routes(self) -> None:
        for callback in (
            "https://localhost:8767/api/summaries/openrouter/callback",
            "http://example.test:8767/api/summaries/openrouter/callback",
            "http://localhost/api/summaries/openrouter/callback",
            "http://user@localhost:8767/api/summaries/openrouter/callback",
            _CALLBACK + "?redirect=secret",
            _CALLBACK + "#secret",
            _CALLBACK + "/",
            _CALLBACK.replace("8767", "99999"),
            _CALLBACK.replace("localhost", "LOCALHOST"),
            _CALLBACK.replace("localhost", "127.1"),
            "\n" + _CALLBACK,
            _CALLBACK.replace("localhost", "local\thost"),
        ):
            with self.subTest(callback=callback):
                with self.assertRaisesRegex(ProviderAuthError, "local dashboard callback"):
                    self.connection.begin(callback)
        self.connection.begin(_CALLBACK.replace("localhost", "127.0.0.1"))

    def test_success_exchanges_exact_verifier_and_persists_private_key(self) -> None:
        query = self.begin()
        transport = self.response(json.dumps({"key": _KEY}).encode())
        with patch(
            "spiritboard.openrouter_auth.http.client.HTTPSConnection", return_value=transport
        ):
            self.connection.complete(query["state"][0], "synthetic-code")
        args, kwargs = transport.request.call_args
        self.assertEqual(args, ("POST", "/api/v1/auth/keys"))
        body = json.loads(kwargs["body"])
        self.assertEqual(body["code"], "synthetic-code")
        self.assertEqual(body["code_challenge_method"], "S256")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        self.assertEqual(challenge, query["code_challenge"][0])
        self.assertEqual(self.connection.key(), _KEY)
        self.assertEqual(OpenRouterConnection(self.directory).key(), _KEY)
        saved = self.directory / "auth/openrouter.json"
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)
        self.assertNotIn(_KEY, json.dumps(self.connection.public_state()))
        self.assertNotIn(body["code_verifier"], json.dumps(self.connection.public_state()))
        self.assertNotIn(query["state"][0], json.dumps(self.connection.public_state()))
        with self.assertRaises(ProviderAuthError):
            self.connection.complete(query["state"][0], "synthetic-code")

    def test_expiry_and_mismatch_do_not_contact_provider(self) -> None:
        query = self.begin()
        with patch("spiritboard.openrouter_auth._exchange") as exchange:
            with self.assertRaises(ProviderAuthError):
                self.connection.complete("wrong-state", "synthetic-code")
            exchange.assert_not_called()
        with patch("spiritboard.openrouter_auth.time.monotonic", return_value=100):
            query = self.begin()
        with patch("spiritboard.openrouter_auth.time.monotonic", return_value=701):
            with patch("spiritboard.openrouter_auth._exchange") as exchange:
                with self.assertRaises(ProviderAuthError):
                    self.connection.complete(query["state"][0], "synthetic-code")
                exchange.assert_not_called()

    def test_malformed_state_is_a_safe_failure(self) -> None:
        for state in (None, "x" * 10000, "\ud800", "é" * 43):
            self.begin()
            with patch("spiritboard.openrouter_auth._exchange") as exchange:
                with self.assertRaises(ProviderAuthError):
                    self.connection.complete(state, "synthetic-code")
                exchange.assert_not_called()

    def test_public_state_expires_pending_and_restart_cancels_it(self) -> None:
        with patch("spiritboard.openrouter_auth.time.monotonic", return_value=100):
            self.begin()
        with patch("spiritboard.openrouter_auth.time.monotonic", return_value=701):
            self.assertFalse(self.connection.public_state()["pending"])
        self.begin()
        restarted = OpenRouterConnection(self.directory)
        self.assertFalse(restarted.public_state()["pending"])

    def test_malformed_codes_are_not_sent(self) -> None:
        for code in ("", "secret\r\nheader", "secret/code", "x" * 2049, None):
            query = self.begin()
            with patch("spiritboard.openrouter_auth._exchange") as exchange:
                with self.assertRaises(ProviderAuthError):
                    self.connection.complete(query["state"][0], code)
                exchange.assert_not_called()

    def test_invalid_response_and_errors_are_fixed_not_reflected(self) -> None:
        responses = (
            self.response(b"private upstream message", status=403),
            self.response(b"private upstream message", status=302),
            self.response(b"not json"),
            self.response(b"[]"),
            self.response(b'{"key":"private upstream message"}'),
            self.response(b"x" * 8193),
            self.response(b"{}", length="999999"),
            self.response(b"{}", length="invalid"),
        )
        for transport in responses:
            with self.subTest(transport=transport):
                query = self.begin()
                with patch(
                    "spiritboard.openrouter_auth.http.client.HTTPSConnection",
                    return_value=transport,
                ):
                    with self.assertRaisesRegex(ProviderAuthError, "OpenRouter sign-in failed"):
                        self.connection.complete(query["state"][0], "synthetic-code")
                self.assertNotIn("private upstream", json.dumps(self.connection.public_state()))
                self.assertIsNone(self.connection.key())

    def test_timeout_is_bounded_even_when_headers_hang(self) -> None:
        release = threading.Event()
        transport = self.response(b"{}")
        response = transport.getresponse.return_value

        def delayed_response():
            release.wait(2)
            return response

        transport.getresponse.side_effect = delayed_response
        query = self.begin()
        start = time.monotonic()
        try:
            with patch("spiritboard.openrouter_auth._TIMEOUT", 0.02):
                with patch(
                    "spiritboard.openrouter_auth.http.client.HTTPSConnection",
                    return_value=transport,
                ):
                    with self.assertRaisesRegex(ProviderAuthError, "OpenRouter sign-in failed"):
                        self.connection.complete(query["state"][0], "synthetic-code")
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            release.set()

    def test_transport_failure_is_safe(self) -> None:
        transport = self.response(b"{}")
        transport.request.side_effect = OSError("private upstream text")
        query = self.begin()
        with patch(
            "spiritboard.openrouter_auth.http.client.HTTPSConnection", return_value=transport
        ):
            with self.assertRaisesRegex(ProviderAuthError, "OpenRouter sign-in failed"):
                self.connection.complete(query["state"][0], "synthetic-code")
        self.assertNotIn("private upstream", json.dumps(self.connection.public_state()))

    def test_inflight_exchange_cannot_override_disconnect_new_begin_or_manual_key(self) -> None:
        for change in ("disconnect", "begin", "set_key"):
            with self.subTest(change=change):
                query = self.begin()
                ready = threading.Event()
                release = threading.Event()
                failures: list[str] = []

                def exchange(
                    _code: str,
                    _verifier: str,
                    ready_event: threading.Event = ready,
                    release_event: threading.Event = release,
                ) -> str:
                    ready_event.set()
                    release_event.wait(2)
                    return _KEY

                def complete(
                    request_state: str = query["state"][0],
                    collected: list[str] = failures,
                ) -> None:
                    try:
                        self.connection.complete(request_state, "synthetic-code")
                    except ProviderAuthError as error:
                        collected.append(str(error))

                with patch("spiritboard.openrouter_auth._exchange", side_effect=exchange):
                    thread = threading.Thread(target=complete)
                    thread.start()
                    self.assertTrue(ready.wait(1))
                    replacement = _KEY + "-replacement"
                    if change == "set_key":
                        self.connection.set_key(replacement)
                    elif change == "begin":
                        self.begin()
                    else:
                        self.connection.disconnect()
                    release.set()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(failures), 1)
                expected = replacement if change == "set_key" else None
                self.assertEqual(self.connection.key(), expected)
                self.assertEqual(OpenRouterConnection(self.directory).key(), expected)
                self.connection.disconnect()

    def test_manual_key_validation_disconnect_and_no_network(self) -> None:
        for key in ("", "secret", "Bearer " + _KEY, _KEY + "\n", "sk-or-" + "x" * 251):
            with self.assertRaisesRegex(ProviderAuthError, "valid OpenRouter API key"):
                self.connection.set_key(key)
        with patch("spiritboard.openrouter_auth.http.client.HTTPSConnection") as transport:
            self.connection.set_key(_KEY)
            self.assertTrue(self.connection.public_state()["connected"])
            self.begin()
            self.connection.disconnect()
            self.assertFalse(self.connection.public_state()["connected"])
            self.assertFalse(self.connection.public_state()["pending"])
            transport.assert_not_called()
        self.assertFalse((self.directory / "auth/openrouter.json").exists())

    def test_malformed_storage_is_safe_and_does_not_expose_content(self) -> None:
        saved = self.directory / "auth/openrouter.json"
        for raw in (b"private transcript", b"x" * 8193, b"[]", b'{"version":1,"key":"bad"}'):
            saved.write_bytes(raw)
            saved.chmod(0o600)
            restored = OpenRouterConnection(self.directory)
            self.assertIsNone(restored.key())
            self.assertIn("safely", str(restored.public_state()["error"]))
            self.assertNotIn("private transcript", json.dumps(restored.public_state()))

    @unittest.skipUnless(os.name == "posix", "POSIX unsafe file types and permissions")
    def test_symlinks_fifo_hardlinks_and_public_files_are_rejected(self) -> None:
        saved = self.directory / "auth/openrouter.json"
        target = self.directory / "synthetic-target"
        target.write_text("untouched")
        target.chmod(0o600)
        for kind in ("symlink", "fifo", "hardlink", "public"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    saved.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(saved, 0o600)
                elif kind == "hardlink":
                    os.link(target, saved)
                else:
                    saved.write_text("private key")
                    saved.chmod(0o644)
                restored = OpenRouterConnection(self.directory)
                self.assertIsNone(restored.key())
                with self.assertRaises(ProviderAuthError):
                    restored.set_key(_KEY)
                with self.assertRaises(ProviderAuthError):
                    restored.disconnect()
                self.assertEqual(target.read_text(), "untouched")
                saved.unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX directory permissions")
    def test_existing_public_directory_not_silently_chmodded(self) -> None:
        auth = self.directory / "auth"
        auth.chmod(0o755)
        restored = OpenRouterConnection(self.directory)
        with self.assertRaises(ProviderAuthError):
            restored.set_key(_KEY)
        self.assertEqual(stat.S_IMODE(auth.stat().st_mode), 0o755)

    def test_failed_atomic_replace_keeps_previous_key_and_removes_temporary(self) -> None:
        self.connection.set_key(_KEY)
        with patch("spiritboard.openrouter_auth.os.replace", side_effect=OSError("secret")):
            with self.assertRaises(ProviderAuthError):
                self.connection.set_key(_KEY + "-replacement")
        self.assertEqual(self.connection.key(), _KEY)
        self.assertEqual(OpenRouterConnection(self.directory).key(), _KEY)
        self.assertEqual(
            [path.name for path in (self.directory / "auth").iterdir()], ["openrouter.json"]
        )
