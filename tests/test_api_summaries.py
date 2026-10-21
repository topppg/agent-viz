from __future__ import annotations

import json
import threading
import time

import pytest

from spiritboard import api_summaries, credentials
from spiritboard.api_summaries import MODELS, APIKeyConnection
from spiritboard.credentials import CredentialStore
from spiritboard.openrouter_auth import ProviderAuthError

KEYS = {"openai": "sk-synthetic-openai-key-123456", "anthropic": "sk-ant-synthetic-key-123456"}
PAYLOAD = {
    "model": "untrusted-model",
    "tools": [{"type": "external"}],
    "messages": [
        {"role": "system", "content": "Return only a JSON object."},
        {"role": "user", "content": "Synthetic task."},
    ],
}


def document(name: str) -> dict:
    if name == "openai":
        return {
            "id": "resp_synthetic",
            "status": "completed",
            "error": None,
            "output": [
                {"type": "reasoning"},
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": '{"r1":'},
                        {"type": "output_text", "text": '"A task."}'},
                    ],
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 12,
                "total_tokens": 22,
                "cost": 999,
                "private": "ignored",
            },
        }
    return {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "not returned"},
            {"type": "text", "text": '{"r1":'},
            {"type": "text", "text": '"A task."}'},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 12, "cost": 999},
    }


class Response:
    status = 200

    def __init__(self, value: dict):
        self.raw = json.dumps(value).encode()

    def getheader(self, name: str) -> str | None:
        return None

    def read1(self, size: int) -> bytes:
        result, self.raw = self.raw[:size], self.raw[size:]
        return result


class Transport:
    sock = None

    def __init__(self, host: str, *, timeout: float):
        self.host, self.timeout = host, timeout
        self.response = Response(document("openai" if host == "api.openai.com" else "anthropic"))
        self.sent = []
        self.closed = False

    def connect(self) -> None:
        pass

    def request(self, method: str, path: str, *, body: bytes, headers: dict) -> None:
        self.sent.append((method, path, json.loads(body), headers))

    def getresponse(self) -> Response:
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def transports(monkeypatch):
    made = []

    def factory(host: str, *, timeout: float) -> Transport:
        item = Transport(host, timeout=timeout)
        made.append(item)
        return item

    monkeypatch.setattr(api_summaries.http.client, "HTTPSConnection", factory)
    return made


@pytest.mark.parametrize("name", KEYS)
def test_secure_save_restore_disconnect_no_network(tmp_path, native_credentials, transports, name):
    connection = APIKeyConnection(tmp_path, name)
    assert connection.public_state()["connected"] is False
    connection.set_key(KEYS[name])
    assert APIKeyConnection(tmp_path, name).public_state()["connected"] is True
    assert connection.key() == KEYS[name]
    assert KEYS[name] not in json.dumps(connection.public_state())
    assert all(KEYS[name] not in p.read_text() for p in tmp_path.rglob("*.json"))
    assert not transports
    connection.disconnect()
    assert connection.key() is None
    assert CredentialStore(tmp_path).load(name) is None
    assert not native_credentials.values


@pytest.mark.parametrize("name", KEYS)
def test_request_uses_fixed_provider_api_and_safe_normalized_response(tmp_path, transports, name):
    connection = APIKeyConnection(tmp_path, name)
    connection.set_key(KEYS[name])
    result = connection.request(PAYLOAD, 1)
    assert result["choices"][0]["message"]["content"] == '{"r1":"A task."}'
    assert result["usage"]["input_tokens"] == 10
    assert "cost" not in result["usage"]
    assert "private" not in result["usage"]
    transport = transports[0]
    method, path, body, headers = transport.sent[0]
    assert method == "POST"
    assert body["model"] == MODELS[name]
    assert not body.get("tools")
    assert transport.closed
    if name == "openai":
        assert (transport.host, path) == ("api.openai.com", "/v1/responses")
        assert headers["Authorization"] == "Bearer " + KEYS[name]
        assert body["store"] is False
        assert body["reasoning"] == {"effort": "medium"}
        assert body["instructions"] == PAYLOAD["messages"][0]["content"]
        assert body["input"] == [PAYLOAD["messages"][1]]
    else:
        assert (transport.host, path) == ("api.anthropic.com", "/v1/messages")
        assert headers["x-api-key"] == KEYS[name]
        assert headers["anthropic-version"] == "2023-06-01"
        assert body["system"] == PAYLOAD["messages"][0]["content"]
        assert body["messages"] == [PAYLOAD["messages"][1]]
        assert 600 <= body["max_tokens"] <= 8192


@pytest.mark.parametrize("name", KEYS)
@pytest.mark.parametrize(
    "bad", ["", "sk-short", "sk-" + "x" * 4096, "sk-abc\r\nx: evil", "sk-☃" * 12, None]
)
def test_reject_invalid_header_keys(tmp_path, transports, name, bad):
    connection = APIKeyConnection(tmp_path, name)
    with pytest.raises(ProviderAuthError):
        connection.set_key(bad)
    assert not transports
    assert connection.public_state()["connected"] is False


def test_provider_namespaces_and_legacy_files_are_not_shared(tmp_path, transports):
    auth = tmp_path / "auth"
    auth.mkdir(mode=0o700)
    (auth / "openai.json").write_text(json.dumps({"key": KEYS["openai"]}))
    one = APIKeyConnection(tmp_path, "openai")
    assert one.key() is None
    one.set_key(KEYS["openai"])
    two = APIKeyConnection(tmp_path, "anthropic")
    assert two.key() is None
    two.set_key(KEYS["anthropic"])
    one.disconnect()
    assert APIKeyConnection(tmp_path, "anthropic").key() == KEYS["anthropic"]


@pytest.mark.parametrize("key", [KEYS["anthropic"], "sk-or-synthetic-openrouter-123456"])
def test_openai_does_not_accept_other_provider_keys(tmp_path, transports, key):
    connection = APIKeyConnection(tmp_path, "openai")
    with pytest.raises(ProviderAuthError):
        connection.set_key(key)
    assert connection.key() is None
    assert not transports


@pytest.mark.parametrize("name", ["codex", "openrouter", "http://untrusted.invalid", [], None])
def test_invalid_provider_name_fails_safely(tmp_path, name):
    with pytest.raises(ProviderAuthError):
        APIKeyConnection(tmp_path, name)


@pytest.mark.parametrize("operation", ["load", "save", "delete"])
def test_storage_failure_is_safe_clears_key_and_explicit_retry_recovers(
    tmp_path, monkeypatch, operation
):
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    original = getattr(CredentialStore, operation)

    def broken(*args):
        raise credentials.CredentialStoreError("synthetic-private-secret")

    monkeypatch.setattr(CredentialStore, operation, broken)
    if operation == "load":
        connection = APIKeyConnection(tmp_path, "openai")
    else:
        with pytest.raises(ProviderAuthError) as error:
            connection.set_key(KEYS["openai"]) if operation == "save" else connection.disconnect()
        assert "synthetic-private" not in str(error.value)
    assert connection.key() is None
    state = connection.public_state()
    assert state["storage_error"] is True
    assert state["connected"] is False
    assert "synthetic-private" not in json.dumps(state)
    monkeypatch.setattr(CredentialStore, operation, original)
    connection.set_key(KEYS["openai"])
    assert connection.public_state()["storage_error"] is False
    assert connection.public_state()["connected"] is True


@pytest.mark.parametrize("name", KEYS)
@pytest.mark.parametrize(
    "kind",
    ["truncated", "tool", "error", "bad_json", "bad_id", "oversize", "redirect", "unauthorized"],
)
def test_invalid_or_unsuccessful_responses_are_safe_errors(tmp_path, monkeypatch, name, kind):
    data = document(name)
    if kind == "truncated":
        data["status" if name == "openai" else "stop_reason"] = (
            "incomplete" if name == "openai" else "max_tokens"
        )
    elif kind == "tool":
        data["output" if name == "openai" else "content"].append({"type": "tool_use"})
    elif kind == "error":
        data["error"] = {"message": "synthetic-private-secret"}
    elif kind == "bad_id":
        data["id"] = "bad\r\nidentifier"
    elif kind == "bad_json":
        data = {"id": "x"}
    made = []

    def factory(host, *, timeout):
        item = Transport(host, timeout=timeout)
        item.response = Response(data)
        if kind == "oversize":
            item.response.raw = b" " * (api_summaries.MAX_BYTES + 1)
        if kind in {"redirect", "unauthorized"}:
            item.response.status = 302 if kind == "redirect" else 401
        made.append(item)
        return item

    monkeypatch.setattr(api_summaries.http.client, "HTTPSConnection", factory)
    connection = APIKeyConnection(tmp_path, name)
    connection.set_key(KEYS[name])
    with pytest.raises(ProviderAuthError) as error:
        connection.request(PAYLOAD, 1)
    assert "synthetic-private" not in str(error.value)
    assert KEYS[name] not in json.dumps(connection.public_state())
    assert len(made) == 1
    assert made[0].closed


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 301])
def test_invalid_timeout_and_input_never_send(tmp_path, transports, timeout):
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    with pytest.raises(ProviderAuthError):
        connection.request(PAYLOAD, timeout)
    assert not transports


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x" * 40000}]},
    ],
)
def test_invalid_input_never_sends(tmp_path, transports, payload):
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    with pytest.raises(ProviderAuthError):
        connection.request(payload, 1)
    assert not transports


@pytest.mark.parametrize("action", ["disconnect", "close", "timeout"])
def test_cancelled_slow_connect_cannot_send_or_restore_credentials(tmp_path, monkeypatch, action):
    entered, release = threading.Event(), threading.Event()
    transport = Transport("api.openai.com", timeout=1)

    def connect():
        entered.set()
        release.wait(2)

    transport.connect = connect
    monkeypatch.setattr(api_summaries.http.client, "HTTPSConnection", lambda *a, **k: transport)
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    result = []

    def run():
        try:
            connection.request(PAYLOAD, 0.1 if action == "timeout" else 1)
        except ProviderAuthError as error:
            result.append(str(error))

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(1)
    if action != "timeout":
        getattr(connection, action)()
    worker.join(0.5)
    release.set()
    worker.join(1)
    time.sleep(0.02)
    assert result
    assert not transport.sent
    assert transport.closed
    if action != "timeout":
        assert connection.key() is None


def test_close_blocks_new_keys_and_requests(tmp_path, transports):
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    connection.close()
    assert connection.public_state()["available"] is False
    with pytest.raises(ProviderAuthError):
        connection.set_key(KEYS["openai"])
    with pytest.raises(ProviderAuthError):
        connection.request(PAYLOAD, 1)
    assert not transports


@pytest.mark.parametrize("name", KEYS)
def test_malformed_saved_credentials_never_connect(tmp_path, name):
    CredentialStore(tmp_path).save(name, '{"version":true,"key":"synthetic-private-secret"}')
    connection = APIKeyConnection(tmp_path, name)
    assert connection.public_state()["storage_error"] is True
    assert connection.key() is None
    assert "synthetic-private" not in json.dumps(connection.public_state())


@pytest.mark.parametrize("length", ["-1", "garbage", str(api_summaries.MAX_BYTES + 1)])
def test_bad_content_length_rejected_without_reading(tmp_path, monkeypatch, length):
    transport = Transport("api.openai.com", timeout=1)
    transport.response.getheader = lambda name: length

    def forbidden_read(size):
        pytest.fail("Oversized or invalid content length must not be read")

    transport.response.read1 = forbidden_read
    monkeypatch.setattr(api_summaries.http.client, "HTTPSConnection", lambda *a, **k: transport)
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    with pytest.raises(ProviderAuthError):
        connection.request(PAYLOAD, 1)
    assert transport.closed


def test_usage_drops_bool_negative_cost_and_untrusted_fields():
    value = document("openai")
    value["usage"] = {
        "input_tokens": True,
        "output_tokens": -1,
        "total_tokens": 10**20,
        "cost": 1.25,
        "anything": "not usage",
    }
    assert api_summaries._normalized("openai", value)["usage"] == {}


def test_output_must_be_complete_json_object():
    for output in ('["not an object"]', '{"unfinished":', ""):
        value = document("anthropic")
        value["content"] = [{"type": "text", "text": output}]
        with pytest.raises(ValueError):
            api_summaries._normalized("anthropic", value)


def test_repeated_timeouts_do_not_accumulate_dns_workers(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    made = []

    def factory(host, *, timeout):
        transport = Transport(host, timeout=timeout)

        def connect():
            entered.set()
            release.wait(2)

        transport.connect = connect
        made.append(transport)
        return transport

    monkeypatch.setattr(api_summaries.http.client, "HTTPSConnection", factory)
    connection = APIKeyConnection(tmp_path, "openai")
    connection.set_key(KEYS["openai"])
    try:
        with pytest.raises(ProviderAuthError):
            connection.request(PAYLOAD, 0.01)
        assert entered.is_set()
        for _ in range(10):
            with pytest.raises(ProviderAuthError):
                connection.request(PAYLOAD, 0.01)
        assert len(made) == 1
    finally:
        connection.close()
        release.set()
