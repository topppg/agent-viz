"""Explicit consent and local connection policy for summary providers."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from .launcher import LaunchError, _private_directory, _regular_descriptor
from .openrouter_auth import OpenRouterConnection, ProviderAuthError

_MODELS = {"openrouter": "deepseek/deepseek-v4-flash", "codex": "gpt-6-luna"}
_PREFERENCE_ERROR = "Summary preferences could not be stored or read safely."
_CONSENT_ERROR = "Summary settings changed. Please start the connection again."
_DISABLED_ERROR = "Summary requests need an enabled, connected provider."
_DEMO_ERROR = "Provider connections are disabled in the synthetic demo."


class _CodexProvider(Protocol):
    def start(self) -> None: ...
    def begin_login(self, callback_url: str) -> dict[str, object]: ...
    def complete(
        self, state: str, code: str, *, client_id: str | None = None, error: str | None = None
    ) -> None: ...
    def public_state(self) -> dict[str, object]: ...
    def request(self, payload: dict, timeout: float) -> dict: ...
    def disconnect(self) -> None: ...
    def close(self) -> None: ...


def _codex_connection(directory: Path) -> _CodexProvider | None:
    try:
        from .codex_summary import CodexConnection
    except ImportError:
        return None
    return CodexConnection(directory)


class SummaryProviders:
    """OpenRouter first, then Codex, only after the user has enabled summaries."""

    def __init__(
        self,
        data_root: Path,
        *,
        demo: bool = False,
        session_key: str | None = None,
        enabled: bool = False,
    ):
        self._directory = Path(data_root)
        self._preferences = self._directory / "summary-settings.json"
        self._demo = demo
        self._lock = threading.RLock()
        self._revision = 0
        self._pending: dict[str, int] = {}
        self._completing: set[str] = set()
        self._blocked: set[str] = set()
        self._disconnect_errors: set[str] = set()
        self._closed = False
        self._active: str | None = None
        self._codex: _CodexProvider | None = None
        self._codex_started = False
        self._codex_error = False
        self._session_key = session_key if not demo else None
        self._openrouter = OpenRouterConnection(self._directory) if not demo else None
        self._enabled = False
        self._preference_error = False
        if not demo:
            self._enabled = self._read_enabled() or enabled

    def _read_enabled(self) -> bool:
        try:
            _private_directory(self._directory)
            if not self._preferences.exists() and not self._preferences.is_symlink():
                return False
            descriptor = _regular_descriptor(self._preferences)
            try:
                raw = os.read(descriptor, 2049)
            finally:
                os.close(descriptor)
            if len(raw) > 2048:
                raise ValueError
            saved = json.loads(raw)
            if (
                not isinstance(saved, dict)
                or saved.get("version") != 1
                or type(saved.get("enabled")) is not bool
            ):
                raise ValueError
            blocked = saved.get("blocked", [])
            if not isinstance(blocked, list) or any(
                not isinstance(name, str) or name not in _MODELS for name in blocked
            ):
                raise ValueError
            self._blocked = set(blocked)
            return saved["enabled"]
        except (LaunchError, OSError, ValueError, UnicodeError, RecursionError):
            self._preference_error = True
            return False

    def _save_enabled(self, enabled: bool, *, unblock: str | None = None) -> None:
        temporary: str | None = None
        blocked = self._blocked - {unblock} if unblock is not None else self._blocked.copy()
        try:
            _private_directory(self._directory)
            if self._preferences.exists() or self._preferences.is_symlink():
                os.close(_regular_descriptor(self._preferences))
            descriptor, temporary = tempfile.mkstemp(
                prefix=".summary-settings-", dir=self._directory
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(
                    json.dumps(
                        {"version": 1, "enabled": enabled, "blocked": sorted(blocked)}
                    ).encode("ascii")
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._preferences)
            temporary = None
            self._blocked = blocked
            self._preference_error = False
        except (LaunchError, OSError):
            self._preference_error = True
            self._enabled = False
            raise ProviderAuthError(_PREFERENCE_ERROR) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _mutating(self) -> None:
        if self._demo:
            raise ProviderAuthError(_DEMO_ERROR)
        if self._closed:
            raise ProviderAuthError("Summary provider connections have been closed.")

    def _name(self, name: str) -> None:
        if not isinstance(name, str) or name not in _MODELS:
            raise ProviderAuthError("Choose an available summary provider.")

    def _changed(self) -> None:
        self._revision += 1
        self._pending.clear()
        self._completing.clear()
        self._active = None

    def start(self) -> None:
        with self._lock:
            if self._demo or self._closed or self._codex_started:
                return
            self._codex_started = True
            blocked = "codex" in self._blocked
        try:
            codex = _codex_connection(self._directory)
            if codex is not None and not blocked:
                codex.start()
            with self._lock:
                if not self._closed:
                    self._codex = codex
                    forget = "codex" in self._blocked
                    closed = False
                else:
                    forget = False
                    closed = True
            if closed and codex is not None:
                codex.close()
            elif forget and codex is not None:
                codex.disconnect()
        except (OSError, ValueError, RuntimeError, ProviderAuthError):
            with self._lock:
                self._codex_error = True

    def _status(self, name: str) -> dict[str, object]:
        if name == "openrouter":
            state = self._openrouter.public_state() if self._openrouter is not None else {}
            connected = state.get("connected") is True or self._session_key is not None
            available = not self._demo
        else:
            state = self._codex.public_state() if self._codex is not None else {}
            connected = state.get("connected") is True
            available = state.get("available") is True
        error = None
        if state.get("error") or (name == "codex" and self._codex_error):
            error = f"{name.title()} connection needs attention."
        if name in self._disconnect_errors:
            error = "Provider disabled in Spiritboard, but saved credentials could not be removed."
        return {
            "available": available,
            "connected": connected and name not in self._blocked,
            "pending": name in self._pending,
            "error": error,
            "revocation_unconfirmed": state.get("revocation_unconfirmed") is True,
            "model": _MODELS[name],
            **({"effort": "medium"} if name == "codex" else {}),
        }

    def public_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._enabled and not self._closed,
                "active_provider": self._active,
                "policy": "openrouter-first",
                "demo": self._demo,
                "providers": {name: self._status(name) for name in _MODELS},
                "error": _PREFERENCE_ERROR if self._preference_error else None,
            }

    def routes(self) -> list[str]:
        with self._lock:
            if not self._enabled or self._demo or self._closed:
                return []
            return [name for name in _MODELS if self._status(name)["connected"]]

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def allowed(self, name: str, revision: int) -> bool:
        with self._lock:
            return revision == self._revision and name in self.routes()

    def model(self, name: str) -> str:
        self._name(name)
        return _MODELS[name]

    def billing_kind(self, name: str) -> str:
        self._name(name)
        return "api" if name == "openrouter" else "subscription"

    def request(self, name: str, payload: dict, timeout: float) -> dict:
        self._name(name)
        with self._lock:
            if name not in self.routes():
                raise ProviderAuthError(_DISABLED_ERROR)
            key = None
            codex = self._codex
            if self._openrouter is not None:
                key = self._openrouter.key() or self._session_key
        if name == "openrouter" and key is not None:
            from .summaries import _request

            return _request(key, payload, timeout)
        if name == "codex" and codex is not None:
            return codex.request(payload, timeout)
        raise ProviderAuthError(_DISABLED_ERROR)

    def record_success(self, name: str) -> None:
        self._name(name)
        with self._lock:
            if name in self.routes():
                self._active = name

    def begin(self, name: str, callback_url: str) -> dict[str, object]:
        self._name(name)
        with self._lock:
            self._mutating()
            if self._pending:
                raise ProviderAuthError("Finish or cancel current sign-in first.")
        if name == "codex":
            self.start()
        with self._lock:
            self._mutating()
            if self._pending:
                raise ProviderAuthError("Finish or cancel current sign-in first.")
            if not self._status(name)["available"]:
                raise ProviderAuthError("This summary provider is unavailable.")
            self._changed()
            revision = self._revision
            self._pending[name] = revision
            provider = self._openrouter if name == "openrouter" else self._codex
        try:
            if name == "openrouter" and provider is not None:
                result = provider.begin(callback_url)
            elif name == "codex" and provider is not None:
                result = provider.begin_login(callback_url)
            else:
                raise ProviderAuthError("This summary provider is unavailable.")
            with self._lock:
                if revision != self._revision or self._closed:
                    raise ProviderAuthError(_CONSENT_ERROR)
            return {key: result[key] for key in ("auth_url", "expires_in") if key in result}
        except Exception:
            with self._lock:
                if revision == self._revision:
                    self._pending.pop(name, None)
            raise

    def complete(self, name: str, state: str, code: str, **extra: object) -> None:
        self._name(name)
        with self._lock:
            self._mutating()
            revision = self._pending.get(name)
            if revision is None or revision != self._revision or name in self._completing:
                raise ProviderAuthError(_CONSENT_ERROR)
            self._completing.add(name)
            provider = self._openrouter if name == "openrouter" else self._codex
        try:
            if name == "openrouter" and provider is not None:
                provider.complete(state, code)
            elif name == "codex" and provider is not None:
                client_id, error = extra.get("client_id"), extra.get("error")
                provider.complete(
                    state,
                    code,
                    client_id=client_id if isinstance(client_id, str) else None,
                    error=error if isinstance(error, str) else None,
                )
            else:
                raise ProviderAuthError("This summary provider is unavailable.")
            with self._lock:
                if revision != self._revision or self._closed:
                    raise ProviderAuthError(_CONSENT_ERROR)
                if provider.public_state().get("connected") is not True:
                    raise ProviderAuthError("Summary provider sign-in did not complete.")
                self._save_enabled(True, unblock=name)
                self._disconnect_errors.discard(name)
                self._enabled = True
                self._changed()
        finally:
            with self._lock:
                if revision == self._revision:
                    self._completing.discard(name)
                    if provider is None or provider.public_state().get("pending") is not True:
                        self._pending.pop(name, None)

    def set_key(self, key: str) -> None:
        with self._lock:
            self._mutating()
            self._changed()
            if self._openrouter is None:
                raise ProviderAuthError("This summary provider is unavailable.")
            self._openrouter.set_key(key)
            self._session_key = None
            self._save_enabled(True, unblock="openrouter")
            self._disconnect_errors.discard("openrouter")
            self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ProviderAuthError("Summary enablement must be true or false.")
        with self._lock:
            self._mutating()
            if enabled and not any(self._status(name)["connected"] for name in _MODELS):
                raise ProviderAuthError("Connect a provider before enabling summaries.")
            self._changed()
            if not enabled:
                self._enabled = False
            self._save_enabled(enabled)
            self._enabled = enabled

    def disconnect(self, name: str) -> None:
        self._name(name)
        with self._lock:
            self._mutating()
            self._changed()
            self._blocked.add(name)
            provider = self._openrouter if name == "openrouter" else self._codex
            if name == "openrouter":
                self._session_key = None
            enabled = self._enabled and any(self._status(item)["connected"] for item in _MODELS)
            self._save_enabled(enabled)
            self._enabled = enabled
        try:
            if provider is not None:
                provider.disconnect()
        except Exception:
            with self._lock:
                self._disconnect_errors.add(name)
            raise ProviderAuthError(
                "Provider disabled in Spiritboard, but saved credentials could not be removed."
            ) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._changed()
            providers = (self._openrouter, self._codex)
        for provider in providers:
            if provider is not None:
                provider.close()
