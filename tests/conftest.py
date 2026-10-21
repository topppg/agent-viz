import pytest

from spiritboard.harnesses import claude


@pytest.fixture(autouse=True)
def no_real_desktop_records(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests never read the real Claude desktop app's session records.
    monkeypatch.setattr(claude, "desktop_dir", lambda: None)
