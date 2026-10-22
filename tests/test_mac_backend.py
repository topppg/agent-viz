"""The Mac app joins only dashboards it can verify through their private data directory."""

import contextlib
import json
import runpy
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import backend
from spiritboard import cli, launcher
from spiritboard.launcher import Instance

BACKEND = Path(__file__).resolve().parents[1] / "src" / "app" / "backend.py"


@contextlib.contextmanager
def health_server(instance_id: str, port: int = 0) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = json.dumps(
                {"ok": True, "service": "spiritboard", "instance_id": instance_id}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@contextlib.contextmanager
def dashboard(directory: Path, health_id: str | None = None) -> Iterator[str]:
    """The data directory's lock owner plus a health endpoint answering as ``health_id``."""
    published_id = uuid.uuid4().hex
    with health_server(health_id or published_id) as url, Instance(directory, {}) as owner:
        owner.publish(url, published_id)
        yield url


def test_forged_health_on_standard_port_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    serve = MagicMock(return_value=0)
    with contextlib.ExitStack() as stack:
        # Impersonate the standard port when it is free; a dashboard may already use it.
        try:
            stack.enter_context(health_server(uuid.uuid4().hex, 8766))
        except OSError:
            stack.enter_context(health_server(uuid.uuid4().hex))
        stack.enter_context(patch.object(cli, "main", serve))
        stack.enter_context(patch("sys.argv", [str(BACKEND)]))
        with pytest.raises(SystemExit) as exited:
            runpy.run_path(str(BACKEND), run_name="__main__")
    assert exited.value.code == 0
    serve.assert_called_once_with(["serve", "--no-open", "--port", "0"])
    assert "already running" not in capsys.readouterr().out


def test_selected_directory_shows_its_verified_dashboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    serve = MagicMock(return_value=0)
    with dashboard(tmp_path / "board") as url, patch.object(cli, "main", serve):
        assert backend.main(["--data-dir", str(tmp_path / "board")]) == 0
    assert capsys.readouterr().out == f"Spiritboard is already running: {url}\n"
    serve.assert_not_called()


def test_selected_directory_with_wrong_identity_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    serve = MagicMock(return_value=0)
    with (
        dashboard(tmp_path / "board", health_id=uuid.uuid4().hex),
        patch.object(cli, "main", serve),
        patch.object(launcher, "_STARTUP_WAIT", 0.3),
    ):
        assert backend.main(["--data-dir", str(tmp_path / "board")]) == 1
    assert capsys.readouterr().out.startswith("Spiritboard could not connect: ")
    serve.assert_not_called()


def test_selected_directory_without_dashboard_never_starts_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    serve = MagicMock(return_value=0)
    with patch.object(cli, "main", serve):
        assert backend.main(["--data-dir", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().out == (
        "Spiritboard could not connect: "
        "No dashboard is running with this data directory. Start one first.\n"
    )
    serve.assert_not_called()
    assert not (tmp_path / "missing").exists()


def test_default_directory_reuses_its_verified_dashboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    serve = MagicMock(return_value=0)
    with (
        dashboard(tmp_path / "default") as url,
        patch.object(backend, "default_data_dir", return_value=tmp_path / "default"),
        patch.object(cli, "main", serve),
    ):
        assert backend.main([]) == 0
    assert capsys.readouterr().out == f"Spiritboard is already running: {url}\n"
    serve.assert_not_called()
