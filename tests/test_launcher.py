import contextlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from spiritboard.launcher import Instance, LaunchError, default_data_dir


@contextlib.contextmanager
def health_server(instance_id, *, status=200, service="spiritboard", redirect=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths.append(self.path)
            payload = json.dumps(
                {"ok": True, "service": service, "instance_id": instance_id}
            ).encode()
            self.send_response(status)
            if redirect:
                self.send_header("Location", redirect)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class DataDirectoryTests(unittest.TestCase):
    def test_mac_user_data_directory(self):
        with patch("spiritboard.launcher.sys.platform", "darwin"):
            with patch("spiritboard.launcher.Path.home", return_value=Path("/synthetic-home")):
                self.assertEqual(
                    default_data_dir(),
                    Path("/synthetic-home/Library/Application Support/spiritboard"),
                )
                self.assertEqual(default_data_dir(demo=True), default_data_dir() / "demo")

    def test_linux_absolute_xdg_and_default(self):
        configured_root = Path(tempfile.gettempdir()).resolve() / "synthetic-data"
        with patch("spiritboard.launcher.sys.platform", "linux"):
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(configured_root)}):
                self.assertEqual(default_data_dir(), configured_root / "spiritboard")
            for configured in ("", "relative-is-not-safe"):
                with self.subTest(configured=configured):
                    with patch.dict(os.environ, {"XDG_DATA_HOME": configured}):
                        self.assertEqual(
                            default_data_dir(), Path.home() / ".local/share/spiritboard"
                        )

    def test_windows_local_data_and_default(self):
        configured_root = Path(tempfile.gettempdir()).resolve() / "synthetic-local"
        with patch("spiritboard.launcher.sys.platform", "win32"):
            with patch.dict(os.environ, {"LOCALAPPDATA": str(configured_root)}):
                self.assertEqual(default_data_dir(), configured_root / "spiritboard")
            for configured in ("", "relative-is-not-safe"):
                with patch.dict(os.environ, {"LOCALAPPDATA": configured}):
                    self.assertEqual(default_data_dir(), Path.home() / "AppData/Local/spiritboard")


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "state"
        self.settings = {"demo": False, "summaries": False, "port": 8766}
        self.instance_id = uuid.uuid4().hex
        self.timeout = patch("spiritboard.launcher._STARTUP_WAIT", 0.15)
        self.timeout.start()

    def tearDown(self):
        self.timeout.stop()
        self.temporary.cleanup()

    def write_marker(self, contents):
        marker = self.directory / "instance.json"
        marker.write_text(contents)
        marker.chmod(0o600)

    def test_owner_publishes_private_marker_and_releases_without_removing_lock(self):
        with Instance(self.directory, self.settings) as instance:
            self.assertTrue(instance.owner)
            instance.publish("http://127.0.0.1:8766", self.instance_id)
            metadata = json.loads((self.directory / "instance.json").read_text())
            self.assertEqual(metadata["settings"], self.settings)
            self.assertEqual(metadata["instance_id"], self.instance_id)
            if os.name == "posix":
                for filename in ("instance.json", "instance.lock"):
                    self.assertEqual(
                        stat.S_IMODE((self.directory / filename).stat().st_mode), 0o600
                    )
                self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertFalse(instance.owner)
        self.assertFalse((self.directory / "instance.json").exists())
        self.assertTrue((self.directory / "instance.lock").is_file())
        with Instance(self.directory, self.settings) as later:
            self.assertTrue(later.owner)

    def test_verified_existing_process_is_reused_without_proxy(self):
        with health_server(self.instance_id) as (server, url):
            with Instance(self.directory, self.settings) as owner:
                owner.publish(url, self.instance_id)
                with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                    with Instance(self.directory, self.settings) as client:
                        self.assertFalse(client.owner)
                        self.assertEqual(client.existing_url(), url)
                self.assertEqual(server.paths, ["/api/health"])
                self.assertTrue((self.directory / "instance.json").exists())

    def test_settings_conflict_does_not_reuse_or_remove_owner_state(self):
        with health_server(self.instance_id) as (_, url):
            with Instance(self.directory, self.settings) as owner:
                owner.publish(url, self.instance_id)
                with Instance(self.directory, {**self.settings, "summaries": True}) as client:
                    with self.assertRaisesRegex(LaunchError, "different options"):
                        client.existing_url()
                self.assertTrue((self.directory / "instance.json").exists())

    def test_boolean_and_numeric_settings_are_not_treated_as_identical(self):
        with health_server(self.instance_id) as (_, url):
            with Instance(self.directory, {"flag": True}) as owner:
                owner.publish(url, self.instance_id)
                with Instance(self.directory, {"flag": 1}) as client:
                    with self.assertRaisesRegex(LaunchError, "different options"):
                        client.existing_url()

    def test_subprocess_contention_and_crash_recovery(self):
        script = (
            "import sys, uuid\n"
            "from pathlib import Path\n"
            "from spiritboard.launcher import Instance\n"
            "with Instance(Path(sys.argv[1]), {}) as instance:\n"
            "    instance.publish('http://127.0.0.1:8766', uuid.uuid4().hex)\n"
            "    print('owner' if instance.owner else 'client', flush=True)\n"
            "    sys.stdin.buffer.read(1)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.directory)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "owner")
            with Instance(self.directory, {}) as blocked:
                self.assertFalse(blocked.owner)
            process.kill()
            process.wait(timeout=5)
            self.assertTrue((self.directory / "instance.json").exists())
            with Instance(self.directory, {}) as recovered:
                self.assertTrue(recovered.owner)
                recovered.publish("http://127.0.0.1:8767", self.instance_id)
            self.assertFalse((self.directory / "instance.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_client_waits_for_initial_publish(self):
        with health_server(self.instance_id) as (_, url):
            with Instance(self.directory, self.settings) as owner:
                published = threading.Event()

                def publish_later():
                    time.sleep(0.03)
                    owner.publish(url, self.instance_id)
                    published.set()

                thread = threading.Thread(target=publish_later)
                thread.start()
                try:
                    with Instance(self.directory, self.settings) as client:
                        self.assertEqual(client.existing_url(), url)
                    self.assertTrue(published.is_set())
                finally:
                    thread.join(2)

    def test_identity_mismatch_service_mismatch_and_redirect_are_not_reused(self):
        cases = [
            {"instance_id": uuid.uuid4().hex},
            {"instance_id": self.instance_id, "service": "another-app"},
            {
                "instance_id": self.instance_id,
                "status": 302,
                "redirect": "http://127.0.0.1:1/private",
            },
        ]
        for options in cases:
            with self.subTest(options=options):
                with health_server(**options) as (_, url):
                    with Instance(self.directory, self.settings) as owner:
                        owner.publish(url, self.instance_id)
                        with Instance(self.directory, self.settings) as client:
                            with self.assertRaisesRegex(LaunchError, "could not be verified"):
                                client.existing_url()

    def test_invalid_and_oversized_marker_are_bounded(self):
        with Instance(self.directory, self.settings):
            for contents in ("not json", "[]", "{}", "x" * 8193):
                with self.subTest(contents=contents[:10]):
                    self.write_marker(contents)
                    with Instance(self.directory, self.settings) as client:
                        with self.assertRaisesRegex(LaunchError, "could not be verified"):
                            client.existing_url()

    def test_stale_marker_is_ignored_when_lock_can_be_owned(self):
        self.directory.mkdir()
        self.write_marker("not valid metadata")
        with Instance(self.directory, self.settings) as owner:
            self.assertTrue(owner.owner)
            owner.publish("http://127.0.0.1:8766", self.instance_id)

    def test_only_owner_can_publish(self):
        with Instance(self.directory, self.settings):
            with Instance(self.directory, self.settings) as client:
                with self.assertRaisesRegex(LaunchError, "Only the dashboard lock owner"):
                    client.publish("http://127.0.0.1:8766", self.instance_id)
        with self.assertRaises(LaunchError):
            client.publish("http://127.0.0.1:8766", self.instance_id)

    def test_owner_cannot_reuse_itself(self):
        with Instance(self.directory, self.settings) as owner:
            with self.assertRaises(LaunchError):
                owner.existing_url()

    def test_owner_does_not_remove_someone_elses_marker(self):
        with Instance(self.directory, self.settings) as owner:
            owner.publish("http://127.0.0.1:8766", self.instance_id)
            metadata = json.loads((self.directory / "instance.json").read_text())
            metadata["instance_id"] = uuid.uuid4().hex
            self.write_marker(json.dumps(metadata))
        self.assertTrue((self.directory / "instance.json").is_file())

    def test_no_private_configuration_is_in_error_messages(self):
        secret = "synthetic-private-value"
        with self.assertRaises(LaunchError) as caught:
            Instance(self.directory, {secret: object()})
        self.assertNotIn(secret, str(caught.exception))

    def test_publish_rejects_remote_ambiguous_or_nonroot_urls(self):
        urls = [
            "https://127.0.0.1:8766",
            "http://localhost:8766",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:08766",
            "http://user@127.0.0.1:8766",
            "http://127.0.0.1:8766/private",
            "http://127.0.0.1:8766?key=value",
            "http://127.0.0.1:8766#fragment",
            "http://example.test:8766",
            "http://[::1]:8766",
        ]
        with Instance(self.directory, self.settings) as owner:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(LaunchError):
                    owner.publish(url, self.instance_id)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_special_lock_and_marker_never_block(self):
        self.directory.mkdir()
        lock = self.directory / "instance.lock"
        os.mkfifo(lock, 0o600)
        with self.assertRaises(LaunchError):
            with Instance(self.directory, self.settings):
                self.fail("FIFO lock was accepted")
        lock.unlink()
        with Instance(self.directory, self.settings):
            os.mkfifo(self.directory / "instance.json", 0o600)
            with Instance(self.directory, self.settings) as client:
                started = time.monotonic()
                with self.assertRaises(LaunchError):
                    client.existing_url()
                self.assertLess(time.monotonic() - started, 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions and links")
    def test_symbolic_link_and_hardlinked_lock_are_rejected_without_target_changes(self):
        self.directory.mkdir()
        target = Path(self.temporary.name) / "target"
        target.write_text("do not change")
        target.chmod(0o600)
        lock = self.directory / "instance.lock"
        for hard in (False, True):
            with self.subTest(hard=hard):
                if hard:
                    os.link(target, lock)
                else:
                    lock.symlink_to(target)
                with self.assertRaises(LaunchError):
                    with Instance(self.directory, self.settings):
                        self.fail("Linked lock was accepted")
                self.assertEqual(target.read_text(), "do not change")
                lock.unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX permissions and links")
    def test_marker_symlink_and_public_permissions_are_not_trusted(self):
        target = Path(self.temporary.name) / "target"
        target.write_text("do not change")
        with Instance(self.directory, self.settings):
            marker = self.directory / "instance.json"
            marker.symlink_to(target)
            with Instance(self.directory, self.settings) as client:
                with self.assertRaises(LaunchError):
                    client.existing_url()
            marker.unlink()
            self.write_marker("{}")
            marker.chmod(0o644)
            with Instance(self.directory, self.settings) as client:
                with self.assertRaises(LaunchError):
                    client.existing_url()
        self.assertEqual(target.read_text(), "do not change")

    @unittest.skipUnless(os.name == "posix", "Symbolic links required")
    def test_data_directory_symlink_is_rejected(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(LaunchError):
            with Instance(self.directory, self.settings):
                self.fail("Directory link was accepted")
        self.assertEqual(list(target.iterdir()), [])

    def test_trickling_untrusted_service_has_a_total_deadline(self):
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        stopped = threading.Event()

        def trickle():
            connection, _ = listening.accept()
            with connection:
                connection.recv(4096)
                while not stopped.wait(0.02):
                    try:
                        connection.sendall(b"H")
                    except OSError:
                        return

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        try:
            with Instance(self.directory, self.settings) as owner:
                owner.publish(f"http://127.0.0.1:{listening.getsockname()[1]}", self.instance_id)
                with Instance(self.directory, self.settings) as client:
                    started = time.monotonic()
                    with self.assertRaises(LaunchError):
                        client.existing_url()
                    self.assertLess(time.monotonic() - started, 1)
        finally:
            stopped.set()
            listening.close()
            thread.join(2)
