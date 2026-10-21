import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from spiritboard.sources import SourceProfile, codex_titles
from spiritboard.summaries import (
    MODEL,
    Summarizer,
    _clean,
    _goal,
    _goal_text,
    _request,
)


class SummariesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Mock()
        self.store.snapshot.return_value = {"runs": [self.make_run()]}
        self.instances = []

    def tearDown(self):
        for instance in self.instances:
            instance.stop()
            instance.db.close()
        self.temp.cleanup()

    def make_run(self, **overrides):
        from datetime import datetime

        result = {
            "run_id": "private-native-id",
            "parent_run_id": None,
            "source": "sdk",
            "state": "working",
            "last_seen": datetime.now(UTC).isoformat(),
            "task": "Fix the retry test",
            "activity": "Reading test expectations",
        }
        result.update(overrides)
        return result

    def summarizer(self, **kwargs):
        instance = Summarizer(lambda: [], self.store, "test-key-not-real", **kwargs)
        self.instances.append(instance)
        return instance

    def response(self, content=None, cost=0.00012):
        response = {
            "id": "gen-test-1",
            "choices": [
                {
                    "message": {
                        "content": content
                        or json.dumps({"r1": "Investigating retry-test expectations"})
                    }
                }
            ],
            "usage": {},
        }
        if cost is not None:
            response["usage"]["cost"] = cost
        return response

    def age_calls(self, instance):
        instance.db.execute("UPDATE calls SET started=?", (time.time() - 125,))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires named pipes")
    def test_codex_title_reader_does_not_block_on_fifo(self):
        path = self.root / "session_index.jsonl"
        os.mkfifo(path)
        code = (
            "import sys; from spiritboard.sources import SourceProfile; "
            "from pathlib import Path; from spiritboard.sources import codex_titles; "
            "assert codex_titles(Path(sys.argv[1])) == {}"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.root)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(writer, b'{"id":"synthetic","thread_name":"Pipe title"}\n')
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.root)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(writer)

    def test_codex_title_reader_checks_descriptor_type_before_reading(self):
        profile = SourceProfile("one", "codex", "Test", str(self.root))
        stream = MagicMock()
        stream.read.side_effect = AssertionError("Non-regular file must not be read")
        stream.seek.side_effect = AssertionError("Non-regular file must not be sought")
        opened = MagicMock()
        opened.__enter__.return_value = stream
        with (
            patch("spiritboard.sources.os.open", return_value=123) as open_file,
            patch("spiritboard.sources.os.fdopen", return_value=opened),
            patch(
                "spiritboard.sources.os.fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFIFO, st_size=100),
            ),
        ):
            self.assertEqual(codex_titles(Path(profile.root)), {})
        self.assertTrue(open_file.call_args.args[1] & os.O_NONBLOCK)
        stream.read.assert_not_called()
        stream.seek.assert_not_called()

    def test_codex_title_reader_rejects_directories_and_symlinks(self):
        profile = SourceProfile("one", "codex", "Test", str(self.root))
        path = self.root / "session_index.jsonl"
        path.mkdir()
        self.assertEqual(codex_titles(Path(profile.root)), {})
        path.rmdir()
        target = self.root / "target.jsonl"
        target.write_text('{"id":"synthetic","thread_name":"Linked title"}\n')
        path.symlink_to(target)
        self.assertEqual(codex_titles(Path(profile.root)), {})

    def test_success_actual_cost_and_opaque_ids(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response()) as request:
            instance.tick()
        state = instance.public_state()
        self.assertEqual(state["calls"], 1)
        self.assertAlmostEqual(state["actual_cost_usd"], 0.00012)
        self.assertEqual(state["summaries"][0]["run_id"], "private-native-id")
        self.assertEqual(state["summaries"][0]["request_id"], "gen-test-1")
        payload = request.call_args.args[1]
        self.assertEqual(payload["model"], MODEL)
        self.assertFalse(payload["reasoning"]["enabled"])
        self.assertNotIn("private-native-id", json.dumps(payload))
        self.assertEqual(set(json.loads(payload["messages"][1]["content"])), {"r1"})
        self.assertIn("untrusted data", payload["messages"][0]["content"])

    def test_unchanged_context_never_repolled(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.age_calls(instance)
            instance.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(instance.public_state()["status"], "waiting_for_changes")

    def test_minimum_interval_even_changed_text(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.store.snapshot.return_value["runs"][0]["task"] = "Write a regression test"
            instance.tick()
            self.assertEqual(request.call_count, 1)
            self.age_calls(instance)
            instance.tick()
            # A changed goal still waits for the per-run refresh interval.
            self.assertEqual(request.call_count, 1)
            self.assertTrue(instance.public_state()["summaries"][0]["stale"])
            instance.db.execute("UPDATE summaries SET generated_at='2020-01-01T00:00:00+00:00'")
            instance.tick()
            self.assertEqual(request.call_count, 2)

    def test_progress_alone_never_regenerates(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.age_calls(instance)
            instance.db.execute("UPDATE summaries SET generated_at='2020-01-01T00:00:00+00:00'")
            self.store.snapshot.return_value["runs"][0]["activity"] = "Writing a regression test"
            instance.tick()
        self.assertEqual(request.call_count, 1)

    def test_only_recent_active_roots(self):
        instance = self.summarizer()
        self.store.snapshot.return_value = {
            "runs": [
                self.make_run(state="idle", parent_run_id="parent"),
                self.make_run(state="completed"),
                self.make_run(parent_run_id="parent"),
                self.make_run(last_seen="2000-01-01T00:00:00Z"),
            ]
        }
        with patch("spiritboard.summaries._request") as request:
            instance.tick()
        request.assert_not_called()

    def test_children_waiting_on_user_are_summarized(self):
        instance = self.summarizer()
        self.store.snapshot.return_value = {
            "runs": [
                self.make_run(run_id="needs-you", parent_run_id="parent", state="waiting_user"),
                self.make_run(run_id="busy-child", parent_run_id="parent"),
            ]
        }
        self.assertEqual([row["run_id"] for row in instance._candidates()], ["needs-you"])

    def test_working_roots_prioritized_and_batch_bounded(self):
        instance = self.summarizer()
        rows = [self.make_run(run_id=f"run-{i}", state="waiting_user") for i in range(9)]
        rows.append(self.make_run(run_id="working", activity="Urgent active work"))
        self.store.snapshot.return_value = {"runs": rows}
        result = instance._candidates()
        self.assertEqual(len(result), 6)
        self.assertEqual(result[0]["run_id"], "working")
        self.assertLessEqual(sum(len(row["text"]) for row in result), 6000)

    def test_redaction_input_and_output(self):
        instance = self.summarizer()
        secret = "sk-or-v1-abcdefghijklmnop"
        self.store.snapshot.return_value["runs"][0]["task"] = (
            f"Fix /Users/private/work/file.py email me@example.org "
            f"Bearer token123 {secret} password=abcdef"
        )
        response = self.response(
            json.dumps({"r1": "Checking /Users/private/work for me@example.org " + secret})
        )
        with patch("spiritboard.summaries._request", return_value=response) as request:
            instance.tick()
        sent = json.dumps(request.call_args.args[1])
        shown = json.dumps(instance.public_state())
        for value in (sent, shown):
            self.assertNotIn("me@example.org", value)
            self.assertNotIn("/Users/private", value)
            self.assertNotIn(secret, value)
            self.assertNotIn("token123", value)
        self.assertLessEqual(len(instance.public_state()["summaries"][0]["text"]), 140)

    def test_timeout_records_unknown_cost_without_blocking(self):
        instance = self.summarizer()
        with patch(
            "spiritboard.summaries._request", side_effect=TimeoutError("SECRET provider body")
        ) as request:
            instance.tick()
            self.age_calls(instance)
            instance.tick()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(instance.public_state()["status"], "error")
            # A failed run is retried only after the per-run refresh interval.
            instance._failed = {key: 0 for key in instance._failed}
            instance.tick()
        state = instance.public_state()
        self.assertEqual(request.call_count, 2)
        self.assertIsNone(state["actual_cost_usd"])
        self.assertEqual(state["unknown_cost_calls"], 2)
        self.assertNotIn("SECRET", json.dumps(state))

    def test_missing_cost_not_zero_and_paused(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response(cost=None)):
            instance.tick()
        state = instance.public_state()
        self.assertIsNone(state["actual_cost_usd"])
        self.assertEqual(state["status"], "ready")
        self.assertEqual(len(state["summaries"]), 1)

    def test_invalid_cost_variants_unknown(self):
        for cost in (True, float("nan"), float("inf"), -1, "0.001"):
            with self.subTest(cost=cost):
                instance = self.summarizer()
                with patch("spiritboard.summaries._request", return_value=self.response(cost=cost)):
                    instance.tick()
                self.assertIsNone(instance.public_state()["actual_cost_usd"])

    def test_invalid_content_still_accounts_for_charge(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response("not JSON")):
            instance.tick()
        state = instance.public_state()
        self.assertEqual(state["status"], "error")
        self.assertAlmostEqual(state["actual_cost_usd"], 0.00012)
        self.assertEqual(state["summaries"], [])

    def test_extra_missing_duplicate_and_wrong_type_ids_rejected(self):
        for content in (
            '{"r2":"Unknown"}',
            '{"r1":"Fine","extra":"No"}',
            '{"r1":"First","r1":"Second"}',
            '{"r1":42}',
            "[]",
        ):
            with self.subTest(content=content):
                instance = self.summarizer()
                with patch("spiritboard.summaries._request", return_value=self.response(content)):
                    instance.tick()
                self.assertEqual(instance.public_state()["summaries"], [])
                self.assertAlmostEqual(instance.public_state()["actual_cost_usd"], 0.00012)

    def test_persistent_ledger_and_cache_without_raw_text(self):
        path = self.root / "summaries.sqlite3"
        instance = self.summarizer(state_path=path)
        with patch("spiritboard.summaries._request", return_value=self.response()):
            instance.tick()
        instance.db.close()
        self.instances.remove(instance)
        resumed = self.summarizer(state_path=path)
        self.assertEqual(resumed.public_state()["calls"], 1)
        self.assertAlmostEqual(resumed.public_state()["actual_cost_usd"], 0.00012)
        self.assertEqual(resumed._candidates(), [])
        contents = path.read_bytes()
        self.assertNotIn(b"test-key-not-real", contents)
        self.assertNotIn(b"Fix the retry test", contents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_pending_after_crash_is_reported_as_unknown_cost(self):
        path = self.root / "summaries.sqlite3"
        first = self.summarizer(state_path=path)
        first.db.execute("INSERT INTO calls(started,outcome) VALUES(?,'pending')", (time.time(),))
        second = self.summarizer(state_path=path)
        self.assertEqual(second.public_state()["unknown_cost_calls"], 1)
        self.assertIsNone(second.public_state()["actual_cost_usd"])
        with patch("spiritboard.summaries._request") as request:
            second.tick()
        request.assert_not_called()

    def test_codex_text_excludes_reasoning_tools_instructions(self):
        rows = [
            {"type": "session_meta", "payload": {"base_instructions": "SECRET instructions"}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "# AGENTS.md instructions SECRET"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Fix the retry test"},
            },
            {"type": "response_item", "payload": {"type": "reasoning", "text": "SECRET reasoning"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "channel": "analysis",
                    "content": "SECRET analysis",
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "SECRET output"},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "arguments": "SECRET args"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "channel": "commentary",
                    "content": [{"type": "output_text", "text": "Checking expectations"}],
                },
            },
        ]
        result = _goal_text(_goal(rows, "codex"))
        self.assertIn("Fix the retry test", result)
        self.assertIn("Checking expectations", result)
        self.assertNotIn("SECRET", result)

    def test_claude_text_excludes_thinking_and_tool_results(self):
        rows = [
            {"type": "user", "message": {"content": "Fix the retry test"}},
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "SECRET output"},
                        {"type": "text", "text": "SECRET supplemental tool output"},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "SECRET thought"},
                        {"type": "tool_use", "input": "SECRET args"},
                        {"type": "text", "text": "Checking expectations"},
                    ]
                },
            },
            {"type": "system", "message": {"content": "SECRET system"}},
        ]
        result = _goal_text(_goal(rows, "claude"))
        self.assertIn("Fix the retry test", result)
        self.assertIn("Checking expectations", result)
        self.assertNotIn("SECRET", result)

    def test_claude_meta_compaction_and_shell_echo_rows_never_sent(self):
        rows = [
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {"content": "SECRET continuation of tool output"},
            },
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "SECRET skill body: follow these steps"},
            },
            {"type": "user", "message": {"content": "<bash-input>cat .env</bash-input>"}},
            {"type": "user", "message": {"content": "<bash-stdout>DB_HOST=SECRET</bash-stdout>"}},
            {
                "type": "user",
                "message": {"content": "<local-command-stdout>SECRET</local-command-stdout>"},
            },
            {"type": "user", "message": {"content": "<command-name>/rename</command-name>"}},
            {"type": "user", "timestamp": 42, "message": {"content": "Fix the retry test"}},
            {"type": "ai-title", "aiTitle": "Generated title"},
            {"type": "custom-title", "customTitle": "My name"},
            {"type": "ai-title", "aiTitle": "Later generated title"},
        ]
        goal = _goal(rows, "claude")
        self.assertEqual(goal["users"], ["Fix the retry test"])
        self.assertEqual(goal["title"], "My name")
        self.assertEqual(goal["last_user_at"], "")
        self.assertNotIn("SECRET", _goal_text(goal))

    def test_native_reads_only_matching_active_transcript(self):
        profile = SourceProfile("codex-one", "codex", "Test Codex", str(self.root))
        folder = self.root / "sessions"
        folder.mkdir()
        path = folder / "run.jsonl"
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": "native", "timestamp": "2026-10-20T08:00:00Z"},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix retries"}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.store.snapshot.return_value = {
            "runs": [
                self.make_run(
                    run_id="codex-one:native",
                    source="codex",
                    mode="observation",
                    task="Text hidden",
                )
            ]
        }
        instance = self.summarizer()
        instance.profiles_provider = lambda: [profile]
        candidates = instance._candidates()
        self.assertEqual(len(candidates), 1)
        self.assertIn("Fix retries", candidates[0]["text"])
        self.assertNotIn("Text hidden", candidates[0]["text"])

    def test_native_never_falls_back_to_hidden_metadata(self):
        self.store.snapshot.return_value = {
            "runs": [self.make_run(source="codex", mode="observation", task="Text hidden")]
        }
        instance = self.summarizer()
        self.assertEqual(instance._candidates(), [])

    def test_fixed_https_host_no_redirects_or_provider_error_body(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b"SECRET provider error"
        with patch(
            "spiritboard.summaries.http.client.HTTPSConnection", return_value=connection
        ) as factory:
            with self.assertRaisesRegex(ValueError, "Provider response unavailable"):
                _request("not-real", {}, 20)
        factory.assert_called_once_with("openrouter.ai", timeout=20)
        connection.request.assert_called_once()
        self.assertEqual(
            connection.request.call_args.args[:2], ("POST", "/api/v1/chat/completions")
        )
        connection.close.assert_called_once()

    def test_input_validation(self):
        for kwargs in ({"interval": 1}, {"timeout": 0}, {"timeout": 61}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.summarizer(**kwargs)
        with self.assertRaises(ValueError):
            Summarizer(lambda: [], self.store, "bad\r\nkey")

    def test_stop_never_dispatches_again(self):
        instance = self.summarizer()
        instance.stop()
        with patch("spiritboard.summaries._request") as request:
            instance.tick()
        request.assert_not_called()

    def test_duplicate_tick_ignored_while_busy(self):
        instance = self.summarizer()
        instance.tick_lock.acquire()
        with patch("spiritboard.summaries._request") as request:
            instance.tick()
        instance.tick_lock.release()
        request.assert_not_called()

    def test_shared_state_file_prevents_second_reservation(self):
        path = self.root / "shared.sqlite3"
        first = self.summarizer(state_path=path)
        second = self.summarizer(state_path=path)
        self.assertIsNotNone(first._reserve())
        self.assertIsNone(second._reserve())
        self.assertEqual(second.public_state()["calls"], 1)
        self.assertEqual(first.public_state()["status"], "running")

    def test_explicit_provider_zero_is_known_not_missing(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response(cost=0)):
            instance.tick()
        self.assertEqual(instance.public_state()["actual_cost_usd"], 0)
        self.assertEqual(instance.public_state()["unknown_cost_calls"], 0)

    def test_instrumented_codex_sdk_uses_supplied_text(self):
        self.store.snapshot.return_value = {"runs": [self.make_run(source="codex", mode="sdk")]}
        self.assertIn("Fix the retry test", self.summarizer()._candidates()[0]["text"])

    def test_sdk_summary_stale_immediately_when_task_changes(self):
        instance = self.summarizer()
        with patch("spiritboard.summaries._request", return_value=self.response()):
            instance.tick()
        self.assertFalse(instance.public_state()["summaries"][0]["stale"])
        self.store.snapshot.return_value["runs"][0]["task"] = "A completely new task"
        self.assertTrue(instance.public_state()["summaries"][0]["stale"])

    def claude_profile(self, rows):
        profile = SourceProfile("claude-one", "claude", "Test Claude", str(self.root))
        folder = self.root / "projects" / "project"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "session.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.store.snapshot.return_value = {
            "runs": [
                self.make_run(
                    run_id="claude-one:session", source="claude", mode="observation", task=""
                )
            ]
        }
        return profile

    def test_claude_recap_used_without_provider_call(self):
        rows = [
            {
                "type": "user",
                "sessionId": "session",
                "timestamp": "2026-10-20T08:00:00Z",
                "message": {"content": "Fix the retry test"},
            },
            {"type": "ai-title", "aiTitle": "Retry test repair", "sessionId": "session"},
            {
                "type": "system",
                "subtype": "away_summary",
                "sessionId": "session",
                "timestamp": "2026-10-20T08:05:00Z",
                "content": (
                    "We're repairing the flaky retry test. Next, rerun the suite. "
                    "(disable recaps in /config)"
                ),
            },
        ]
        profile = self.claude_profile(rows)
        instance = self.summarizer()
        instance.profiles_provider = lambda: [profile]
        with patch("spiritboard.summaries._request") as request:
            instance.tick()
        request.assert_not_called()
        entry = instance.public_state()["summaries"][0]
        self.assertEqual(entry["origin"], "claude-recap")
        self.assertEqual(entry["title"], "Retry test repair")
        self.assertEqual(
            entry["text"], "We're repairing the flaky retry test. Next, rerun the suite."
        )
        self.assertEqual(instance.public_state()["calls"], 0)

    def test_request_after_recap_uses_provider_with_goal_context(self):
        rows = [
            {
                "type": "user",
                "sessionId": "session",
                "timestamp": "2026-10-20T08:00:00Z",
                "message": {"content": "Fix the retry test"},
            },
            {
                "type": "system",
                "subtype": "away_summary",
                "sessionId": "session",
                "timestamp": "2026-10-20T08:05:00Z",
                "content": "Repairing the retry test.",
            },
            {
                "type": "user",
                "sessionId": "session",
                "timestamp": "2026-10-20T08:06:00Z",
                "message": {"content": "Now also add docs"},
            },
        ]
        profile = self.claude_profile(rows)
        instance = self.summarizer()
        instance.profiles_provider = lambda: [profile]
        with patch("spiritboard.summaries._request", return_value=self.response()) as request:
            instance.tick()
        sent = json.loads(request.call_args.args[1]["messages"][1]["content"])["r1"]
        for part in (
            "recap: Repairing the retry test.",
            "user: Fix the retry test",
            "user: Now also add docs",
        ):
            self.assertIn(part, sent)
        self.assertIn(
            "overall problem or goal", request.call_args.args[1]["messages"][0]["content"]
        )
        self.assertEqual(instance.public_state()["summaries"][0]["origin"], "model")

    def test_codex_thread_name_from_session_index(self):
        profile = SourceProfile("codex-one", "codex", "Test Codex", str(self.root))
        (self.root / "sessions").mkdir()
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": "native", "timestamp": "2026-10-20T08:00:00Z"},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix retries"}},
        ]
        (self.root / "sessions" / "run.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        (self.root / "session_index.jsonl").write_text(
            json.dumps({"id": "native", "thread_name": "Retry repair"}) + "\n"
        )
        self.store.snapshot.return_value = {
            "runs": [self.make_run(run_id="codex-one:native", source="codex", mode="observation")]
        }
        instance = self.summarizer()
        instance.profiles_provider = lambda: [profile]
        candidate = instance._candidates()[0]
        self.assertEqual(candidate["title"], "Retry repair")
        self.assertIn("title: Retry repair", candidate["text"])

    def test_control_characters_stripped(self):
        self.assertEqual(_clean("Checking\x00\x1b[31m tests\n"), "Checking [31m tests")


if __name__ == "__main__":
    unittest.main()
