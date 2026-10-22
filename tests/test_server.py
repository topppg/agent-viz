import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus
from spiritboard.cli import main, token_for, write_private
from spiritboard.server import ASSETS, Collector
from spiritboard.store import Store

TOKEN = "x" * 40


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.server = Collector(self.store, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.stop()
        self.thread.join(2)
        self.store.close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def event(self):
        return dict(
            schema_version=1,
            event_id="event",
            session_id="session",
            run_id="run",
            producer_id="producer",
            sequence=1,
            occurred_at="2026-10-20T10:00:00Z",
            type="run.started",
            payload={"name": "<script>not executable</script>"},
        )

    def post(self, raw, token=TOKEN, headers=None):
        return self.request(
            "POST",
            "/api/events",
            raw,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                **(headers or {}),
            },
        )

    def test_health_assets_and_empty_snapshot(self):
        for path in (*ASSETS, "/api/health", "/api/snapshot"):
            status, headers, data = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertTrue(data)
        snapshot = json.loads(self.request("GET", "/api/snapshot")[2])
        self.assertEqual(snapshot["runs"], [])

    def test_loopback_startup_does_not_need_a_dns_lookup(self):
        with patch("socket.getfqdn", side_effect=AssertionError("No DNS needed for loopback")):
            server = Collector(self.store, TOKEN)
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()

    def test_ingestion_auth_schema_dedup_and_history(self):
        raw = json.dumps(self.event())
        self.assertEqual(self.post(raw, token="wrong")[0], 401)
        self.assertEqual(self.post(raw, token="é" * 40)[0], 401)
        self.assertEqual(self.post(raw)[0], 200)
        duplicate = json.loads(self.post(raw)[2])
        self.assertTrue(duplicate["duplicate"])
        snapshot = json.loads(self.request("GET", "/api/snapshot")[2])
        self.assertEqual(len(snapshot["runs"]), 1)
        self.assertEqual(snapshot["runs"][0]["name"], "<script>not executable</script>")
        history = json.loads(self.request("GET", "/api/runs/run/events?limit=1")[2])
        self.assertEqual(len(history["events"]), 1)
        self.assertFalse(history["has_more"])
        self.assertEqual(self.post(b'{"type": "x"}')[0], 400)
        changed = self.event()
        changed["payload"]["name"] = "different"
        self.assertEqual(self.post(json.dumps(changed))[0], 400)

    def test_host_origin_fetch_site_and_no_cors(self):
        for headers in (
            {"Host": "evil.invalid"},
            {"Origin": "https://evil.invalid"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            self.assertEqual(self.request("GET", "/api/snapshot", headers=headers)[0], 403)
            self.assertEqual(self.post(json.dumps(self.event()), headers=headers)[0], 403)
        response = self.request("GET", "/api/health")
        self.assertNotIn("Access-Control-Allow-Origin", response[1])

    def test_bounded_strict_json_and_routes(self):
        self.assertEqual(self.post("x" * 32769)[0], 400)
        self.assertEqual(self.post('{"type":"run.started","type":"run.finished"}')[0], 400)
        self.assertEqual(self.post('{"x":NaN}')[0], 400)
        self.assertEqual(self.request("GET", "/api/runs/run/events?after=wrong")[0], 400)
        self.assertEqual(self.request("GET", "/../pyproject.toml")[0], 404)
        self.assertEqual(self.request("GET", "/api/events?after=-1")[0], 400)
        self.assertEqual(self.request("GET", "/api/runs/run/events?after=" + "9" * 100)[0], 400)
        self.assertEqual(self.request("GET", "/api/events?after=" + "9" * 100)[0], 400)

    def test_initial_and_reset_sse_snapshots(self):
        for cursor, kind in ((0, "snapshot"), (999, "reset")):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            connection.request("GET", f"/api/events?after={cursor}")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            lines = [response.readline().decode().strip() for _ in range(4)]
            self.assertEqual(lines[1], f"event: {kind}")
            self.assertEqual(json.loads(lines[2].removeprefix("data: "))["runs"], [])
            response.close()
            connection.close()

    def test_sse_last_event_id_wins_over_initial_query(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/api/events?after=0", headers={"Last-Event-ID": "999"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.readline()
        self.assertEqual(response.readline().decode().strip(), "event: reset")
        response.close()
        connection.close()

    def json_get(self, path):
        status, _, body = self.request("GET", path, headers={"Host": self.host})
        return status, json.loads(body)

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def test_activity_windows_are_bounded(self):
        for window, bucket, count in (
            (600, 10, 60),
            (3600, 60, 60),
            (86400, 1200, 72),
            (604800, 7200, 84),
            (2592000, 28800, 90),
        ):
            status, data = self.json_get(f"/api/activity?window={window}")
            self.assertEqual(status, 200)
            self.assertEqual((data["bucket_seconds"], data["buckets"]), (bucket, count))
        status, data = self.json_get("/api/activity")
        self.assertEqual((status, data["buckets"]), (200, 60))
        for query in ("window=59", "window=2592001", "window=abc", "window=600&x=1", "window=1e3"):
            self.assertEqual(self.json_get(f"/api/activity?{query}")[0], 400, query)

    def test_past_snapshot_validates_the_moment(self):
        self.assertEqual(self.post(json.dumps(self.event()))[0], 200)
        current = time.time()
        status, data = self.json_get(f"/api/snapshot?at={current - 60}")
        self.assertEqual(status, 200)
        self.assertEqual(data["runs"], [])
        self.assertIn("at", data)
        self.assertEqual(data["activity"]["buckets"], 60)
        status, data = self.json_get(f"/api/snapshot?at={current + 0.5}")
        self.assertEqual(status, 200)
        self.assertEqual([r["run_id"] for r in data["runs"]], ["run"])
        iso = datetime.fromtimestamp(current - 3600, UTC).isoformat().replace("+00:00", "Z")
        self.assertEqual(self.json_get(f"/api/snapshot?at={iso}")[0], 200)
        for at in (
            current + 120,
            current - 31 * 86400,
            "nan",
            "inf",
            "yesterday",
            "2026-10-20T10:00:00",
            "1" * 80,
        ):
            self.assertEqual(self.json_get(f"/api/snapshot?at={at}")[0], 400, at)
        self.assertEqual(self.json_get("/api/snapshot?at=1&at=2")[0], 400)
        self.server.retention_days = 0
        self.assertEqual(self.json_get(f"/api/snapshot?at={current - 31 * 86400}")[0], 200)

    def focus_post(self, headers=None, body=None):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        return self.request(
            "POST",
            "/api/runs/run/focus",
            body,
            {"Origin": origin, "X-Spiritboard": "focus", **(headers or {})},
        )

    def rename_post(self, body, headers=None, run="run"):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        raw = body if isinstance(body, str) else json.dumps(body)
        return self.request(
            "POST",
            f"/api/runs/{run}/name",
            raw,
            {
                "Origin": origin,
                "X-Spiritboard": "rename",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )

    def test_rename_sets_and_clears_a_local_name(self):
        self.assertEqual(self.post(json.dumps(self.event()))[0], 200)
        status, _, data = self.rename_post({"name": "  Retry\twork  "})
        self.assertEqual(
            (status, json.loads(data)), (200, {"ok": True, "local_name": "Retry work"})
        )
        run = self.json_get("/api/snapshot")[1]["runs"][0]
        self.assertEqual(run["local_name"], "Retry work")
        status, _, data = self.rename_post({"name": " "})
        self.assertEqual(json.loads(data), {"ok": True, "local_name": None})
        self.assertNotIn("local_name", self.json_get("/api/snapshot")[1]["runs"][0])

    def test_rename_requires_dashboard_origin_and_a_valid_name(self):
        self.assertEqual(self.post(json.dumps(self.event()))[0], 200)
        self.assertEqual(self.rename_post({"name": "x"}, {"X-Spiritboard": "focus"})[0], 403)
        self.assertEqual(
            self.rename_post({"name": "x"}, {"Origin": "https://evil.invalid"})[0], 403
        )
        self.assertEqual(self.rename_post({"name": "x"}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        for body in ({"name": "x" * 121}, {"name": "a\x00b"}, {"name": 7}, {"other": 1}, "[]", "{"):
            with self.subTest(body=body):
                self.assertEqual(self.rename_post(body)[0], 400)
        self.assertEqual(self.rename_post({"name": "x"}, run="missing")[0], 404)
        self.assertNotIn("local_name", self.json_get("/api/snapshot")[1]["runs"][0])

    def test_focus_lookup_is_lazy_and_sdk_runs_are_unsupported(self):
        self.assertEqual(self.post(json.dumps(self.event()))[0], 200)
        status, _, data = self.request("GET", "/api/runs/run/focus")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"available": False, "kind": "unsupported", "label": ""})
        self.assertEqual(
            json.loads(self.request("GET", "/api/runs/missing/focus")[2])["kind"], "unsupported"
        )
        self.assertEqual(
            self.request("GET", "/api/runs/run/focus", headers={"Host": "evil.invalid"})[0], 403
        )
        self.assertEqual(self.request("GET", "/api/runs/" + "x" * 300 + "/focus")[0], 400)
        with patch("spiritboard.focus.resolve") as resolve:
            self.request("GET", "/api/snapshot")
        resolve.assert_not_called()

    def test_focus_returns_public_target_only(self):
        target = focus.Target(
            "resume",
            "resume",
            command="claude --resume invented-0000",
            steps=(focus.Step(("/secret/helper",)),),
        )
        with patch("spiritboard.focus.resolve", return_value=target):
            data = json.loads(self.request("GET", "/api/runs/run/focus")[2])
        self.assertEqual(
            data,
            {
                "available": True,
                "kind": "resume",
                "label": "resume",
                "command": "claude --resume invented-0000",
            },
        )

    def test_focus_post_requires_origin_header_and_no_body(self):
        target = focus.Target("focus", "cmux", steps=(focus.Step(("/invented/cmux",)),))
        with (
            patch("spiritboard.focus.resolve", return_value=target),
            patch("spiritboard.focus.focus", return_value=True) as perform,
        ):
            self.assertEqual(self.focus_post({"X-Spiritboard": "other"})[0], 403)
            self.assertEqual(self.focus_post({"Origin": "https://evil.invalid"})[0], 403)
            self.assertEqual(self.focus_post({"Sec-Fetch-Site": "cross-site"})[0], 403)
            headers = {"X-Spiritboard": "focus"}
            no_origin = self.request("POST", "/api/runs/run/focus", None, headers)
            self.assertEqual(no_origin[0], 403)
            self.assertEqual(self.focus_post(body="x")[0], 400)
            perform.assert_not_called()
            status, _, data = self.focus_post()
            self.assertEqual((status, json.loads(data)), (200, {"ok": True}))
            perform.assert_called_once_with(target)
        with (
            patch("spiritboard.focus.resolve", return_value=target),
            patch("spiritboard.focus.focus", return_value=False),
        ):
            status, _, data = self.focus_post()
            self.assertEqual((status, json.loads(data)), (502, {"error": "could not open the app"}))
        resume = focus.Target("resume", "resume", command="codex resume x")
        with (
            patch("spiritboard.focus.resolve", return_value=resume),
            patch("spiritboard.focus.focus") as perform,
        ):
            self.assertEqual(self.focus_post()[0], 409)
            perform.assert_not_called()


class LocalConfigTests(unittest.TestCase):
    def test_tokens_private_stable_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "owned"
            token = token_for(directory)
            self.assertEqual(token_for(directory), token)
            self.assertGreaterEqual(len(token), 24)
            self.assertEqual((directory / "token").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_private(directory / "token", "overwrite")

    def test_symlink_token_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "actual").write_text(TOKEN)
            (directory / "token").symlink_to(directory / "actual")
            with self.assertRaises(ValueError):
                token_for(directory)

    def test_no_nonloopback_bind(self):
        store = Store()
        try:
            with self.assertRaises(ValueError):
                Collector(store, TOKEN, host="0.0.0.0")
        finally:
            store.close()

    def test_cli_argument_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["serve", "--port", "-1", "--data-dir", folder]), 1)
            self.assertEqual(main(["serve", "--stale-after", "nan", "--data-dir", folder]), 1)
            self.assertEqual(main(["serve", "--retention-days", "-1", "--data-dir", folder]), 1)

    def test_discover_label_selection_writes_only_selected_profiles(self):
        from spiritboard.sources import SourceProfile

        profiles = [
            SourceProfile("personal", "codex", "Personal", "/example/personal"),
            SourceProfile("work", "codex", "Work", "/example/work"),
        ]
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("spiritboard.cli.discover_profiles", return_value=profiles),
        ):
            path = Path(folder) / "profiles.json"
            self.assertEqual(main(["discover", "--label", "Work", "--write-config", str(path)]), 0)
            entries = json.loads(path.read_text())["profiles"]
            self.assertEqual([p["id"] for p in entries], ["work"])
            self.assertEqual(main(["discover", "--label", "Missing"]), 1)


if __name__ == "__main__":
    unittest.main()


class LivenessSnapshotTests(unittest.TestCase):
    def test_runs_carry_checked_liveness(self):
        from spiritboard.model import now

        class Monitor:
            profiles = []

            def public_state(self):
                return {
                    "profiles": [
                        {"id": "cl", "kind": "claude", "label": "Claude", "include_text": True},
                        {"id": "cx", "kind": "codex", "label": "Codex", "include_text": True},
                        {"id": "cu", "kind": "codex", "label": "Unknown", "include_text": True},
                    ],
                    "source_errors": {},
                    "source_stats": [],
                    "automatic_discovery": False,
                    "live": {"cl": ["s1"], "cx": ["t1"], "cu": None},
                }

        store = Store()
        self.addCleanup(store.close)
        base = {
            "source": "claude",
            "account": "a",
            "name": "n",
            "state": "idle",
            "last_seen": now(),
        }
        store.observe({**base, "run_id": "cl:s1", "session_id": "cl:observed"})
        store.observe(
            {
                **base,
                "run_id": "cl:s1:agent:x",
                "session_id": "cl:observed",
                "parent_run_id": "cl:s1",
            }
        )
        store.observe({**base, "run_id": "cl:s2", "session_id": "cl:observed"})
        desktop = {**base, "run_id": "cl:d1", "session_id": "cl:observed", "app": "Claude desktop"}
        store.observe(desktop)
        codex = {**base, "source": "codex"}
        store.observe({**codex, "run_id": "cx:t1", "session_id": "cx:observed"})
        store.observe({**codex, "run_id": "cx:t2", "session_id": "cx:observed"})
        store.observe({**codex, "run_id": "cu:t3", "session_id": "cu:observed"})
        server = Collector(store, TOKEN, monitor=Monitor())
        self.addCleanup(server.server_close)
        live = {run["run_id"]: run.get("live", "absent") for run in server.snapshot()["runs"]}
        self.assertEqual(
            live,
            {
                "cl:s1": True,
                "cl:s1:agent:x": True,
                "cl:s2": False,
                "cl:d1": None,
                "cx:t1": True,
                "cx:t2": False,
                "cu:t3": None,
            },
        )
        past = server.snapshot(time.time())
        self.assertEqual({run.get("live", "absent") for run in past["runs"]}, {None})
