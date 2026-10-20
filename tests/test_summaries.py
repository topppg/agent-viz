import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_viz.summaries import MODEL, Summarizer, _clean, _recent_text, _request
from agent_viz.sources import SourceProfile


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
        from datetime import datetime, timezone
        result = {"run_id": "private-native-id", "parent_run_id": None, "source": "sdk",
                  "state": "working", "last_seen": datetime.now(timezone.utc).isoformat(),
                  "task": "Fix the retry test", "activity": "Reading test expectations"}
        result.update(overrides)
        return result

    def summarizer(self, **kwargs):
        instance = Summarizer(lambda: [], self.store, "test-key-not-real", **kwargs)
        self.instances.append(instance)
        return instance

    def response(self, content=None, cost=.00012):
        response = {"id": "gen-test-1", "choices": [{"message": {"content": content or json.dumps({"r1": "Investigating retry-test expectations"})}}], "usage": {}}
        if cost is not None:
            response["usage"]["cost"] = cost
        return response

    def age_calls(self, instance):
        instance.db.execute("UPDATE calls SET started=?", (time.time() - 125,))

    def test_success_actual_cost_and_opaque_ids(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response()) as request:
            instance.tick()
        state = instance.public_state()
        self.assertEqual(state["calls"], 1)
        self.assertAlmostEqual(state["actual_cost_usd"], .00012)
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
        with patch("agent_viz.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.age_calls(instance)
            instance.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(instance.public_state()["status"], "waiting_for_changes")

    def test_minimum_interval_even_changed_text(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.store.snapshot.return_value["runs"][0]["activity"] = "Writing a regression test"
            instance.tick()
            self.assertEqual(request.call_count, 1)
            self.age_calls(instance)
            instance.tick()
            self.assertEqual(request.call_count, 2)

    def test_only_recent_active_roots(self):
        instance = self.summarizer()
        self.store.snapshot.return_value = {"runs": [self.make_run(state="idle"), self.make_run(state="completed"),
                                                      self.make_run(parent_run_id="parent"),
                                                      self.make_run(last_seen="2000-01-01T00:00:00Z")]}
        with patch("agent_viz.summaries._request") as request:
            instance.tick()
        request.assert_not_called()

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
        self.store.snapshot.return_value["runs"][0]["task"] = f"Fix /Users/private/work/file.py email me@example.org Bearer token123 {secret} password=abcdef"
        response = self.response(json.dumps({"r1": "Checking /Users/private/work for me@example.org " + secret}))
        with patch("agent_viz.summaries._request", return_value=response) as request:
            instance.tick()
        sent = json.dumps(request.call_args.args[1])
        shown = json.dumps(instance.public_state())
        for value in (sent, shown):
            self.assertNotIn("me@example.org", value)
            self.assertNotIn("/Users/private", value)
            self.assertNotIn(secret, value)
            self.assertNotIn("token123", value)
        self.assertLessEqual(len(instance.public_state()["summaries"][0]["text"]), 140)

    def test_timeout_unknown_billing_blocks_future_calls(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", side_effect=TimeoutError("SECRET provider body")) as request:
            instance.tick()
            self.age_calls(instance)
            instance.tick()
        state = instance.public_state()
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(state["actual_cost_usd"])
        self.assertEqual(state["unknown_cost_calls"], 1)
        self.assertEqual(state["status"], "paused_unknown_cost")
        self.assertNotIn("SECRET", json.dumps(state))

    def test_missing_cost_not_zero_and_paused(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response(cost=None)):
            instance.tick()
        state = instance.public_state()
        self.assertIsNone(state["actual_cost_usd"])
        self.assertEqual(state["status"], "paused_unknown_cost")
        self.assertEqual(len(state["summaries"]), 1)

    def test_invalid_cost_variants_unknown(self):
        for cost in (True, float("nan"), float("inf"), -1, "0.001"):
            with self.subTest(cost=cost):
                instance = self.summarizer()
                with patch("agent_viz.summaries._request", return_value=self.response(cost=cost)):
                    instance.tick()
                self.assertIsNone(instance.public_state()["actual_cost_usd"])

    def test_invalid_content_still_accounts_for_charge(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response("not JSON")):
            instance.tick()
        state = instance.public_state()
        self.assertEqual(state["status"], "error")
        self.assertAlmostEqual(state["actual_cost_usd"], .00012)
        self.assertEqual(state["summaries"], [])

    def test_extra_missing_duplicate_and_wrong_type_ids_rejected(self):
        for content in ('{"r2":"Unknown"}', '{"r1":"Fine","extra":"No"}',
                        '{"r1":"First","r1":"Second"}', '{"r1":42}', '[]'):
            with self.subTest(content=content):
                instance = self.summarizer()
                with patch("agent_viz.summaries._request", return_value=self.response(content)):
                    instance.tick()
                self.assertEqual(instance.public_state()["summaries"], [])
                self.assertAlmostEqual(instance.public_state()["actual_cost_usd"], .00012)

    def test_call_limit(self):
        instance = self.summarizer(max_calls=1)
        with patch("agent_viz.summaries._request", return_value=self.response()) as request:
            instance.tick()
            self.age_calls(instance)
            self.store.snapshot.return_value["runs"][0]["task"] = "Another task"
            instance.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(instance.public_state()["status"], "call_limit")

    def test_budget_reserves_next_request(self):
        instance = self.summarizer(budget_usd=.015)
        with patch("agent_viz.summaries._request", return_value=self.response(cost=.006)) as request:
            instance.tick()
            self.age_calls(instance)
            self.store.snapshot.return_value["runs"][0]["task"] = "Another task"
            instance.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(instance.public_state()["status"], "budget_limit")

    def test_persistent_ledger_and_cache_without_raw_text(self):
        path = self.root / "summaries.sqlite3"
        instance = self.summarizer(state_path=path)
        with patch("agent_viz.summaries._request", return_value=self.response()):
            instance.tick()
        instance.db.close()
        self.instances.remove(instance)
        resumed = self.summarizer(state_path=path)
        self.assertEqual(resumed.public_state()["calls"], 1)
        self.assertAlmostEqual(resumed.public_state()["actual_cost_usd"], .00012)
        self.assertEqual(resumed._candidates(), [])
        contents = path.read_bytes()
        self.assertNotIn(b"test-key-not-real", contents)
        self.assertNotIn(b"Fix the retry test", contents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_pending_after_crash_blocks_across_restart(self):
        path = self.root / "summaries.sqlite3"
        first = self.summarizer(state_path=path)
        first.db.execute("INSERT INTO calls(started,outcome) VALUES(?,'pending')", (time.time(),))
        second = self.summarizer(state_path=path)
        self.assertEqual(second.public_state()["status"], "paused_unknown_cost")
        with patch("agent_viz.summaries._request") as request:
            second.tick()
        request.assert_not_called()

    def test_codex_text_excludes_reasoning_tools_instructions(self):
        rows = [
            {"type": "session_meta", "payload": {"base_instructions": "SECRET instructions"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "# AGENTS.md instructions SECRET"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the retry test"}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": "SECRET reasoning"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": "SECRET analysis"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "SECRET output"}},
            {"type": "response_item", "payload": {"type": "function_call", "arguments": "SECRET args"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "commentary", "content": [{"type": "output_text", "text": "Checking expectations"}]}},
        ]
        result = _recent_text(rows, "codex")
        self.assertIn("Fix the retry test", result)
        self.assertIn("Checking expectations", result)
        self.assertNotIn("SECRET", result)

    def test_claude_text_excludes_thinking_and_tool_results(self):
        rows = [{"type": "user", "message": {"content": "Fix the retry test"}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET output"}, {"type": "text", "text": "SECRET supplemental tool output"}]}},
                {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "SECRET thought"}, {"type": "tool_use", "input": "SECRET args"}, {"type": "text", "text": "Checking expectations"}]}},
                {"type": "system", "message": {"content": "SECRET system"}}]
        result = _recent_text(rows, "claude")
        self.assertIn("Fix the retry test", result)
        self.assertIn("Checking expectations", result)
        self.assertNotIn("SECRET", result)

    def test_native_reads_only_matching_active_transcript(self):
        profile = SourceProfile("codex-one", "codex", "Test Codex", str(self.root))
        folder = self.root / "sessions"
        folder.mkdir()
        path = folder / "run.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": "native", "timestamp": "2026-10-20T08:00:00Z"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix retries"}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.store.snapshot.return_value = {"runs": [self.make_run(run_id="codex-one:native", source="codex", mode="observation", task="Text hidden")]}
        instance = self.summarizer()
        instance.profiles_provider = lambda: [profile]
        candidates = instance._candidates()
        self.assertEqual(len(candidates), 1)
        self.assertIn("Fix retries", candidates[0]["text"])
        self.assertNotIn("Text hidden", candidates[0]["text"])

    def test_native_never_falls_back_to_hidden_metadata(self):
        self.store.snapshot.return_value = {"runs": [self.make_run(source="codex", mode="observation", task="Text hidden")]}
        instance = self.summarizer()
        self.assertEqual(instance._candidates(), [])

    def test_fixed_https_host_no_redirects_or_provider_error_body(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b"SECRET provider error"
        with patch("agent_viz.summaries.http.client.HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(ValueError, "Provider response unavailable"):
                _request("not-real", {}, 20)
        factory.assert_called_once_with("openrouter.ai", timeout=20)
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/api/v1/chat/completions"))
        connection.close.assert_called_once()

    def test_input_validation(self):
        for kwargs in ({"budget_usd": -1}, {"budget_usd": float("nan")}, {"max_calls": True},
                       {"max_calls": 0}, {"interval": 1}, {"timeout": 0}, {"timeout": 61}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.summarizer(**kwargs)
        with self.assertRaises(ValueError):
            Summarizer(lambda: [], self.store, "bad\r\nkey")

    def test_stop_never_dispatches_again(self):
        instance = self.summarizer()
        instance.stop()
        with patch("agent_viz.summaries._request") as request:
            instance.tick()
        request.assert_not_called()

    def test_duplicate_tick_ignored_while_busy(self):
        instance = self.summarizer()
        instance.tick_lock.acquire()
        with patch("agent_viz.summaries._request") as request:
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
        self.assertEqual(second.public_state()["status"], "paused_unknown_cost")
        self.assertEqual(first.public_state()["status"], "running")

    def test_explicit_provider_zero_is_known_not_missing(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response(cost=0)):
            instance.tick()
        self.assertEqual(instance.public_state()["actual_cost_usd"], 0)
        self.assertEqual(instance.public_state()["unknown_cost_calls"], 0)

    def test_instrumented_codex_sdk_uses_supplied_text(self):
        self.store.snapshot.return_value = {"runs": [self.make_run(source="codex", mode="sdk")]}
        self.assertIn("Fix the retry test", self.summarizer()._candidates()[0]["text"])

    def test_sdk_summary_stale_immediately_when_task_changes(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response()):
            instance.tick()
        self.assertFalse(instance.public_state()["summaries"][0]["stale"])
        self.store.snapshot.return_value["runs"][0]["task"] = "A completely new task"
        self.assertTrue(instance.public_state()["summaries"][0]["stale"])

    def test_native_newer_activity_marks_cached_summary_possibly_stale(self):
        instance = self.summarizer()
        with patch("agent_viz.summaries._request", return_value=self.response()):
            instance.tick()
        run = self.store.snapshot.return_value["runs"][0]
        run["mode"] = "observation"
        instance._verified_until[run["run_id"]] = time.time() - 60
        self.assertTrue(instance.public_state()["summaries"][0]["stale"])
        # A same-content native scan can verify newer activity without a call.
        instance._verified_until[run["run_id"]] = time.time()
        with patch("agent_viz.summaries._recent_files") as files, patch("agent_viz.summaries._request") as request:
            self.assertFalse(instance.public_state()["summaries"][0]["stale"])
        files.assert_not_called()
        request.assert_not_called()

    def test_control_characters_stripped(self):
        self.assertEqual(_clean("Checking\x00\x1b[31m tests\n"), "Checking [31m tests")


if __name__ == "__main__":
    unittest.main()
