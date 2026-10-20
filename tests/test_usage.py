import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_viz import sources
from agent_viz.monitor import Monitor
from agent_viz.sources import SourceProfile, scan_usage

NOW = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def stamp(days_ago: int, minute: int = 0) -> str:
    return (NOW - timedelta(days=days_ago, minutes=-minute)).isoformat().replace("+00:00", "Z")


def day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).date().isoformat()


def claude_user(days_ago, text, **extra):
    return {
        "type": "user",
        "sessionId": "claude-session",
        "timestamp": stamp(days_ago),
        "message": {"role": "user", "content": text},
        **extra,
    }


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        sources._USAGE_CACHE.clear()

    def tearDown(self):
        self.temp.cleanup()
        sources._USAGE_CACHE.clear()

    def profile(self, kind, name):
        root = self.root / name
        (root / ("sessions" if kind == "codex" else "projects")).mkdir(parents=True)
        return SourceProfile(name, kind, name, str(root))

    def log(self, profile, name, rows, extra=b""):
        folder = Path(profile.root) / ("sessions" if profile.kind == "codex" else "projects")
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + extra)
        return path

    def test_claude_counts_real_requests_by_day_and_skips_meta_and_tool_results(self):
        profile = self.profile("claude", "claude-one")
        self.log(
            profile,
            "p/a.jsonl",
            [
                claude_user(3, "Plan the release"),
                claude_user(3, [{"type": "text", "text": "And the changelog"}]),
                claude_user(3, [{"type": "tool_result", "content": "SECRET output"}]),
                claude_user(3, "Skill body", isMeta=True),
                claude_user(3, "Summary", isCompactSummary=True),
                claude_user(3, "<command-name>/clear</command-name>"),
                {"type": "assistant", "sessionId": "claude-session", "timestamp": stamp(2)},
                claude_user(0, "Ship it"),
            ],
            extra=b'{"type":"user","timestamp":"' + stamp(0).encode() + b'","message":',
        )
        self.log(
            profile,
            "p/a/subagents/agent-x.jsonl",
            [claude_user(0, "Parent agent prompt", isSidechain=True, agentId="x")],
        )
        usage = scan_usage(profile, tz=UTC)
        session = frozenset({"claude-one:claude-session"})
        self.assertEqual(usage, {day(3): (2, session), day(2): (0, session), day(0): (1, session)})

    def test_codex_user_messages_and_subagents_share_parent_session(self):
        profile = self.profile("codex", "codex-one")
        meta = {"type": "session_meta", "timestamp": stamp(1), "payload": {"id": "root"}}

        def message(days_ago, text):
            payload = {"type": "user_message", "message": text}
            return {"type": "event_msg", "timestamp": stamp(days_ago), "payload": payload}

        self.log(
            profile,
            "2026/root.jsonl",
            [
                meta,
                message(1, "Fix the flaky test"),
                message(1, "<environment_context>cwd</environment_context>"),
                {"type": "response_item", "timestamp": stamp(1), "payload": {"role": "user"}},
                message(0, "Now the docs"),
            ],
        )
        child_source = {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}
        child_meta = {"id": "child", "source": child_source}
        self.log(
            profile,
            "2026/child.jsonl",
            [
                {"type": "session_meta", "timestamp": stamp(0), "payload": child_meta},
                message(0, "Delegated prompt"),
            ],
        )
        usage = scan_usage(profile, tz=UTC)
        session = frozenset({"codex-one:root"})
        self.assertEqual(usage, {day(1): (1, session), day(0): (1, session)})

    def test_window_cutoff_ignores_old_files_and_records(self):
        profile = self.profile("claude", "claude-one")
        self.log(profile, "p/mixed.jsonl", [claude_user(400, "Old"), claude_user(0, "New")])
        old = self.log(profile, "p/old.jsonl", [claude_user(0, "Never read")])
        aged = time.time() - 400 * 86400
        os.utime(old, (aged, aged))
        coverage = {}
        usage = scan_usage(profile, tz=UTC, diagnostics=coverage)
        self.assertEqual(list(usage), [day(0)])
        self.assertEqual(usage[day(0)][0], 1)
        self.assertEqual(coverage["aged_out"], 1)

    def test_key_and_non_transcript_files_are_never_opened(self):
        profile = self.profile("claude", "claude-one")
        self.log(profile, "p/a.jsonl", [claude_user(0, "Hello")])
        folder = Path(profile.root) / "projects" / "p"
        (folder / "private.key").write_text("SECRET")
        (folder / "a.jsonl.key").write_text("SECRET")
        (Path(profile.root) / "settings.json").write_text("{}")
        opened = []
        real_open = os.open

        def tracking_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch.object(sources.os, "open", tracking_open):
            usage = scan_usage(profile, tz=UTC)
        self.assertEqual(usage[day(0)][0], 1)
        self.assertEqual([Path(p).name for p in opened], ["a.jsonl"])

    def test_unchanged_files_come_from_cache_and_changes_are_reread(self):
        profile = self.profile("codex", "codex-one")
        meta = {"type": "session_meta", "timestamp": stamp(0), "payload": {"id": "s"}}
        msg = {
            "type": "event_msg",
            "timestamp": stamp(0),
            "payload": {"type": "user_message", "message": "Go"},
        }
        path = self.log(profile, "a.jsonl", [meta, msg])
        self.log(profile, "b.jsonl", [{**meta, "payload": {"id": "t"}}])
        first = scan_usage(profile, tz=UTC)
        with patch.object(sources, "_usage_file", side_effect=AssertionError("re-read")):
            self.assertEqual(scan_usage(profile, tz=UTC), first)
        self.log(profile, "a.jsonl", [meta, msg, msg])
        reads = []
        real = sources._usage_file

        def counting(profile, path, tz, coverage):
            reads.append(path.name)
            return real(profile, path, tz, coverage)

        with patch.object(sources, "_usage_file", counting):
            usage = scan_usage(profile, tz=UTC)
        self.assertEqual(reads, ["a.jsonl"])
        self.assertEqual(usage[day(0)], (2, frozenset({"codex-one:s", "codex-one:t"})))
        path.unlink()
        scan_usage(profile, tz=UTC)
        self.assertEqual(len(sources._USAGE_CACHE), 1)

    def test_oversized_records_and_clipped_files_are_bounded_and_reported(self):
        profile = self.profile("claude", "claude-one")
        big = claude_user(1, "x" * 2000)
        self.log(profile, "p/a.jsonl", [big, claude_user(0, "Kept")])
        coverage = {}
        with (
            patch.object(sources, "USAGE_MAX_LINE", 1000),
            patch.object(sources, "USAGE_MAX_FILE_BYTES", 1500),
        ):
            usage = scan_usage(profile, tz=UTC, diagnostics=coverage)
        self.assertEqual(usage[day(0)][0], 1)
        self.assertNotIn(day(1), usage)
        self.assertEqual(coverage["clipped_files"], 1)
        sources._USAGE_CACHE.clear()
        coverage = {}
        with patch.object(sources, "USAGE_MAX_LINE", 1000):
            usage = scan_usage(profile, tz=UTC, diagnostics=coverage)
        self.assertEqual(coverage["oversized_records"], 1)
        self.assertEqual(usage[day(0)][0], 1)

    def test_monitor_publishes_usage_per_profile_without_paths_or_text(self):
        good = self.profile("claude", "claude-one")
        self.log(good, "p/a.jsonl", [claude_user(0, "SECRET prompt")])
        missing = SourceProfile("missing", "codex", "missing", str(self.root / "nope"))
        monitor = Monitor([good, missing], store=object())
        usage = monitor.scan_usage_once()
        self.assertEqual(set(usage["profiles"]), {"claude-one"})
        self.assertTrue(usage["limited"])
        self.assertIs(monitor.public_usage(), usage)
        text = repr(usage)
        self.assertNotIn("SECRET", text)
        self.assertNotIn(str(self.root), text)


if __name__ == "__main__":
    unittest.main()
