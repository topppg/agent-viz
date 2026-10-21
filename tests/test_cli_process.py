"""Synthetic end-to-end launches from outside a source checkout."""

import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit


class CliProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.environment = os.environ.copy()
        for key in ("OPENROUTER_API_KEY", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            self.environment.pop(key, None)
        self.environment.update(
            HOME=str(self.root),
            USERPROFILE=str(self.root),
            LOCALAPPDATA=str(self.root / "data"),
            XDG_DATA_HOME=str(self.root / "data"),
        )
        self.data = (
            self.root / "Library" / "Application Support" / "spiritboard"
            if sys.platform == "darwin"
            else self.root / "data" / "spiritboard"
        )

    def command(self, *arguments: str) -> list[str]:
        return [sys.executable, "-m", "spiritboard", *arguments]

    def launch(self, *arguments: str) -> subprocess.Popen:
        process = subprocess.Popen(
            self.command(*arguments),
            cwd=self.first,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process: subprocess.Popen) -> str:
        if process.poll() is None:
            if os.name == "posix":
                process.send_signal(signal.SIGINT)
            else:
                process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=2)
        return output

    def request(self, url: str, path: str) -> tuple[int, bytes]:
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=1)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def ready(self, process: subprocess.Popen, data: Path) -> dict:
        deadline = time.monotonic() + 8
        last_error = "no response"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"Dashboard exited before readiness: {process.communicate()[0]}")
            try:
                metadata = json.loads((data / "instance.json").read_text())
                status, body = self.request(metadata["url"], "/api/health")
                if status == 200 and json.loads(body)["instance_id"] == metadata["instance_id"]:
                    return metadata
            except (OSError, ValueError, KeyError) as error:
                last_error = type(error).__name__
            time.sleep(0.05)
        output = self.stop(process)
        self.fail(f"Synthetic collector did not become ready ({last_error}). Output: {output}")

    def test_default_storage_reuse_assets_conflict_prune_and_restart(self):
        arguments = ["--no-discovery", "--no-open", "--port", "0"]
        process = self.launch(*arguments)
        metadata = self.ready(process, self.data)
        for path in ("/", "/app.js", "/style.css"):
            status, body = self.request(metadata["url"], path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
        snapshot = json.loads(self.request(metadata["url"], "/api/snapshot")[1])
        self.assertEqual(snapshot["runs"], [])
        self.assertFalse(snapshot["ai"]["enabled"])
        self.assertEqual(snapshot["profiles"], [])

        second = subprocess.run(
            self.command(*arguments),
            cwd=self.second,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already running", second.stdout)
        self.assertIn(metadata["url"], second.stdout)
        self.assertEqual(list(self.first.iterdir()), [])
        self.assertEqual(list(self.second.iterdir()), [])
        self.assertIsNone(process.poll())

        for extra in (["--stale-after", "20"], ["--summaries"]):
            conflict = subprocess.run(
                self.command(*arguments, *extra),
                cwd=self.second,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(conflict.returncode, 1)
            if "different options" not in conflict.stdout:
                try:
                    health = self.request(metadata["url"], "/api/health")
                except OSError as error:
                    health = type(error).__name__
                owner_output = self.stop(process)
                self.fail(
                    f"Conflict {extra!r}: {conflict.stdout!r}; health={health!r}; "
                    f"owner output={owner_output!r}"
                )
            self.assertIn("different options", conflict.stdout)
            self.assertNotIn("Traceback", conflict.stderr)
        prune = subprocess.run(
            self.command("prune", "--older-than-days", "7"),
            cwd=self.second,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(prune.returncode, 1)
        self.assertIn("Stop the dashboard", prune.stdout)
        self.stop(process)
        if os.name == "posix":
            self.assertEqual(process.returncode, 0)
            self.assertFalse((self.data / "instance.json").exists())
        restarted = self.launch(*arguments)
        self.assertNotEqual(
            self.ready(restarted, self.data)["instance_id"], metadata["instance_id"]
        )

    def test_demo_uses_separate_history_without_real_profiles_or_model_calls(self):
        process = self.launch("--demo", "--demo-delay", "0", "--no-open", "--port", "0")
        metadata = self.ready(process, self.data / "demo")
        deadline = time.monotonic() + 5
        snapshot = {}
        while time.monotonic() < deadline:
            snapshot = json.loads(self.request(metadata["url"], "/api/snapshot")[1])
            if snapshot["total_runs"] == 5:
                break
            time.sleep(0.05)
        self.assertEqual(snapshot["total_runs"], 5)
        self.assertTrue(all(run["demo"] for run in snapshot["runs"]))
        self.assertFalse(snapshot["ai"]["enabled"])
        self.assertEqual(snapshot["profiles"], [])
        self.assertFalse((self.data / "events.sqlite3").exists())

    def test_editing_the_same_source_config_requires_a_restart(self):
        profile_root = self.root / "synthetic-profile"
        (profile_root / "sessions").mkdir(parents=True)
        profile = {
            "id": "synthetic",
            "kind": "codex",
            "label": "Invented source",
            "root": str(profile_root),
            "include_text": True,
        }
        config = self.root / "sources.json"
        config.write_text(json.dumps({"profiles": [profile]}))
        arguments = ["--config", str(config), "--no-open", "--port", "0"]
        process = self.launch(*arguments)
        metadata = self.ready(process, self.data)
        profile["include_text"] = False
        config.write_text(json.dumps({"profiles": [profile]}))
        changed = subprocess.run(
            self.command(*arguments),
            cwd=self.second,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(changed.returncode, 1, changed.stdout + changed.stderr)
        self.assertIn("different options", changed.stdout)
        self.assertEqual(self.request(metadata["url"], "/api/health")[0], 200)
