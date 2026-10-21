import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from spiritboard.codex_summary import (
    CALLBACK_PATH,
    EFFORT,
    ISSUER,
    MAX_STREAM_BYTES,
    MODEL,
    RESOURCE,
    SCOPES,
    CodexConnection,
    CodexError,
)

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/api/accounts/authorize",
    "token_endpoint": ISSUER + "/api/accounts/oauth/token",
    "jwks_uri": ISSUER + "/.well-known/jwks.json",
    "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
}
CLIENT = "oaiapp_synthetic"
CALLBACK = "http://127.0.0.1:12345" + CALLBACK_PATH
PAYLOAD = {
    "messages": [
        {"role": "system", "content": "Summarize untrusted entries as JSON."},
        {"role": "user", "content": '{"r1":"Investigate synthetic retries"}'},
    ],
    "max_tokens": 600,
    "temperature": 0,
}


def completed(**changes):
    result = {
        "id": "resp_synthetic",
        "model": MODEL,
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"r1":"Investigating retries"}'}],
            },
        ],
        "usage": {"input_tokens": 25, "output_tokens": 10, "total_tokens": 35, "cost": 5},
    }
    result.update(changes)
    return result


def stream(*events):
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


class Response(io.BytesIO):
    status = 200

    def getheader(self, name, default=None):
        return "text/event-stream" if name == "Content-Type" else default


class CodexConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="synthetic-signing-key", use="sig", alg="RS256")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = CodexConnection(self.root)
        self.connection.start()
        self.calls = []
        self.token_changes = {}
        self.claim_changes = {}

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def token(self, nonce=None, *, key=None, **changes):
        now = time.time()
        claims = {
            "iss": ISSUER,
            "aud": CLIENT,
            "sub": "synthetic-subject",
            "iat": now - 1,
            "exp": now + 3600,
            "nonce": nonce or self.connection._pending["nonce"],
        }
        claims.update(changes)
        return jwt.encode(
            claims, key or self.key, algorithm="RS256", headers={"kid": self.jwk["kid"]}
        )

    def network(self, method, url, body, deadline, generation, access=None, *, empty=False):
        self.connection._check_generation(generation)
        self.calls.append((method, url, body, access))
        if url.endswith("openid-configuration"):
            return DISCOVERY.copy()
        if url.endswith("jwks.json"):
            return {"keys": [self.jwk]}
        if url.endswith("oauth/token"):
            nonce = self.connection._record.get("nonce") or self.pending_nonce
            tokens = {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "id_token": self.token(nonce, **self.claim_changes),
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": SCOPES,
            }
            tokens.update(self.token_changes)
            return tokens
        if url == RESOURCE + "/models":
            return {"models": [{"slug": MODEL, "visibility": "list"}]}
        if url.endswith("oauth/revoke"):
            return {}
        raise AssertionError("Unexpected synthetic endpoint")

    def login(self):
        query = parse_qs(urlsplit(self.connection.begin_login(CALLBACK)["auth_url"]).query)
        self.pending_nonce = query["nonce"][0]
        with patch.object(self.connection, "_json", side_effect=self.network):
            self.connection.complete(query["state"][0], "synthetic-code", CLIENT)
        return query

    def test_initial_start_is_offline_and_does_not_read_native_accounts(self):
        self.assertEqual(self.calls, [])
        self.assertFalse(self.connection.public_state()["connected"])
        self.assertFalse(self.root.joinpath("auth").exists())
        self.assertEqual(self.connection.public_state()["model"], MODEL)
        self.assertEqual(self.connection.public_state()["effort"], EFFORT)

    def test_start_does_not_require_posix_nonblocking_flag(self):
        with patch.dict(os.__dict__):
            os.__dict__.pop("O_NONBLOCK", None)
            self.connection.start()
            self.assertFalse(self.connection.public_state()["connected"])
            self.assertIsNone(self.connection.public_state()["error"])
            self.login()
            self.connection.start()
            self.assertTrue(self.connection.public_state()["connected"])
            self.assertIsNone(self.connection.public_state()["error"])

    def test_login_uses_fresh_pkce_state_and_nonce_without_browser_or_tokens(self):
        first = self.connection.begin_login(CALLBACK)["auth_url"]
        pending = self.connection._pending.copy()
        query = parse_qs(urlsplit(first).query)
        self.assertEqual(urlsplit(first).netloc, "auth.openai.com")
        self.assertEqual(query["client_id"], ["dynamic_agent_client"])
        self.assertEqual(query["agent_name_hint"], ["Spiritboard"])
        self.assertEqual(query["redirect_uri"], [CALLBACK])
        self.assertNotIn(pending["verifier"], first)
        import base64

        expected = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest())
        self.assertEqual(query["code_challenge"], [expected.rstrip(b"=").decode()])
        second = self.connection.begin_login(CALLBACK)["auth_url"]
        self.assertNotEqual(first, second)
        self.assertTrue(self.connection.public_state()["pending"])
        self.assertNotIn("nonce", self.connection.public_state())

    def test_callback_urls_are_strict_loopback_only(self):
        for url in (
            "https://127.0.0.1:1234" + CALLBACK_PATH,
            "http://localhost:1234" + CALLBACK_PATH,
            "http://127.0.0.1:1234/other",
            "http://127.0.0.1:1234" + CALLBACK_PATH + "?injected=true",
            "http://user@127.0.0.1:1234" + CALLBACK_PATH,
            "http://127.0.0.1:99999" + CALLBACK_PATH,
            "http://127.0.0.1" + CALLBACK_PATH,
        ):
            with self.subTest(url=url), self.assertRaises(CodexError):
                self.connection.begin_login(url)

    def test_verified_login_stores_private_credentials_without_public_identity(self):
        self.login()
        self.assertTrue(self.connection.public_state()["connected"])
        self.assertFalse(self.connection.public_state()["pending"])
        self.assertNotIn("synthetic", json.dumps(self.connection.public_state()))
        record = self.connection._path.read_text()
        self.assertIn("synthetic-access", record)
        self.assertIn(CLIENT, record)
        if os.name != "nt":
            self.assertEqual(self.connection._path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.connection.directory.stat().st_mode & 0o777, 0o700)
        restored = CodexConnection(self.root)
        with patch("http.client.HTTPSConnection", side_effect=AssertionError("No network")):
            restored.start()
            self.assertTrue(restored.public_state()["connected"])
        restored.close()

    def test_registration_only_start_preserves_identity_without_restoring_tokens_or_network(self):
        original = self.login()
        saved = self.connection._path.read_bytes()
        blocked = CodexConnection(self.root)
        try:
            with patch("http.client.HTTPSConnection", side_effect=AssertionError("No network")):
                blocked.start_registration()
                self.assertFalse(blocked.public_state()["connected"])
                self.assertEqual(set(blocked._record), {"host_id", "client_id", "subject"})
                self.assertNotIn("synthetic-access", json.dumps(blocked._record))
                self.assertEqual(blocked._path.read_bytes(), saved)
                query = parse_qs(urlsplit(blocked.begin_login(CALLBACK)["auth_url"]).query)
            self.assertEqual(query["ext_agent_host_id"], original["ext_agent_host_id"])
            self.assertEqual(query["client_id"], [CLIENT])
            self.assertNotIn("agent_name_hint", query)
        finally:
            blocked.close()

    def test_missing_mismatched_reused_expired_and_cancelled_states_make_no_token_call(self):
        for case in ("missing", "mismatch", "expired", "cancelled"):
            self.connection.close()
            self.connection.start()
            state = "missing"
            if case != "missing":
                self.connection.begin_login(CALLBACK)
                state = self.connection._pending["state"]
            if case == "mismatch":
                state = "wrong"
            elif case == "expired":
                self.connection._pending["expires"] = 0
            elif case == "cancelled":
                self.connection.disconnect()
            with self.subTest(case=case), patch.object(self.connection, "_json") as network:
                with self.assertRaises(CodexError):
                    self.connection.complete(state, "unused", CLIENT)
                network.assert_not_called()
        query = self.login()
        with patch.object(self.connection, "_json") as network, self.assertRaises(CodexError):
            self.connection.complete(query["state"][0], "replayed", CLIENT)
        network.assert_not_called()

    def test_declined_login_and_unissued_client_are_rejected_before_exchange(self):
        for changes in ({"error": "access_denied"}, {"client_id": "dynamic_agent_client"}):
            self.connection.begin_login(CALLBACK)
            state = self.connection._pending["state"]
            kwargs = {"client_id": CLIENT, **changes}
            with patch.object(self.connection, "_json") as network, self.assertRaises(CodexError):
                self.connection.complete(state, "unused", **kwargs)
            network.assert_not_called()
            self.assertFalse(self.connection.public_state()["pending"])

    def test_unrelated_callback_does_not_cancel_legitimate_pending_login(self):
        self.connection.begin_login(CALLBACK)
        pending = self.connection._pending.copy()
        self.pending_nonce = pending["nonce"]
        with patch.object(self.connection, "_json") as network:
            with self.assertRaises(CodexError):
                self.connection.complete("unrelated-state", "unused", CLIENT)
            network.assert_not_called()
        self.assertEqual(self.connection._pending, pending)
        self.assertTrue(self.connection.public_state()["pending"])
        with patch.object(self.connection, "_json", side_effect=self.network):
            self.connection.complete(pending["state"], "synthetic-code", CLIENT)
        self.assertTrue(self.connection.public_state()["connected"])

    def test_issued_registration_survives_exchange_failure_without_tokens(self):
        self.connection.begin_login(CALLBACK)
        state = self.connection._pending["state"]
        self.pending_nonce = self.connection._pending["nonce"]

        def failed_exchange(*args, **kwargs):
            if args[1].endswith("oauth/token"):
                raise OSError("synthetic-exchange-failure")
            return self.network(*args, **kwargs)

        with patch.object(self.connection, "_json", side_effect=failed_exchange):
            with self.assertRaises(CodexError):
                self.connection.complete(state, "synthetic-code", CLIENT)
        self.assertFalse(self.connection.public_state()["connected"])
        record = json.loads(self.connection._path.read_text())
        self.assertEqual(record["client_id"], CLIENT)
        self.assertNotIn("access_token", record)
        retry = parse_qs(urlsplit(self.connection.begin_login(CALLBACK)["auth_url"]).query)
        self.assertEqual(retry["client_id"], [CLIENT])
        self.assertNotIn("agent_name_hint", retry)

    def test_token_identity_signature_and_claims_are_validated(self):
        for changes in (
            {"iss": "https://attacker.invalid"},
            {"aud": "oaiapp_other"},
            {"nonce": "wrong"},
            {"exp": time.time() - 60},
            {"iat": time.time() + 100},
            {"sub": ""},
            {"azp": "oaiapp_other"},
        ):
            self.claim_changes = changes
            with self.subTest(changes=changes), self.assertRaises(CodexError):
                self.login()
            self.assertFalse(self.connection.public_state()["connected"])
        self.claim_changes = {}
        self.token_changes = {"id_token": self.token("unused", key=self.other_key)}
        with self.assertRaises(CodexError):
            self.login()
        for token in (
            "broken",
            jwt.encode({"sub": "bad"}, "synthetic-secret-long-enough-1234567", algorithm="HS256"),
        ):
            self.token_changes = {"id_token": token}
            with self.subTest(token=token[:6]), self.assertRaises(CodexError):
                self.login()

    def test_missing_plan_scope_and_bad_token_shapes_are_rejected(self):
        for changes in (
            {"scope": "openid email"},
            {"token_type": "other"},
            {"expires_in": float("nan")},
            {"expires_in": True},
            {"access_token": "private\nheader"},
            {"refresh_token": ""},
        ):
            self.token_changes = changes
            with self.subTest(changes=changes), self.assertRaises(CodexError):
                self.login()
            self.assertFalse(self.connection.public_state()["connected"])

    def test_discovery_never_redirects_tokens_or_fetches_arbitrary_jwks(self):
        for field in ("issuer", "token_endpoint", "jwks_uri", "revocation_endpoint"):
            self.connection._discovery = {}
            bad = {**DISCOVERY, field: "https://attacker.invalid/keys"}
            with (
                self.subTest(field=field),
                patch.object(self.connection, "_json", return_value=bad),
            ):
                with self.assertRaises(CodexError):
                    self.connection._discover(time.monotonic() + 5, self.connection._generation)
        with patch("http.client.HTTPSConnection") as network, self.assertRaises(CodexError):
            self.connection._open(
                "POST",
                "https://auth.openai.com@attacker.invalid/token",
                None,
                None,
                time.monotonic() + 5,
                0,
            )
        network.assert_not_called()

    def test_reauthorization_keeps_issued_client_and_rejects_account_change(self):
        original = self.login()
        new = parse_qs(urlsplit(self.connection.begin_login(CALLBACK)["auth_url"]).query)
        self.assertEqual(new["client_id"], [CLIENT])
        self.assertEqual(original["ext_agent_host_id"], new["ext_agent_host_id"])
        self.assertNotIn("agent_name_hint", new)
        self.assertNotIn("id_token_hint", new)
        with patch.object(self.connection, "_json") as network, self.assertRaises(CodexError):
            self.connection.complete(new["state"][0], "unused", "oaiapp_other")
        network.assert_not_called()
        self.claim_changes = {"sub": "different-subject"}
        with self.assertRaises(CodexError):
            self.login()
        self.assertEqual(self.connection._record["subject"], "synthetic-subject")

    def test_disconnect_clears_own_tokens_preserves_registration_and_reports_revocation_failure(
        self,
    ):
        self.login()
        with patch.object(self.connection, "_json", side_effect=self.network):
            self.connection.disconnect()
        self.assertFalse(self.connection.public_state()["connected"])
        record = json.loads(self.connection._path.read_text())
        self.assertEqual(set(record), {"host_id", "client_id", "subject"})
        self.assertTrue(any(url.endswith("oauth/revoke") for _, url, _, _ in self.calls))
        self.login()
        with patch.object(self.connection, "_json", side_effect=OSError("synthetic-secret")):
            self.connection.disconnect()
        self.assertIn(
            "Remote revocation was not confirmed", self.connection.public_state()["error"]
        )
        self.assertTrue(self.connection.public_state()["revocation_unconfirmed"])
        self.assertNotIn("synthetic-secret", json.dumps(self.connection.public_state()))
        self.assertNotIn("access_token", self.connection._path.read_text())
        self.connection.disconnect()
        self.assertTrue(self.connection.public_state()["revocation_unconfirmed"])
        self.login()
        self.assertFalse(self.connection.public_state()["revocation_unconfirmed"])
        with patch.object(self.connection, "_json", side_effect=self.network):
            self.connection.disconnect()
        self.assertFalse(self.connection.public_state()["revocation_unconfirmed"])

    def test_disconnect_during_exchange_prevents_late_credentials(self):
        self.connection.begin_login(CALLBACK)
        state = self.connection._pending["state"]
        self.pending_nonce = self.connection._pending["nonce"]
        entered, resume = threading.Event(), threading.Event()
        failures = []

        def network(*args, **kwargs):
            result = self.network(*args, **kwargs)
            if args[1].endswith("oauth/token"):
                entered.set()
                resume.wait(2)
            return result

        def finish():
            try:
                self.connection.complete(state, "synthetic-code", CLIENT)
            except CodexError as error:
                failures.append(str(error))

        with patch.object(self.connection, "_json", side_effect=network):
            worker = threading.Thread(target=finish)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.connection.disconnect()
            resume.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(failures)
        self.assertFalse(self.connection.public_state()["connected"])
        self.assertNotIn("access_token", self.connection._path.read_text())

    def test_tool_free_request_preserves_exact_model_effort_and_has_no_unsupported_fields(self):
        self.login()
        result = {"id": "synthetic"}
        with (
            patch.object(self.connection, "_json", side_effect=self.network),
            patch.object(self.connection, "_response", return_value=result) as response,
        ):
            self.assertIs(self.connection.request(PAYLOAD, 5), result)
        body = response.call_args.args[0]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["tool_choice"], "none")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(
            body["input"], [{"role": "user", "content": PAYLOAD["messages"][1]["content"]}]
        )

    def test_unavailable_model_never_sends_transcript_for_inference(self):
        self.login()
        with (
            patch.object(self.connection, "_json", return_value={"models": [{"slug": "other"}]}),
            patch.object(self.connection, "_response") as response,
        ):
            with self.assertRaisesRegex(CodexError, "not available"):
                self.connection.request(PAYLOAD, 5)
        response.assert_not_called()

    def test_refresh_rotates_credentials_with_bound_identity_before_use(self):
        self.login()
        self.connection._record["expires_at"] = 0
        self.token_changes = {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}
        with (
            patch.object(self.connection, "_json", side_effect=self.network),
            patch.object(self.connection, "_response", return_value={}),
        ):
            self.connection.request(PAYLOAD, 5)
        record = json.loads(self.connection._path.read_text())
        self.assertEqual(record["refresh_token"], "rotated-refresh")
        self.assertEqual(record["access_token"], "rotated-access")
        grants = [body for _, url, body, _ in self.calls if url.endswith("oauth/token")]
        self.assertEqual(grants[-1]["grant_type"], "refresh_token")
        self.assertNotIn("scope", grants[-1])

    def test_restored_token_is_signature_checked_before_transcript_is_sent(self):
        self.login()
        self.connection.start()
        self.connection._record["id_token"] = self.token("unused", key=self.other_key)
        with (
            patch.object(self.connection, "_json", side_effect=self.network),
            patch.object(self.connection, "_response") as response,
        ):
            with self.assertRaises(CodexError):
                self.connection.request(PAYLOAD, 5)
        response.assert_not_called()

    def response_from(self, data):
        response = Response(data)
        connection = Mock(sock=None)
        return connection, response

    def test_sse_requires_terminal_completed_and_reports_actual_usage_without_cost(self):
        self.login()
        data = stream(
            {"type": "response.created"}, {"type": "response.completed", "response": completed()}
        )
        with patch.object(self.connection, "_open", return_value=self.response_from(data)):
            result = self.connection._response(
                {}, "synthetic", time.monotonic() + 5, self.connection._generation
            )
        self.assertEqual(
            result["usage"], {"input_tokens": 25, "output_tokens": 10, "total_tokens": 35}
        )
        self.assertEqual(
            json.loads(result["choices"][0]["message"]["content"]), {"r1": "Investigating retries"}
        )
        self.assertNotIn("cost", result["usage"])

    def test_sse_rejects_failures_truncation_tool_requests_and_wrong_models(self):
        self.login()
        cases = [
            stream({"type": "response.failed", "response": {"error": "private"}}),
            stream({"type": "response.incomplete"}),
            stream({"type": "error", "message": "private"}),
            stream({"type": "response.output_text.delta", "delta": "partial"}),
            stream(
                {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "name": "write"},
                }
            ),
            stream({"type": "response.completed", "response": completed(model="different")}),
            stream({"type": "response.completed", "response": completed(status="incomplete")}),
            stream(
                {
                    "type": "response.completed",
                    "response": completed(output=[{"type": "function_call"}]),
                }
            ),
            b"data: [DONE]\n\n",
            b"data: invalid\n\n",
            b"x" * (MAX_STREAM_BYTES + 1),
        ]
        for data in cases:
            with (
                self.subTest(data=data[:60]),
                patch.object(self.connection, "_open", return_value=self.response_from(data)),
            ):
                with self.assertRaises((CodexError, ValueError)):
                    self.connection._response(
                        {}, "synthetic", time.monotonic() + 5, self.connection._generation
                    )

    def test_completed_rejects_non_json_refusals_non_assistant_and_invalid_usage(self):
        for output in (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "not json"}],
                }
            ],
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "No"}],
                }
            ],
            [{"type": "message", "role": "user", "content": []}],
        ):
            with self.subTest(output=output), self.assertRaises((CodexError, ValueError)):
                CodexConnection._completed(completed(output=output))
        result = CodexConnection._completed(
            completed(usage={"cost": 5, "input_tokens": True, "output_tokens": -1})
        )
        self.assertEqual(result["usage"], {})

    def test_close_preserves_login_but_invalidates_pending_login(self):
        self.login()
        saved = self.connection._path.read_bytes()
        self.connection.begin_login(CALLBACK)
        self.connection.close()
        self.assertFalse(self.connection.public_state()["pending"])
        self.assertFalse(self.connection.public_state()["connected"])
        self.assertEqual(saved, self.connection._path.read_bytes())
        with self.assertRaises(CodexError):
            self.connection.begin_login(CALLBACK)

    def test_unsafe_private_files_are_not_followed_or_read(self):
        self.connection.directory.mkdir(parents=True)
        self.connection._path.write_text("not json")
        self.connection.start()
        self.assertIn("could not be read safely", self.connection.public_state()["error"])
        self.connection._path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.connection._path)
            self.connection.start()
            self.assertFalse(self.connection.public_state()["connected"])
            self.connection._path.unlink()
        if os.name != "nt":
            target = self.root / "unrelated-private-file"
            target.write_text("DO NOT READ")
            self.connection._path.symlink_to(target)
            self.connection.start()
            self.assertFalse(self.connection.public_state()["connected"])
            with self.assertRaises(CodexError):
                self.connection.begin_login(CALLBACK)
            self.assertEqual(target.read_text(), "DO NOT READ")

    def test_network_errors_are_fixed_safe_messages_and_requests_are_not_retried(self):
        self.login()
        with patch.object(
            self.connection, "_json", side_effect=OSError("private-token-secret")
        ) as network:
            with self.assertRaises(CodexError) as caught:
                self.connection.request(PAYLOAD, 5)
        self.assertEqual(network.call_count, 1)
        self.assertNotIn("private-token-secret", str(caught.exception))
        self.assertNotIn("private-token-secret", json.dumps(self.connection.public_state()))

    def test_unknown_and_duplicated_jwks_keys_are_rejected_after_bounded_refresh(self):
        self.connection.begin_login(CALLBACK)
        token = self.token()
        for keys in ([{**self.jwk, "kid": "unknown"}], [self.jwk, self.jwk], []):
            self.connection._jwks = {}
            self.connection._discovery = DISCOVERY.copy()
            with (
                self.subTest(keys=len(keys)),
                patch.object(self.connection, "_json", return_value={"keys": keys}) as network,
            ):
                with self.assertRaises(CodexError):
                    self.connection._identity(
                        token,
                        CLIENT,
                        self.connection._pending["nonce"],
                        time.monotonic() + 5,
                        self.connection._generation,
                    )
                self.assertEqual(network.call_count, 2)

    def test_deadline_limits_detached_response_socket_and_incomplete_stream(self):
        sock = Mock()
        response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)))
        with patch("spiritboard.codex_summary.time.monotonic", return_value=20):
            self.connection._read_timeout(Mock(sock=None), response, 22)
            sock.settimeout.assert_called_once_with(2)
            with self.assertRaises(CodexError):
                self.connection._read_timeout(Mock(sock=None), response, 19)
        data = stream({"type": "response.created"})
        with patch.object(self.connection, "_open", return_value=self.response_from(data)):
            with self.assertRaisesRegex(CodexError, "timed out"):
                self.connection._response(
                    {}, "synthetic", time.monotonic() - 1, self.connection._generation
                )

    def test_credential_commit_failure_does_not_activate_unpersisted_login(self):
        self.connection.begin_login(CALLBACK)
        self.pending_nonce = self.connection._pending["nonce"]
        state = self.connection._pending["state"]
        with (
            patch.object(self.connection, "_json", side_effect=self.network),
            patch.object(
                self.connection, "_save_record", side_effect=OSError("synthetic-path-secret")
            ),
        ):
            with self.assertRaises(CodexError):
                self.connection.complete(state, "synthetic-code", CLIENT)
        self.assertFalse(self.connection.public_state()["connected"])
        self.assertNotIn("synthetic-path-secret", json.dumps(self.connection.public_state()))

    def test_http_errors_redirects_and_excessive_json_are_rejected_without_following(self):
        for status in (302, 401, 403, 429, 500):
            connection = Mock(sock=None)
            connection.getresponse.return_value.status = status
            with (
                self.subTest(status=status),
                patch("http.client.HTTPSConnection", return_value=connection),
            ):
                with self.assertRaises(CodexError):
                    self.connection._json(
                        "GET",
                        RESOURCE + "/models",
                        None,
                        time.monotonic() + 5,
                        self.connection._generation,
                    )
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called()
        data = self.response_from(b"x" * (256 * 1024 + 1))
        with patch.object(self.connection, "_open", return_value=data):
            with self.assertRaisesRegex(CodexError, "safety limit"):
                self.connection._json(
                    "GET",
                    RESOURCE + "/models",
                    None,
                    time.monotonic() + 5,
                    self.connection._generation,
                )

    def test_invalid_timeout_and_prompt_roles_fail_without_network(self):
        self.login()
        with patch.object(self.connection, "_json") as network:
            for timeout in (0, -1, float("nan"), 301, True):
                with self.subTest(timeout=timeout), self.assertRaises(CodexError):
                    self.connection.request(PAYLOAD, timeout)
            with self.assertRaises(CodexError):
                self.connection.request({"messages": [{"role": "tool", "content": "run"}]}, 5)
        network.assert_not_called()
