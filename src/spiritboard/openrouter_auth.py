"""Explicit OpenRouter connection with private, local credential storage."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from spiritboard.launcher import LaunchError, _private_directory, _regular_descriptor

_MAX_DOCUMENT = 8192
_TIMEOUT = 15.0
_EXPIRY = 600
_CALLBACK_PATH = "/api/summaries/openrouter/callback"
_STORAGE_ERROR = "OpenRouter credentials could not be stored or read safely."
_EXCHANGE_ERROR = "OpenRouter sign-in failed. Please start sign-in again."
_PENDING_ERROR = "OpenRouter sign-in expired or was replaced. Please start sign-in again."
_KEY_ERROR = "Enter a valid OpenRouter API key."


class ProviderAuthError(ValueError):
    """An authentication failure with a fixed, safe public message."""


def _valid_key(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"sk-or-[A-Za-z0-9_-]{14,250}", value) is not None
    )


def _callback(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("localhost", "127.0.0.1")
            or port is None
            or not 1 <= port <= 65535
            or parsed.netloc != f"{parsed.hostname}:{port}"
            or parsed.path != _CALLBACK_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ProviderAuthError("OpenRouter sign-in needs a local dashboard callback.") from None
    return value


def _exchange(code: str, verifier: str) -> str:
    try:
        connection = http.client.HTTPSConnection("openrouter.ai", timeout=_TIMEOUT)
    except (OSError, ValueError, http.client.HTTPException):
        raise ProviderAuthError(_EXCHANGE_ERROR) from None
    finished = threading.Event()
    result: list[str] = []

    def request() -> None:
        deadline = time.monotonic() + _TIMEOUT
        try:
            connection.request(
                "POST",
                "/api/v1/auth/keys",
                body=json.dumps(
                    {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
                ),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                return
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_DOCUMENT):
                return
            raw = bytearray()
            while len(raw) <= _MAX_DOCUMENT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(4096, _MAX_DOCUMENT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_DOCUMENT:
                return
            document = json.loads(raw)
            key = document.get("key") if isinstance(document, dict) else None
            if _valid_key(key):
                result.append(key)
        except (OSError, ValueError, UnicodeError, RecursionError, http.client.HTTPException):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass
            finally:
                finished.set()

    threading.Thread(target=request, name="spiritboard-openrouter-sign-in", daemon=True).start()
    if not finished.wait(_TIMEOUT):
        # Closing the transport also interrupts a slow response-header read in the worker.
        try:
            if connection.sock is not None:
                connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
        raise ProviderAuthError(_EXCHANGE_ERROR)
    if not result:
        raise ProviderAuthError(_EXCHANGE_ERROR)
    return result[0]


class OpenRouterConnection:
    """One explicit connection; keys and PKCE verifiers never enter public state."""

    def __init__(self, directory: Path):
        self._directory = Path(directory) / "auth"
        self._path = self._directory / "openrouter.json"
        self._lock = threading.RLock()
        self._generation = 0
        self._pending: tuple[str, str, str, float, int] | None = None
        self._key: str | None = None
        self._error: str | None = None
        try:
            self._prepare()
            if self._path.exists() or self._path.is_symlink():
                descriptor = _regular_descriptor(self._path)
                try:
                    raw = os.read(descriptor, _MAX_DOCUMENT + 1)
                finally:
                    os.close(descriptor)
                if len(raw) > _MAX_DOCUMENT:
                    raise ValueError
                saved = json.loads(raw)
                if not isinstance(saved, dict) or saved.get("version") != 1:
                    raise ValueError
                key = saved.get("key")
                if not _valid_key(key):
                    raise ValueError
                self._key = key
        except (LaunchError, OSError, ValueError, UnicodeError, RecursionError):
            self._error = _STORAGE_ERROR

    def _prepare(self) -> None:
        _private_directory(self._directory.parent)
        _private_directory(self._directory)

    def _save(self, key: str) -> None:
        temporary: str | None = None
        try:
            self._prepare()
            if self._path.exists() or self._path.is_symlink():
                os.close(_regular_descriptor(self._path))
            descriptor, temporary = tempfile.mkstemp(prefix=".openrouter-", dir=self._directory)
            with os.fdopen(descriptor, "wb") as output:
                output.write(json.dumps({"version": 1, "key": key}).encode("ascii"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except (LaunchError, OSError):
            raise ProviderAuthError(_STORAGE_ERROR) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def begin(self, callback_url: str) -> dict[str, object]:
        callback_url = _callback(callback_url)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        with self._lock:
            self._generation += 1
            self._pending = (
                state,
                verifier,
                callback_url,
                time.monotonic() + _EXPIRY,
                self._generation,
            )
            self._error = None
        return {
            "auth_url": "https://openrouter.ai/auth?"
            + urlencode(
                {
                    "callback_url": callback_url,
                    "state": state,
                    "code_challenge": challenge.decode("ascii").rstrip("="),
                    "code_challenge_method": "S256",
                    "key_label": "Spiritboard summaries",
                }
            ),
            "expires_in": _EXPIRY,
        }

    def complete(self, state: str, code: str) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or time.monotonic() >= pending[3]:
                self._pending = None
                self._error = _PENDING_ERROR
                raise ProviderAuthError(_PENDING_ERROR)
            if (
                not isinstance(state, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", state) is None
                or not secrets.compare_digest(state, pending[0])
            ):
                raise ProviderAuthError(_PENDING_ERROR)
            self._pending = None
            if not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", code) is None:
                self._error = _PENDING_ERROR
                raise ProviderAuthError(_PENDING_ERROR)
            generation = pending[4]
        try:
            key = _exchange(code, pending[1])
            with self._lock:
                if generation != self._generation:
                    raise ProviderAuthError(_PENDING_ERROR)
                self._save(key)
                self._key = key
                self._error = None
        except ProviderAuthError as error:
            with self._lock:
                if generation == self._generation:
                    self._error = str(error)
            raise

    def set_key(self, key: str) -> None:
        if not _valid_key(key):
            raise ProviderAuthError(_KEY_ERROR)
        with self._lock:
            self._generation += 1
            self._pending = None
            self._save(key)
            self._key = key
            self._error = None

    def key(self) -> str | None:
        with self._lock:
            return self._key

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._pending = None
            self._key = None
            self._error = None
            try:
                self._prepare()
                if self._path.exists() or self._path.is_symlink():
                    os.close(_regular_descriptor(self._path))
                    self._path.unlink()
            except (LaunchError, OSError):
                self._error = _STORAGE_ERROR
                raise ProviderAuthError(_STORAGE_ERROR) from None

    def close(self) -> None:
        """Cancel pending authorization without removing the saved connection."""
        with self._lock:
            self._generation += 1
            self._pending = None

    def public_state(self) -> dict[str, object]:
        with self._lock:
            if self._pending is not None and time.monotonic() >= self._pending[3]:
                self._pending = None
                self._error = _PENDING_ERROR
            return {
                "available": True,
                "connected": self._key is not None,
                "pending": self._pending is not None,
                "error": self._error,
            }
