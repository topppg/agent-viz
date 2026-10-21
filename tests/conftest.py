import pytest

from spiritboard import credentials
from spiritboard.harnesses import claude


class MemoryCredentials:
    """A per-test credential vault; tests never access the developer's OS keychain."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        self.values.pop((service, username), None)


@pytest.fixture(autouse=True)
def native_credentials(monkeypatch: pytest.MonkeyPatch) -> MemoryCredentials:
    vault = MemoryCredentials()
    monkeypatch.setattr(credentials, "_native_backend", lambda: vault)
    return vault


@pytest.fixture(autouse=True)
def no_real_desktop_records(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests never read the real Claude desktop app's session records.
    monkeypatch.setattr(claude, "desktop_dir", lambda: None)
