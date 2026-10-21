"""Native OS credentials, with nonsecret pointers in Spiritboard's private data directory."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Protocol

from .launcher import _private_directory, _regular_descriptor

_MESSAGE = (
    "Secure credential storage is unavailable. Unlock your OS credential store and try again."
)
_USERNAME = "spiritboard"
_CHUNK_SIZE = 1000
_MAX_PAYLOAD = 256 * 1024
_MAX_CHUNKS = (4 * ((_MAX_PAYLOAD + 2) // 3) + _CHUNK_SIZE - 1) // _CHUNK_SIZE
_MAX_MANIFEST = 8192
_STORE_LOCK = threading.RLock()


class CredentialStoreError(ValueError):
    """A fixed, safe storage error, without provider details or secret values."""


class _Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _native_backend() -> _Backend:
    # Never use keyring's configurable discovery, plugins, chains, or environment properties.
    # Bypass its base constructor, which applies arbitrary KEYRING_PROPERTY_* attributes.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        backend = object.__new__(Keyring)
        backend.keychain = None
        return backend
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        backend = object.__new__(WinVaultKeyring)
        backend._persist = 2  # CRED_PERSIST_LOCAL_MACHINE; not enterprise roaming.
        return backend
    if sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring

        backend = object.__new__(Keyring)
        backend.scheme = "default"
        backend.appid = "Spiritboard"
        return backend
    raise CredentialStoreError(_MESSAGE)


class CredentialStore:
    """Keep bounded credentials in an explicitly selected secure native backend."""

    def __init__(self, data_root: Path) -> None:
        self._lock = _STORE_LOCK
        self._initialization_failed = True
        try:
            self._root = Path(data_root).absolute()
            canonical = os.path.normcase(str(self._root.resolve()))
            self._namespace = hashlib.sha256(os.fsencode(canonical)).hexdigest()
            self._directory = self._root / "auth" / "credentials"
        except Exception:
            # Optional credentials must not prevent the local monitor from being constructed.
            return
        self._initialization_failed = False

    def load(self, name: str) -> str | None:
        try:
            with self._lock:
                manifest = self._read(name)
                if manifest is None:
                    return None
                if manifest["retired"] or manifest["active"] is None:
                    raise ValueError
                record = manifest["active"]
                backend = _native_backend()
                encoded = "".join(
                    self._part(backend, name, record, i) for i in range(record["count"])
                )
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) != record["size"] or not hmac.compare_digest(
                    hashlib.sha256(raw).hexdigest(), record["digest"]
                ):
                    raise ValueError
                return raw.decode("utf-8")
        except Exception:
            raise CredentialStoreError(_MESSAGE) from None

    def save(self, name: str, value: str) -> None:
        try:
            with self._lock:
                self._name(name)
                if not isinstance(value, str) or not value or len(value) > _MAX_PAYLOAD:
                    raise ValueError
                raw = value.encode("utf-8")
                if len(raw) > _MAX_PAYLOAD:
                    raise ValueError
                encoded = base64.b64encode(raw).decode("ascii")
                chunks = [encoded[i : i + _CHUNK_SIZE] for i in range(0, len(encoded), _CHUNK_SIZE)]
                record = {
                    "generation": uuid.uuid4().hex,
                    "count": len(chunks),
                    "size": len(raw),
                    "digest": hashlib.sha256(raw).hexdigest(),
                }
                previous = self._read(name)
                backend = _native_backend()
                if previous is not None and previous["retired"]:
                    previous = self._cleanup(backend, name, previous)
                active = previous["active"] if previous is not None else None
                # Record the new generation before writing secrets, so interruption is recoverable.
                self._write(name, {"version": 1, "active": active, "retired": [record]})
                try:
                    for i, chunk in enumerate(chunks):
                        service = self._service(name, record, i)
                        backend.set_password(service, _USERNAME, chunk)
                        if backend.get_password(service, _USERNAME) != chunk:
                            raise ValueError
                except Exception:
                    self._remove(backend, name, record)
                    if previous is None:
                        self._unlink(name)
                    else:
                        self._write(name, previous)
                    raise
                retired = [active] if active is not None else []
                self._write(name, {"version": 1, "active": record, "retired": retired})
                for old in retired:
                    self._remove(backend, name, old)
                if retired:
                    self._write(name, {"version": 1, "active": record, "retired": []})
        except Exception:
            raise CredentialStoreError(_MESSAGE) from None

    def delete(self, name: str) -> None:
        try:
            with self._lock:
                manifest = self._read(name)
                if manifest is None:
                    return
                records = manifest["retired"]
                if manifest["active"] is not None:
                    records = [manifest["active"], *records]
                self._write(name, {"version": 1, "active": None, "retired": records})
                backend = _native_backend()
                for record in records:
                    self._remove(backend, name, record)
                self._unlink(name)
        except Exception:
            raise CredentialStoreError(_MESSAGE) from None

    @staticmethod
    def _name(name: str) -> None:
        if name not in ("openrouter", "codex", "openai", "anthropic"):
            raise ValueError

    def _service(self, name: str, record: dict, index: int) -> str:
        return f"spiritboard:{self._namespace}:{name}:{record['generation']}:{index}"

    def _part(self, backend: _Backend, name: str, record: dict, index: int) -> str:
        value = backend.get_password(self._service(name, record, index), _USERNAME)
        encoded_size = 4 * ((record["size"] + 2) // 3)
        expected = min(_CHUNK_SIZE, encoded_size - index * _CHUNK_SIZE)
        if not isinstance(value, str) or len(value) != expected or not value.isascii():
            raise ValueError
        return value

    def _remove(self, backend: _Backend, name: str, record: dict) -> None:
        for i in range(record["count"]):
            service = self._service(name, record, i)
            if backend.get_password(service, _USERNAME) is not None:
                backend.delete_password(service, _USERNAME)
            if backend.get_password(service, _USERNAME) is not None:
                raise ValueError

    def _cleanup(self, backend: _Backend, name: str, manifest: dict) -> dict | None:
        retired = list(manifest["retired"])
        while retired:
            self._remove(backend, name, retired[0])
            retired.pop(0)
            if manifest["active"] is None and not retired:
                self._unlink(name)
                return None
            manifest = {"version": 1, "active": manifest["active"], "retired": retired}
            self._write(name, manifest)
        return manifest

    def _directories(self, *, create: bool = False) -> bool:
        for path in (self._root, self._root / "auth", self._directory):
            if not create:
                try:
                    path.lstat()
                except FileNotFoundError:
                    return False
            _private_directory(path)
        return True

    def _read(self, name: str) -> dict | None:
        if self._initialization_failed:
            raise ValueError
        self._name(name)
        if not self._directories():
            return None
        path = self._directory / f"{name}.json"
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        descriptor = _regular_descriptor(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_MANIFEST + 1)
        if len(raw) > _MAX_MANIFEST:
            raise ValueError
        manifest = json.loads(raw)
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"version", "active", "retired"}
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or not isinstance(manifest["retired"], list)
            or len(manifest["retired"]) > 2
        ):
            raise ValueError
        records = list(manifest["retired"])
        if manifest["active"] is not None:
            records.append(manifest["active"])
        if not records or len(records) > 2:
            raise ValueError
        generations = set()
        for record in records:
            self._record(record)
            if record["generation"] in generations:
                raise ValueError
            generations.add(record["generation"])
        return manifest

    @staticmethod
    def _record(record: object) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != {"generation", "count", "size", "digest"}
            or not isinstance(record["generation"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", record["generation"])
            or not isinstance(record["digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["digest"])
            or type(record["size"]) is not int
            or not 1 <= record["size"] <= _MAX_PAYLOAD
            or type(record["count"]) is not int
            or not 1 <= record["count"] <= _MAX_CHUNKS
            or record["count"] != (4 * ((record["size"] + 2) // 3) + 999) // 1000
        ):
            raise ValueError

    def _write(self, name: str, manifest: dict) -> None:
        self._directories(create=True)
        path = self._directory / f"{name}.json"
        if path.exists() or path.is_symlink():
            os.close(_regular_descriptor(path))
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}-", dir=self._directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _unlink(self, name: str) -> None:
        path = self._directory / f"{name}.json"
        os.close(_regular_descriptor(path))
        path.unlink()
