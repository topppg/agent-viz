"""Explicit API-key summary connections, separate from subscription sign-in."""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import threading
import time
from pathlib import Path

from .credentials import CredentialStore, CredentialStoreError
from .openrouter_auth import ProviderAuthError

MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}
MAX_BYTES = 256 * 1024
_ORIGINS = {"openai": "api.openai.com", "anthropic": "api.anthropic.com"}
_PATHS = {"openai": "/v1/responses", "anthropic": "/v1/messages"}
_STORAGE_ERROR = "Secure credential storage is unavailable. Unlock it and try again."
_REQUEST_ERROR = "The API summary request failed; usage may still have been charged."
_INPUT_ERROR = "The API summary input or timeout is invalid."
_CLOSED_ERROR = "This API connection has been closed."


def _valid_key(name: str, value: object) -> bool:
    prefix = "sk-ant-" if name == "anthropic" else "sk-"
    return (
        isinstance(value, str)
        and re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{14,500}", value) is not None
        and (name != "openai" or not value.startswith(("sk-ant-", "sk-or-")))
    )


def _body(name: str, payload: dict) -> dict:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise ProviderAuthError(_INPUT_ERROR)
    instructions, inputs = [], []
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in {"system", "developer", "user"}
            or not isinstance(message.get("content"), str)
            or len(message["content"]) > 32768
        ):
            raise ProviderAuthError(_INPUT_ERROR)
        if message["role"] in {"system", "developer"}:
            instructions.append(message["content"])
        else:
            inputs.append({"role": "user", "content": message["content"]})
    if not inputs:
        raise ProviderAuthError(_INPUT_ERROR)
    if name == "openai":
        return {
            "model": MODELS[name],
            "instructions": "\n\n".join(instructions),
            "input": inputs,
            "reasoning": {"effort": "medium"},
            "store": False,
            "stream": False,
            "tools": [],
            "tool_choice": "none",
            "max_output_tokens": 4096,
            "text": {"format": {"type": "json_object"}},
        }
    return {
        "model": MODELS[name],
        "system": "\n\n".join(instructions),
        "messages": inputs,
        "max_tokens": 4096,
        "stream": False,
    }


def _normalized(name: str, document: object) -> dict:
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("id"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", document["id"]) is None
        or document.get("error") is not None
    ):
        raise ValueError
    text = []
    if name == "openai":
        output = document.get("output")
        if document.get("status") != "completed" or not isinstance(output, list):
            raise ValueError
        for item in output:
            if not isinstance(item, dict):
                raise ValueError
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
                or not isinstance(item.get("content"), list)
            ):
                raise ValueError
            for part in item["content"]:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "output_text"
                    or not isinstance(part.get("text"), str)
                ):
                    raise ValueError
                text.append(part["text"])
    else:
        content = document.get("content")
        if (
            document.get("type") != "message"
            or document.get("role") != "assistant"
            or document.get("stop_reason") != "end_turn"
            or not isinstance(content, list)
        ):
            raise ValueError
        for part in content:
            if not isinstance(part, dict):
                raise ValueError
            if part.get("type") in {"thinking", "redacted_thinking"}:
                continue
            if part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise ValueError
            text.append(part["text"])
    result = "".join(text)
    if not result or len(result) > MAX_BYTES or not isinstance(json.loads(result), dict):
        raise ValueError
    raw_usage = document.get("usage")
    usage = {}
    if isinstance(raw_usage, dict):
        for field in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = raw_usage.get(field)
            if type(value) is int and 0 <= value <= 10**12:
                usage[field] = value
    return {"id": document["id"], "choices": [{"message": {"content": result}}], "usage": usage}


class APIKeyConnection:
    """One user's explicit API key, never borrowed from another app or provider."""

    def __init__(self, directory: Path, name: str) -> None:
        if not isinstance(name, str) or name not in MODELS:
            raise ProviderAuthError("Choose an available API summary provider.")
        self.name = name
        self._store = CredentialStore(directory)
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._key: str | None = None
        self._error: str | None = None
        self._storage_error = False
        self._connections: dict[http.client.HTTPSConnection, threading.Event] = {}
        try:
            raw = self._store.load(name)
            if raw is not None:
                if len(raw) > 2048:
                    raise ValueError
                record = json.loads(raw)
                if (
                    not isinstance(record, dict)
                    or type(record.get("version")) is not int
                    or record["version"] != 1
                    or not _valid_key(name, record.get("key"))
                ):
                    raise ValueError
                self._key = record["key"]
        except (CredentialStoreError, ValueError, UnicodeError, RecursionError):
            self._storage_error = True
            self._error = _STORAGE_ERROR

    def key(self) -> str | None:
        with self._lock:
            return self._key if not self._closed and not self._storage_error else None

    def public_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "available": not self._closed,
                "connected": self.key() is not None,
                "storage_error": self._storage_error,
                "error": self._error,
                "model": MODELS[self.name],
                **({"effort": "medium"} if self.name == "openai" else {}),
            }

    @staticmethod
    def _cancel(connection: http.client.HTTPSConnection, finished: threading.Event) -> None:
        finished.set()
        connection.auto_open = 0
        try:
            if connection.sock is not None:
                connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _invalidate(self) -> None:
        self._generation += 1
        self._key = None
        for connection, finished in tuple(self._connections.items()):
            self._cancel(connection, finished)

    def set_key(self, value: str) -> None:
        if not _valid_key(self.name, value):
            raise ProviderAuthError("Enter a valid API key for this provider.")
        with self._lock:
            if self._closed:
                raise ProviderAuthError(_CLOSED_ERROR)
            self._invalidate()
            try:
                self._store.save(self.name, json.dumps({"version": 1, "key": value}))
            except CredentialStoreError:
                self._storage_error = True
                self._error = _STORAGE_ERROR
                raise ProviderAuthError(_STORAGE_ERROR) from None
            self._key = value
            self._storage_error = False
            self._error = None

    def disconnect(self) -> None:
        with self._lock:
            self._invalidate()
            try:
                self._store.delete(self.name)
            except CredentialStoreError:
                self._storage_error = True
                self._error = _STORAGE_ERROR
                raise ProviderAuthError(_STORAGE_ERROR) from None
            self._storage_error = False
            self._error = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._invalidate()

    def request(self, payload: dict, timeout: float) -> dict:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise ProviderAuthError(_INPUT_ERROR)
        try:
            body = json.dumps(_body(self.name, payload), allow_nan=False).encode("utf-8")
            if len(body) > MAX_BYTES:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderAuthError(_INPUT_ERROR) from None
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._closed:
                raise ProviderAuthError(_CLOSED_ERROR)
            key, generation = self.key(), self._generation
            if key is None:
                raise ProviderAuthError("Connect this API provider in settings first.")
            if self._connections:
                raise ProviderAuthError("An API summary request is already running. Try again.")
            try:
                connection = http.client.HTTPSConnection(_ORIGINS[self.name], timeout=timeout)
            except (OSError, ValueError, http.client.HTTPException):
                raise ProviderAuthError(_REQUEST_ERROR) from None
            finished = threading.Event()
            self._connections[connection] = finished
        result: list[dict] = []

        def perform() -> None:
            try:
                # Resolve/connect before the generation check so cancelled DNS cannot send later.
                connection.connect()
                with self._lock:
                    if finished.is_set() or self._closed or generation != self._generation:
                        return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if self.name == "openai":
                    headers["Authorization"] = "Bearer " + key
                else:
                    headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
                connection.request("POST", _PATHS[self.name], body=body, headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    return
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                    return
                raw = bytearray()
                while len(raw) <= MAX_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or finished.is_set():
                        return
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(8192, MAX_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    return
                normalized = _normalized(self.name, json.loads(raw))
                with self._lock:
                    if (
                        not finished.is_set()
                        and generation == self._generation
                        and not self._closed
                    ):
                        result.append(normalized)
            except (
                OSError,
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
                http.client.HTTPException,
            ):
                pass
            finally:
                self._cancel(connection, finished)
                with self._lock:
                    self._connections.pop(connection, None)

        threading.Thread(
            target=perform, name=f"spiritboard-{self.name}-summary", daemon=True
        ).start()
        completed = finished.wait(max(0, deadline - time.monotonic()))
        if not completed:
            self._cancel(connection, finished)
        with self._lock:
            if not result or generation != self._generation or self._closed:
                if generation == self._generation and not self._closed:
                    self._error = _REQUEST_ERROR
                raise ProviderAuthError(_REQUEST_ERROR)
            self._error = None
            return result[0]
