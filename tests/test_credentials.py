from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from spiritboard import credentials
from spiritboard.credentials import CredentialStore, CredentialStoreError

_NATIVE_FACTORY = credentials._native_backend


def test_new_store_and_missing_manifest_never_select_keychain(tmp_path, monkeypatch) -> None:
    def forbidden():
        pytest.fail("Native credential access on an unconnected installation")

    monkeypatch.setattr(credentials, "_native_backend", forbidden)
    root = tmp_path / "new"
    store = CredentialStore(root)
    assert store.load("openrouter") is None
    assert store.load("codex") is None
    store.delete("codex")
    assert not root.exists()


def test_round_trip_keeps_only_nonsecret_manifest(tmp_path, native_credentials) -> None:
    store = CredentialStore(tmp_path)
    secret = '{"access_token":"synthetic-credential","refresh_token":"synthetic-refresh"}'
    store.save("codex", secret)
    assert CredentialStore(tmp_path).load("codex") == secret
    manifest = tmp_path / "auth" / "credentials" / "codex.json"
    assert "synthetic" not in manifest.read_text()
    data = json.loads(manifest.read_text())
    assert set(data) == {"version", "active", "retired"}
    assert not data["retired"]
    if os.name == "posix":
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert manifest.parent.stat().st_mode & 0o777 == 0o700
    assert all(username == "spiritboard" for _, _, username in native_credentials.calls)
    store.delete("codex")
    assert not manifest.exists()
    assert not native_credentials.values
    assert store.load("codex") is None


@pytest.mark.parametrize("size", [1, 749, 750, 751, 25000, 262144])
def test_chunks_fit_windows_limit(tmp_path, native_credentials, size) -> None:
    value = "x" * size
    store = CredentialStore(tmp_path)
    store.save("codex", value)
    assert store.load("codex") == value
    assert all(len(part.encode("utf-16-le")) <= 2000 for part in native_credentials.values.values())
    assert all(part.isascii() for part in native_credentials.values.values())
    store.delete("codex")
    assert not native_credentials.values


def test_unicode_and_generation_replacement(tmp_path, native_credentials) -> None:
    store = CredentialStore(tmp_path)
    store.save("openrouter", "synthetic-old")
    original = set(native_credentials.values)
    store.save("openrouter", "🔐" * 1000)
    assert store.load("openrouter") == "🔐" * 1000
    assert not original.intersection(native_credentials.values)


def test_namespaces_separate_providers_and_canonical_roots(tmp_path, native_credentials) -> None:
    first = CredentialStore(tmp_path / "first")
    alias = CredentialStore(tmp_path / "first" / ".." / "first")
    second = CredentialStore(tmp_path / "second")
    first.save("openrouter", "or-one")
    first.save("codex", "codex-one")
    second.save("openrouter", "or-two")
    assert alias.load("openrouter") == "or-one"
    assert first.load("codex") == "codex-one"
    assert second.load("openrouter") == "or-two"
    assert all(str(tmp_path) not in service for service, _ in native_credentials.values)


@pytest.mark.parametrize("value", ["", "x" * 262145, "🔐" * 70000, None, 123])
def test_reject_oversized_or_invalid_payload_before_keychain(tmp_path, native_credentials, value):
    with pytest.raises(CredentialStoreError):
        CredentialStore(tmp_path).save("codex", value)
    assert not native_credentials.calls


@pytest.mark.parametrize("name", ["../codex", "other", "", None])
def test_only_fixed_provider_names(tmp_path, native_credentials, name) -> None:
    store = CredentialStore(tmp_path)
    for operation in (
        lambda: store.load(name),
        lambda: store.save(name, "x"),
        lambda: store.delete(name),
    ):
        with pytest.raises(CredentialStoreError):
            operation()
    assert not native_credentials.calls


def test_legacy_files_are_ignored_and_never_deleted(tmp_path, monkeypatch) -> None:
    auth = tmp_path / "auth"
    auth.mkdir(mode=0o700)
    (auth / "codex").mkdir(mode=0o700)
    paths = [auth / "openrouter.json", auth / "codex" / "connection.json"]
    for path in paths:
        path.write_text("do-not-read-legacy-synthetic-secret")
    monkeypatch.setattr(credentials, "_native_backend", lambda: pytest.fail("Legacy keychain read"))
    store = CredentialStore(tmp_path)
    for name in ("openrouter", "codex"):
        assert store.load(name) is None
        store.delete(name)
    assert all(path.read_text() == "do-not-read-legacy-synthetic-secret" for path in paths)


def test_missing_or_corrupt_chunks_fail_closed(tmp_path, native_credentials) -> None:
    store = CredentialStore(tmp_path)
    store.save("codex", "x" * 3000)
    service = next(iter(native_credentials.values))
    original = native_credentials.values.pop(service)
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    native_credentials.values[service] = "!" * len(original)
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    native_credentials.values[service] = "A" * len(original)
    with pytest.raises(CredentialStoreError):
        store.load("codex")


def test_backend_errors_are_fixed_and_do_not_leak_values(tmp_path, monkeypatch, native_credentials):
    store = CredentialStore(tmp_path)
    store.save("codex", "synthetic-secret")

    def broken():
        raise RuntimeError("synthetic-secret /private/local/path")

    monkeypatch.setattr(credentials, "_native_backend", broken)
    for operation in (lambda: store.load("codex"), lambda: store.save("codex", "new")):
        with pytest.raises(CredentialStoreError) as error:
            operation()
        assert str(error.value) == credentials._MESSAGE
        assert error.value.__suppress_context__


def test_partial_write_failure_restores_previous_generation(
    tmp_path, native_credentials, monkeypatch
):
    store = CredentialStore(tmp_path)
    store.save("codex", "old")
    before = dict(native_credentials.values)
    setter = native_credentials.set_password
    writes = 0

    def broken(service, username, value):
        nonlocal writes
        writes += 1
        setter(service, username, value)
        if writes == 2:
            raise RuntimeError("upstream detail")

    monkeypatch.setattr(native_credentials, "set_password", broken)
    with pytest.raises(CredentialStoreError):
        store.save("codex", "x" * 3000)
    assert native_credentials.values == before
    assert store.load("codex") == "old"


def test_failed_cleanup_keeps_every_reference_for_disconnect(
    tmp_path, native_credentials, monkeypatch
):
    store = CredentialStore(tmp_path)
    store.save("codex", "old")
    deleter = native_credentials.delete_password

    def locked(service, username):
        raise RuntimeError("locked")

    monkeypatch.setattr(native_credentials, "delete_password", locked)
    with pytest.raises(CredentialStoreError):
        store.save("codex", "new")
    manifest = tmp_path / "auth" / "credentials" / "codex.json"
    assert len(json.loads(manifest.read_text())["retired"]) == 1
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    with pytest.raises(CredentialStoreError):
        store.delete("codex")
    assert manifest.exists()
    monkeypatch.setattr(native_credentials, "delete_password", deleter)
    store.delete("codex")
    assert not native_credentials.values
    assert not manifest.exists()


def test_interrupted_initial_write_is_still_removable(tmp_path, native_credentials, monkeypatch):
    store = CredentialStore(tmp_path)
    setter = native_credentials.set_password
    deleter = native_credentials.delete_password

    def broken(service, username, value):
        setter(service, username, value)
        raise RuntimeError("write interrupted")

    monkeypatch.setattr(native_credentials, "set_password", broken)
    monkeypatch.setattr(native_credentials, "delete_password", broken)
    with pytest.raises(CredentialStoreError):
        store.save("openrouter", "new")
    with pytest.raises(CredentialStoreError):
        store.load("openrouter")
    monkeypatch.setattr(native_credentials, "delete_password", deleter)
    store.delete("openrouter")
    assert not native_credentials.values


@pytest.mark.parametrize("failure", ["initial-write", "replacement-write", "superseded"])
def test_unlock_then_explicit_save_cleans_retired_generations(
    tmp_path, native_credentials, monkeypatch, failure
) -> None:
    store = CredentialStore(tmp_path)
    if failure != "initial-write":
        store.save("codex", "old")
    setter = native_credentials.set_password
    deleter = native_credentials.delete_password

    def blocked_write(service, username, value):
        setter(service, username, value)
        raise RuntimeError("interrupted synthetic write")

    def blocked_delete(service, username):
        raise RuntimeError("locked synthetic keychain")

    if failure != "superseded":
        monkeypatch.setattr(native_credentials, "set_password", blocked_write)
    monkeypatch.setattr(native_credentials, "delete_password", blocked_delete)
    with pytest.raises(CredentialStoreError):
        store.save("codex", "interrupted")
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    retained = set(native_credentials.values)
    assert retained
    with pytest.raises(CredentialStoreError):
        store.save("codex", "still-locked")
    assert set(native_credentials.values) == retained
    monkeypatch.setattr(native_credentials, "set_password", setter)
    monkeypatch.setattr(native_credentials, "delete_password", deleter)
    CredentialStore(tmp_path).save("codex", "retry-succeeded")
    assert store.load("codex") == "retry-succeeded"
    assert not retained.intersection(native_credentials.values)
    assert len(native_credentials.values) == 1
    manifest = tmp_path / "auth" / "credentials" / "codex.json"
    assert json.loads(manifest.read_text())["retired"] == []


def test_multiple_store_instances_serialize_save_and_delete(tmp_path, native_credentials) -> None:
    stores = [CredentialStore(tmp_path), CredentialStore(tmp_path / ".")]
    assert stores[0]._lock is stores[1]._lock

    def change(index: int) -> None:
        store = stores[index % len(stores)]
        if index % 3:
            store.save("codex", f"synthetic-{index}" * 100)
        else:
            store.delete("codex")

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(change, range(40)))
    stores[0].save("codex", "final")
    assert stores[1].load("codex") == "final"
    assert len(native_credentials.values) == 1
    stores[1].delete("codex")
    assert not native_credentials.values


@pytest.mark.parametrize("mutation", ["truncated", "oversized", "count", "duplicate", "extra"])
def test_bad_manifest_does_not_access_keychain(tmp_path, native_credentials, mutation) -> None:
    store = CredentialStore(tmp_path)
    store.save("codex", "original")
    manifest = tmp_path / "auth" / "credentials" / "codex.json"
    data = json.loads(manifest.read_text())
    if mutation == "truncated":
        manifest.write_text("{")
    elif mutation == "oversized":
        manifest.write_text("x" * 8193)
    else:
        if mutation == "count":
            data["active"]["count"] = 10000000
        elif mutation == "duplicate":
            data["retired"].append(data["active"])
        else:
            data["unexpected"] = "no"
        manifest.write_text(json.dumps(data))
    native_credentials.calls.clear()
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    assert not native_credentials.calls


@pytest.mark.skipif(os.name != "posix", reason="POSIX file protections")
@pytest.mark.parametrize("target", ["auth", "credentials", "manifest", "permissions", "hardlink"])
def test_unsafe_local_metadata_rejected(tmp_path, native_credentials, target) -> None:
    store = CredentialStore(tmp_path)
    store.save("codex", "original")
    manifest = tmp_path / "auth" / "credentials" / "codex.json"
    if target in ("auth", "credentials"):
        directory = manifest.parent.parent if target == "auth" else manifest.parent
        moved = tmp_path / f"moved-{target}"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    elif target == "manifest":
        moved = tmp_path / "moved.json"
        manifest.rename(moved)
        manifest.symlink_to(moved)
    elif target == "permissions":
        manifest.chmod(0o644)
    else:
        os.link(manifest, tmp_path / "extra-link")
    native_credentials.calls.clear()
    with pytest.raises(CredentialStoreError):
        store.load("codex")
    assert not native_credentials.calls


@pytest.mark.parametrize(
    ("platform", "module", "class_name"),
    [
        ("darwin", "keyring.backends.macOS", "Keyring"),
        ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
        ("linux", "keyring.backends.SecretService", "Keyring"),
    ],
)
def test_factory_uses_only_pinned_native_class_ignoring_environment(
    monkeypatch, platform, module, class_name
) -> None:
    class Native:
        def __init__(self):
            pytest.fail("Backend init applies untrusted environment properties")

    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(**{class_name: Native}))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    monkeypatch.setenv("KEYRING_PROPERTY_KEYCHAIN", "/tmp/not-a-keychain")
    monkeypatch.setenv("KEYRING_PROPERTY_SCHEME", "KeePassXC")
    monkeypatch.setenv("KEYRING_PROPERTY_PREFERRED_COLLECTION", "/untrusted")
    monkeypatch.setenv("KEYCHAIN_PATH", "/untrusted")
    backend = _NATIVE_FACTORY()
    assert type(backend) is Native
    if platform == "darwin":
        assert backend.keychain is None
    elif platform == "win32":
        assert backend._persist == 2
    else:
        assert backend.scheme == "default"
        assert not hasattr(backend, "preferred_collection")


def test_unsupported_platform_has_no_backend_fallback(monkeypatch) -> None:
    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform="unsupported"))
    with pytest.raises(CredentialStoreError, match="Secure credential storage"):
        _NATIVE_FACTORY()
