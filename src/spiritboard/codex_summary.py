"""Explicit, private ChatGPT-plan sign-in and tool-free summary requests."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import secrets
import socket
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import jwt

MODEL = "gpt-6-luna"
EFFORT = "medium"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/api/summaries/codex/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
MAX_BYTES = 256 * 1024
MAX_STREAM_BYTES = 1024 * 1024
LOGIN_SECONDS = 600


class CodexError(ValueError):
    """An intentionally safe error suitable for the local settings panel."""


def _text(value: object, limit: int = 16384) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise CodexError("ChatGPT request timed out; usage may still have been charged.")
    return min(left, 30.0)


class CodexConnection:
    """Own one app-specific registration, never a discovered Codex account."""

    def __init__(self, directory: Path):
        self.directory = directory / "auth" / "codex"
        self._path = self.directory / "connection.json"
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._connections: set[http.client.HTTPSConnection] = set()
        self._generation = 0
        self._record: dict = {}
        self._pending: dict | None = None
        self._error: str | None = None
        self._closed = False
        self._validated = False
        self._discovery: dict = {}
        self._jwks: dict = {}

    def start(self) -> None:
        """Restore only this app's previously approved credentials, without network I/O."""
        with self._lock:
            self._closed = False
            try:
                self._record = self._read_record()
                self._validated = False
                self._error = None
            except (OSError, ValueError, TypeError):
                self._record = {}
                self._error = (
                    "Saved ChatGPT sign-in could not be read safely. Reconnect in settings."
                )

    def public_state(self) -> dict:
        with self._lock:
            pending = self._pending is not None and self._pending["expires"] > time.monotonic()
            if self._pending is not None and not pending:
                self._pending = None
                self._error = "ChatGPT sign-in expired. Start sign-in again."
            return {
                "available": True,
                "connected": bool(self._record.get("access_token")) and not self._closed,
                "pending": pending,
                "error": self._error,
                "model": MODEL,
                "effort": EFFORT,
            }

    def begin_login(self, callback_url: str) -> dict:
        parsed = urlsplit(callback_url)
        try:
            valid_port = parsed.port is not None and 0 < parsed.port <= 65535
        except ValueError:
            valid_port = False
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or not valid_port
            or parsed.path != CALLBACK_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise CodexError("ChatGPT sign-in requires the local dashboard callback.")
        with self._lock:
            if self._closed:
                raise CodexError("ChatGPT connection is closed.")
            self._generation += 1
            generation = self._generation
            host = self._record.get("host_id") or "urn:uuid:" + str(uuid.uuid4())
            self._record["host_id"] = host
            self._save_record()
            verifier = secrets.token_urlsafe(48)
            pending = {
                "generation": generation,
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "verifier": verifier,
                "redirect_uri": callback_url,
                "client_id": self._record.get("client_id"),
                "subject": self._record.get("subject"),
                "expires": time.monotonic() + LOGIN_SECONDS,
            }
            self._pending = pending
            self._error = None
            params = {
                "client_id": pending["client_id"] or "dynamic_agent_client",
                "ext_agent_host_id": host,
                "response_type": "code",
                "redirect_uri": callback_url,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": pending["state"],
                "nonce": pending["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode("ascii")).digest()
                )
                .rstrip(b"=")
                .decode("ascii"),
            }
            if not pending["client_id"]:
                params["agent_name_hint"] = "Spiritboard"
            return {"auth_url": ISSUER + "/api/accounts/authorize?" + urlencode(params)}

    def complete(
        self,
        state: str,
        code: str | None,
        client_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._operation:
            with self._lock:
                pending = self._pending
                if pending is not None and pending["expires"] <= time.monotonic():
                    self._pending = None
                if (
                    pending is None
                    or not _text(state, 256)
                    or not secrets.compare_digest(state, pending["state"])
                    or pending["expires"] <= time.monotonic()
                    or pending["generation"] != self._generation
                    or self._closed
                ):
                    self._error = "ChatGPT sign-in could not be verified. Start sign-in again."
                    raise CodexError(self._error)
                self._pending = None
                generation = self._generation
            try:
                if error is not None or not _text(code, 4096):
                    raise CodexError("ChatGPT sign-in was not completed. Start sign-in again.")
                chosen = pending["client_id"] or client_id
                if (
                    not _text(chosen, 256)
                    or not chosen.startswith("oaiapp_")
                    or (
                        pending["client_id"] is not None
                        and client_id is not None
                        and client_id != pending["client_id"]
                    )
                ):
                    raise CodexError("ChatGPT registration could not be verified.")
                with self._lock:
                    self._check_generation(generation)
                    if "client_id" not in self._record:
                        registration = {**self._record, "client_id": chosen}
                        self._save_record(registration)
                        self._record = registration
                deadline = time.monotonic() + 30
                discovery = self._discover(deadline, generation)
                tokens = self._json(
                    "POST",
                    discovery["token_endpoint"],
                    {
                        "grant_type": "authorization_code",
                        "client_id": chosen,
                        "code": code,
                        "code_verifier": pending["verifier"],
                        "redirect_uri": pending["redirect_uri"],
                        "resource": RESOURCE,
                    },
                    deadline,
                    generation,
                )
                record = self._tokens(tokens, chosen)
                claims = self._identity(
                    record["id_token"], chosen, pending["nonce"], deadline, generation
                )
                if pending["subject"] is not None and claims["sub"] != pending["subject"]:
                    raise CodexError("ChatGPT sign-in did not match the saved account.")
                with self._lock:
                    self._check_generation(generation)
                    record.update(
                        host_id=self._record["host_id"],
                        subject=claims["sub"],
                        nonce=pending["nonce"],
                    )
                    self._save_record(record)
                    self._record = record
                    self._validated = True
                    self._error = None
            except (
                OSError,
                ValueError,
                TypeError,
                http.client.HTTPException,
                jwt.PyJWTError,
            ) as exc:
                self._fail(exc, generation, "ChatGPT sign-in could not be completed safely.")

    def request(self, payload: dict, timeout: float) -> dict:
        if not _number(timeout) or timeout <= 0 or timeout > 300:
            raise CodexError("ChatGPT request timeout is invalid.")
        deadline = time.monotonic() + timeout
        with self._operation:
            with self._lock:
                generation = self._generation
                self._check_generation(generation)
                if not self._record.get("access_token"):
                    raise CodexError("Connect ChatGPT in settings to generate summaries.")
            try:
                messages = payload.get("messages")
                if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
                    raise CodexError("ChatGPT summary input is invalid.")
                instructions, inputs = [], []
                for message in messages:
                    if (
                        not isinstance(message, dict)
                        or message.get("role") not in {"system", "developer", "user"}
                        or not isinstance(message.get("content"), str)
                        or len(message["content"]) > 32768
                    ):
                        raise CodexError("ChatGPT summary input is invalid.")
                    if message["role"] in {"system", "developer"}:
                        instructions.append(message["content"])
                    else:
                        inputs.append({"role": "user", "content": message["content"]})
                if not inputs:
                    raise CodexError("ChatGPT summary input is invalid.")
                access = self._access(deadline, generation)
                catalog = self._json(
                    "GET", RESOURCE + "/models", None, deadline, generation, access
                )
                models = catalog.get("models")
                if not isinstance(models, list) or not any(
                    isinstance(model, dict) and model.get("slug") == MODEL for model in models
                ):
                    raise CodexError("GPT-6 Luna is not available to this ChatGPT account.")
                body = {
                    "model": MODEL,
                    "reasoning": {"effort": EFFORT},
                    "instructions": "\n\n".join(instructions),
                    "input": inputs,
                    "tools": [],
                    "tool_choice": "none",
                    "store": False,
                    "stream": True,
                    "text": {"format": {"type": "json_object"}},
                }
                return self._response(body, access, deadline, generation)
            except (
                OSError,
                ValueError,
                TypeError,
                http.client.HTTPException,
                jwt.PyJWTError,
            ) as exc:
                self._fail(exc, generation, "ChatGPT summary request failed; usage may be charged.")

    def disconnect(self) -> None:
        with self._lock:
            refresh, client = self._record.get("refresh_token"), self._record.get("client_id")
            self._generation += 1
            generation = self._generation
            self._pending = None
            self._validated = False
            self._record = {
                key: self._record[key]
                for key in ("host_id", "client_id", "subject")
                if key in self._record
            }
            self._save_record()
            self._error = None
            for connection in tuple(self._connections):
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
        if refresh and client:
            try:
                deadline = time.monotonic() + 5
                discovery = self._discover(deadline, generation)
                self._json(
                    "POST",
                    discovery["revocation_endpoint"],
                    {"token": refresh, "token_type_hint": "refresh_token", "client_id": client},
                    deadline,
                    generation,
                    empty=True,
                )
            except (OSError, ValueError, TypeError, http.client.HTTPException):
                with self._lock:
                    if generation == self._generation:
                        self._error = (
                            "Signed out locally. Remote revocation was not confirmed; "
                            "disconnect Spiritboard in ChatGPT settings."
                        )

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            self._closed = True
            self._pending = None
            for connection in tuple(self._connections):
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()

    def _check_generation(self, generation: int) -> None:
        if self._closed or generation != self._generation:
            raise CodexError("ChatGPT operation was cancelled.")

    def _fail(self, error: Exception, generation: int, fallback: str) -> None:
        safe = str(error) if isinstance(error, CodexError) else fallback
        with self._lock:
            if generation == self._generation:
                self._error = safe
        raise CodexError(safe) from None

    def _read_record(self) -> dict:
        if (
            self.directory.is_symlink()
            or self.directory.parent.is_symlink()
            or self._path.is_symlink()
        ):
            raise ValueError("Invalid private storage")
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Invalid private storage")
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValueError("Unsafe private storage permissions")
            data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("Invalid private storage")
            record = json.loads(data)
        if not isinstance(record, dict) or not _text(record.get("host_id"), 128):
            raise ValueError("Invalid connection")
        if "client_id" in record and (
            not _text(record["client_id"], 256) or not record["client_id"].startswith("oaiapp_")
        ):
            raise ValueError("Invalid registration")
        if "access_token" in record and (
            not all(_text(record.get(key)) for key in ("access_token", "refresh_token", "id_token"))
            or not _text(record.get("subject"), 256)
            or not _text(record.get("nonce"), 256)
            or not _number(record.get("expires_at"))
            or not isinstance(record.get("scopes"), list)
            or not set(SCOPES.split()).issubset(record["scopes"])
        ):
            raise ValueError("Invalid credentials")
        return record

    def _save_record(self, record: dict | None = None) -> None:
        try:
            self._write_record(record)
        except OSError:
            raise CodexError("ChatGPT private storage could not be written safely.") from None

    def _write_record(self, record: dict | None) -> None:
        if self.directory.parent.is_symlink() or self.directory.is_symlink():
            raise CodexError("ChatGPT private storage could not be written safely.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self._path.is_symlink():
            raise CodexError("ChatGPT private storage could not be written safely.")
        if os.name != "nt":
            self.directory.parent.chmod(0o700)
            self.directory.chmod(0o700)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                temporary = handle.name
                os.chmod(temporary, 0o600)
                handle.write(json.dumps(self._record if record is None else record).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _discover(self, deadline: float, generation: int) -> dict:
        if self._discovery:
            return self._discovery
        data = self._json(
            "GET", ISSUER + "/.well-known/openid-configuration", None, deadline, generation
        )
        expected = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/api/accounts/authorize",
            "token_endpoint": ISSUER + "/api/accounts/oauth/token",
            "jwks_uri": ISSUER + "/.well-known/jwks.json",
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise CodexError("ChatGPT authentication endpoints could not be verified.")
        revoke = data.get("revocation_endpoint")
        if not isinstance(revoke, str) or not self._official_url(revoke, "auth.openai.com"):
            raise CodexError("ChatGPT authentication endpoints could not be verified.")
        self._discovery = data
        return data

    def _identity(
        self, token: str, client: str, nonce: str | None, deadline: float, generation: int
    ) -> dict:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not _text(header.get("kid"), 256):
            raise CodexError("ChatGPT identity could not be verified.")
        discovery = self._discover(deadline, generation)
        for refresh in (False, True):
            if not self._jwks or refresh:
                self._jwks = self._json("GET", discovery["jwks_uri"], None, deadline, generation)
            keys = self._jwks.get("keys")
            if not isinstance(keys, list) or len(keys) > 32:
                raise CodexError("ChatGPT identity could not be verified.")
            matches = [
                key
                for key in keys
                if isinstance(key, dict)
                and key.get("kid") == header["kid"]
                and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
                and _text(key.get("n"), 1400)
                and _text(key.get("e"), 8)
            ]
            if len(matches) == 1:
                key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256")
                claims = jwt.decode(
                    token,
                    key.key,
                    algorithms=["RS256"],
                    issuer=ISSUER,
                    audience=client,
                    leeway=5,
                    options={"require": ["sub", "exp", "iat", "iss", "aud"]},
                )
                if not _text(claims.get("sub"), 256) or (
                    nonce is not None and claims.get("nonce") != nonce
                ):
                    raise CodexError("ChatGPT identity could not be verified.")
                if "azp" in claims and claims["azp"] != client:
                    raise CodexError("ChatGPT identity could not be verified.")
                return claims
        raise CodexError("ChatGPT identity could not be verified.")

    def _tokens(self, tokens: dict, client: str, previous: dict | None = None) -> dict:
        scopes = tokens.get("scope")
        scope_list = (
            scopes.split() if isinstance(scopes, str) else (previous or {}).get("scopes", [])
        )
        if (
            not all(_text(tokens.get(key)) for key in ("access_token", "refresh_token", "id_token"))
            or str(tokens.get("token_type", "")).lower() != "bearer"
            or not _number(tokens.get("expires_in"))
            or not 0 < tokens["expires_in"] <= 86400
            or not set(SCOPES.split()).issubset(scope_list)
        ):
            raise CodexError("ChatGPT plan access was not authorized. Reconnect in settings.")
        return {
            "client_id": client,
            **{key: tokens[key] for key in ("access_token", "refresh_token", "id_token")},
            "expires_at": time.time() + tokens["expires_in"],
            "scopes": scope_list,
        }

    def _access(self, deadline: float, generation: int) -> str:
        with self._lock:
            self._check_generation(generation)
            previous = self._record.copy()
        if previous["expires_at"] > time.time() + 60:
            if not self._validated:
                claims = self._identity(
                    previous["id_token"],
                    previous["client_id"],
                    previous["nonce"],
                    deadline,
                    generation,
                )
                if claims["sub"] != previous["subject"]:
                    raise CodexError("Saved ChatGPT identity did not match. Reconnect in settings.")
                with self._lock:
                    self._check_generation(generation)
                    self._validated = True
            return previous["access_token"]
        discovery = self._discover(deadline, generation)
        tokens = self._json(
            "POST",
            discovery["token_endpoint"],
            {
                "grant_type": "refresh_token",
                "refresh_token": previous["refresh_token"],
                "client_id": previous["client_id"],
                "resource": RESOURCE,
            },
            deadline,
            generation,
        )
        record = self._tokens(tokens, previous["client_id"], previous)
        claims = self._identity(
            record["id_token"], previous["client_id"], None, deadline, generation
        )
        if claims["sub"] != previous["subject"] or (
            "nonce" in claims and claims["nonce"] != previous["nonce"]
        ):
            raise CodexError("ChatGPT renewed identity did not match. Reconnect in settings.")
        record.update({key: previous[key] for key in ("host_id", "subject", "nonce")})
        with self._lock:
            self._check_generation(generation)
            self._save_record(record)
            self._record = record
            self._validated = True
        return record["access_token"]

    @staticmethod
    def _official_url(url: str, host: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == host
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not parsed.query
            and parsed.path.startswith("/")
        )

    def _open(
        self,
        method: str,
        url: str,
        body: bytes | None,
        content_type: str | None,
        deadline: float,
        generation: int,
        access: str | None = None,
    ) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
        parsed = urlsplit(url)
        if parsed.hostname not in {"auth.openai.com", "api.openai.com"} or not self._official_url(
            url, parsed.hostname
        ):
            raise CodexError("ChatGPT request endpoint could not be verified.")
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=_remaining(deadline))
        with self._lock:
            self._check_generation(generation)
            self._connections.add(connection)
        headers = {"Accept": "application/json", "User-Agent": "Spiritboard/0.1"}
        if content_type:
            headers["Content-Type"] = content_type
        if access:
            headers["Authorization"] = "Bearer " + access
        try:
            connection.request(method, parsed.path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                if response.status in {401, 403}:
                    raise CodexError("ChatGPT access was declined. Reconnect or check plan access.")
                if response.status == 429:
                    raise CodexError("ChatGPT usage limit reached. Try again later.")
                raise CodexError("ChatGPT service could not complete the request.")
            return connection, response
        except Exception:
            self._release(connection)
            raise

    def _release(self, connection: http.client.HTTPSConnection) -> None:
        connection.close()
        with self._lock:
            self._connections.discard(connection)

    @staticmethod
    def _read_timeout(
        connection: http.client.HTTPSConnection, response: http.client.HTTPResponse, deadline: float
    ) -> None:
        timeout = _remaining(deadline)
        # A Connection: close response can detach the socket from HTTPSConnection.
        stream = getattr(getattr(response, "fp", None), "raw", None)
        sock = connection.sock or getattr(stream, "_sock", None)
        if sock is not None:
            sock.settimeout(timeout)

    def _json(
        self,
        method: str,
        url: str,
        body: dict | None,
        deadline: float,
        generation: int,
        access: str | None = None,
        *,
        empty: bool = False,
    ) -> dict:
        encoded = urlencode(body).encode("ascii") if body is not None else None
        connection, response = self._open(
            method,
            url,
            encoded,
            "application/x-www-form-urlencoded" if body is not None else None,
            deadline,
            generation,
            access,
        )
        try:
            data = bytearray()
            while True:
                self._read_timeout(connection, response, deadline)
                with self._lock:
                    self._check_generation(generation)
                block = response.read1(min(8192, MAX_BYTES + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > MAX_BYTES:
                    raise CodexError("ChatGPT response exceeded the safety limit.")
            _remaining(deadline)
            if empty and not data:
                return {}
            result = json.loads(data)
            if not isinstance(result, dict):
                raise CodexError("ChatGPT response was invalid.")
            return result
        finally:
            self._release(connection)

    def _response(self, body: dict, access: str, deadline: float, generation: int) -> dict:
        connection, response = self._open(
            "POST",
            RESOURCE + "/responses",
            json.dumps(body).encode("utf-8"),
            "application/json",
            deadline,
            generation,
            access,
        )
        try:
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip()
                != "text/event-stream"
            ):
                raise CodexError("ChatGPT did not return a response stream.")
            total, pending, event_data = 0, bytearray(), []
            while True:
                self._read_timeout(connection, response, deadline)
                with self._lock:
                    self._check_generation(generation)
                chunk = response.read1(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    raise CodexError("ChatGPT response exceeded the safety limit.")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    line = line.rstrip(b"\r")
                    if line.startswith(b"data:"):
                        event_data.append(line[5:].lstrip(b" ").decode("utf-8"))
                    elif not line and event_data:
                        raw, event_data = "\n".join(event_data), []
                        if raw == "[DONE]":
                            raise CodexError("ChatGPT stream ended without completed inference.")
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            raise CodexError("ChatGPT response stream was invalid.")
                        kind = event.get("type")
                        if kind in {"error", "response.failed", "response.incomplete"}:
                            raise CodexError(
                                "ChatGPT inference did not complete; usage may be charged."
                            )
                        if kind in {"response.output_item.added", "response.output_item.done"}:
                            item = event.get("item")
                            if not isinstance(item, dict) or item.get("type") not in {
                                "message",
                                "reasoning",
                            }:
                                raise CodexError(
                                    "ChatGPT returned a tool request; it was not executed."
                                )
                        if kind == "response.completed":
                            _remaining(deadline)
                            with self._lock:
                                self._check_generation(generation)
                            return self._completed(event.get("response"))
            raise CodexError(
                "ChatGPT stream ended without completed inference; usage may be charged."
            )
        finally:
            self._release(connection)

    @staticmethod
    def _completed(response: object) -> dict:
        if (
            not isinstance(response, dict)
            or response.get("status") != "completed"
            or response.get("model") != MODEL
            or not _text(response.get("id"), 256)
            or response.get("error") is not None
            or not isinstance(response.get("output"), list)
        ):
            raise CodexError("ChatGPT completed response could not be verified.")
        text = []
        for item in response["output"]:
            if not isinstance(item, dict) or item.get("type") not in {"reasoning", "message"}:
                raise CodexError("ChatGPT returned a tool request; it was not executed.")
            if item["type"] == "message":
                if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                    raise CodexError("ChatGPT summary output was invalid.")
                for part in item["content"]:
                    if (
                        not isinstance(part, dict)
                        or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)
                    ):
                        raise CodexError("ChatGPT summary output was invalid.")
                    text.append(part["text"])
        content = "".join(text)
        if len(content) > MAX_BYTES or not isinstance(json.loads(content), dict):
            raise CodexError("ChatGPT summary output was invalid.")
        usage = response.get("usage")
        safe_usage = {}
        if isinstance(usage, dict):
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                if (
                    isinstance(usage.get(name), int)
                    and not isinstance(usage[name], bool)
                    and usage[name] >= 0
                ):
                    safe_usage[name] = usage[name]
        return {
            "id": response["id"],
            "choices": [{"message": {"content": content}}],
            "usage": safe_usage,
        }
