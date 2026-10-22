import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from spiritboard import transcripts
from spiritboard.harnesses import claude
from spiritboard.monitor import Monitor
from spiritboard.sources import (
    SourceProfile,
    discover_profiles,
    load_profiles,
    scan_inventory,
    scan_profile,
)
from spiritboard.transcripts import Session


def unit(path):
    return Session(str(path), path, 0, 0, 0.0)


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def profile(self, kind="codex", name="one", text=False):
        root = self.root / name
        (root / ("sessions" if kind == "codex" else "projects")).mkdir(parents=True)
        return SourceProfile(name, kind, name, str(root), text)

    def log(self, profile, name, rows, extra=b""):
        folder = Path(profile.root) / ("sessions" if profile.kind == "codex" else "projects")
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + extra)
        return path

    def codex(self, native="same", parent=None):
        meta = {
            "id": native,
            "timestamp": "2026-10-20T08:00:00Z",
            "cwd": "/private/deep/project",
            "base_instructions": "SECRET instructions",
            "creator_account_id": "SECRET account",
        }
        if parent:
            meta["source"] = {
                "subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": "PRIVATE"}}
            }
        return [
            {"type": "session_meta", "payload": meta},
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:01Z",
                "payload": {"type": "user_message", "message": "SECRET prompt"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-10-20T08:00:02Z",
                "payload": {"type": "function_call", "arguments": "SECRET args"},
            },
        ]

    def claude(self, agent=None):
        row = {
            "type": "assistant",
            "sessionId": "same",
            "cwd": "/private/deep/project",
            "timestamp": "2026-10-20T08:00:00Z",
            "parentUuid": "message-not-agent",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "SECRET tool", "input": {"key": "SECRET key"}}
                ],
                "stop_reason": "tool_use",
            },
        }
        if agent:
            row.update(isSidechain=True, agentId=agent)
        return [row]

    def test_four_profiles_are_independent_and_default_private(self):
        observations = []
        for kind, name in [
            ("codex", "codex-one"),
            ("codex", "codex-two"),
            ("claude", "claude-one"),
            ("claude", "claude-two"),
        ]:
            profile = self.profile(kind, name)
            self.log(profile, "session.jsonl", self.codex() if kind == "codex" else self.claude())
            observations.extend(scan_profile(profile))
        self.assertEqual(len({row["run_id"] for row in observations}), 4)
        self.assertEqual(len({row["session_id"] for row in observations}), 4)
        self.assertTrue(all(row["state"] == "waiting_tool" for row in observations))
        self.assertTrue(
            all(row["health"] == "observed" and row["inferred"] for row in observations)
        )
        rendered = json.dumps(observations)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("/private", rendered)
        self.assertTrue(all(row["project"] == "project" for row in observations))

    def test_codex_actual_parent_metadata_and_three_levels(self):
        profile = self.profile()
        for native, parent in [("root", None), ("child", "root"), ("grandchild", "child")]:
            self.log(profile, native + ".jsonl", self.codex(native, parent))
        rows = {row["native_id"]: row for row in scan_profile(profile)}
        self.assertEqual(rows["child"]["parent_run_id"], "one:root")
        self.assertEqual(rows["grandchild"]["parent_run_id"], "one:child")
        self.assertEqual({r["session_id"] for r in rows.values()}, {"one:observed"})

    def test_claude_sidechain_and_message_parent_not_confused(self):
        profile = self.profile("claude")
        self.log(profile, "root.jsonl", self.claude())
        self.log(profile, "subagents/random.jsonl", self.claude("worker"))
        rows = {row["native_id"]: row for row in scan_profile(profile)}
        self.assertIsNone(rows["same"]["parent_run_id"])
        self.assertEqual(rows["same:agent:worker"]["parent_run_id"], "one:same")
        self.assertEqual(rows["same:agent:worker"]["session_id"], "one:observed")

    def test_explicit_codex_root_session_stable_outside_window(self):
        profile = self.profile()
        rows = self.codex("grandchild", "child")
        rows[0]["payload"]["session_id"] = "root"
        self.log(profile, "recent.jsonl", rows)
        first = scan_profile(profile, limit=1)[0]
        self.assertEqual(first["session_id"], "one:observed")
        self.log(profile, "root.jsonl", self.codex("root"))
        later = {r["native_id"]: r for r in scan_profile(profile)}
        self.assertEqual(later["grandchild"]["session_id"], first["session_id"])

    def test_old_codex_collection_stable_when_ancestors_arrive_and_window_rotates(self):
        from spiritboard.store import Store

        profile = self.profile()
        grandchild = self.log(profile, "grandchild.jsonl", self.codex("grandchild", "child"))
        store = Store()
        monitor = Monitor([profile], store, limit=1)
        try:
            self.assertFalse(monitor.scan_once()["errors"])
            first = store.snapshot()["runs"][0]
            self.assertEqual(first["session_id"], "one:observed")
            self.assertEqual(first["parent_run_id"], "one:child")
            child = self.log(profile, "child.jsonl", self.codex("child", "root"))
            root = self.log(profile, "root.jsonl", self.codex("root"))
            monitor.limit = 3
            self.assertFalse(monitor.scan_once()["errors"])
            self.assertEqual(store.snapshot()["total_runs"], 3)
            # Rotate which files are visible; never change any observed identity.
            for newest in (root, grandchild, child):
                os.utime(newest, None)
                monitor.limit = 1
                self.assertFalse(monitor.scan_once()["errors"])
            monitor.limit = 3
            self.assertFalse(monitor.scan_once()["errors"])
            rows = store.snapshot()["runs"]
            self.assertEqual({r["session_id"] for r in rows}, {"one:observed"})
            self.assertEqual(
                {r["run_id"] for r in rows}, {"one:root", "one:child", "one:grandchild"}
            )
        finally:
            store.close()

    def test_turn_end_is_idle_not_session_completion_then_resumes(self):
        profile = self.profile()
        rows = self.codex()
        rows.append(
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:03Z",
                "payload": {"type": "task_complete"},
            }
        )
        self.log(profile, "run.jsonl", rows)
        self.assertEqual(scan_profile(profile)[0]["state"], "idle")
        rows.append(
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:04Z",
                "payload": {"type": "task_started"},
            }
        )
        self.log(profile, "run.jsonl", rows)
        self.assertEqual(scan_profile(profile)[0]["state"], "working")

    def test_text_can_be_hidden_and_tool_arguments_never_shown(self):
        private = self.profile(text=True)
        self.log(private, "run.jsonl", self.codex())
        output = scan_profile(private)[0]
        self.assertEqual(output["task"], "SECRET prompt")
        self.assertNotIn("SECRET args", json.dumps(output))
        safe = SourceProfile(
            private.id, private.kind, private.label, private.root, include_text=False
        )
        self.assertNotIn("SECRET", json.dumps(scan_profile(safe)))

    def test_task_is_the_latest_real_user_request(self):
        claude = self.profile("claude", "claude-latest", text=True)
        user = {"type": "user", "sessionId": "same", "timestamp": "2026-10-20T08:00:00Z"}
        self.log(
            claude,
            "p/session.jsonl",
            [
                {**user, "message": {"content": "First request"}},
                {**user, "message": {"content": "Latest request"}},
                {**user, "isMeta": True, "message": {"content": "Skill body: follow these steps"}},
                {**user, "message": {"content": "<command-name>/clear</command-name>"}},
            ],
        )
        self.assertEqual(scan_profile(claude)[0]["task"], "Latest request")
        codex = self.profile("codex", "codex-latest", text=True)
        rows = self.codex()
        rows.append(
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:05Z",
                "payload": {"type": "user_message", "message": "Follow-up request"},
            }
        )
        self.log(codex, "run.jsonl", rows)
        self.assertEqual(scan_profile(codex)[0]["task"], "Follow-up request")

    def test_claude_now_keeps_latest_agent_message_after_tool_result(self):
        profile = self.profile("claude", "claude-reply", text=True)
        base = {"sessionId": "same", "timestamp": "2026-10-20T08:00:00Z"}
        self.log(
            profile,
            "p/session.jsonl",
            [
                {
                    **base,
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Investigating the retry path"}]
                    },
                },
                {**base, "type": "user", "message": {"content": "Check one more caller"}},
                {
                    **base,
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "SECRET output"}]},
                },
            ],
        )
        with patch.object(claude, "latest_reply", wraps=claude.latest_reply) as search:
            row = scan_profile(profile)[0]
        search.assert_not_called()
        self.assertEqual(row["activity"], "Investigating the retry path")
        self.assertNotIn("SECRET output", json.dumps(row))
        hidden = replace(profile, include_text=False)
        self.assertEqual(scan_profile(hidden)[0]["activity"], "Tool result recorded")

    def test_subagent_named_by_description_with_parent_prompt_as_goal(self):
        profile = self.profile("claude", "claude-sub", text=True)
        side = {
            "type": "user",
            "sessionId": "same",
            "isSidechain": True,
            "agentId": "a1b2c3",
            "timestamp": "2026-10-20T08:00:00Z",
        }
        path = self.log(
            profile,
            "p/same/subagents/agent-a1b2c3.jsonl",
            [
                {**side, "message": {"content": "Find every caller of the retry helper"}},
                {**side, "message": {"content": "A later user row"}},
            ],
        )
        meta = path.with_name("agent-a1b2c3.meta.json")
        meta.write_text(json.dumps({"description": "Map retry callers", "agentType": "Explore"}))
        row = scan_profile(profile)[0]
        self.assertEqual(row["name"], "Map retry callers")
        self.assertEqual(row["task"], "Find every caller of the retry helper")
        hidden = SourceProfile(
            profile.id + "-h", "claude", "Hidden", profile.root, include_text=False
        )
        self.assertTrue(scan_profile(hidden)[0]["name"].startswith("Claude subagent"))
        fork = self.log(
            profile,
            "p/same/subagents/agent-f00d.jsonl",
            [
                {"type": "fork-context-ref", "agentId": "f00d"},
                {
                    **side,
                    "agentId": "f00d",
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": "SECRET inherited output"},
                            {
                                "type": "text",
                                "text": "<fork-boilerplate>"
                                + "x" * 3000
                                + "</fork-boilerplate>\nYour directive: Ship A4",
                            },
                        ]
                    },
                },
            ],
        )
        forked = next(r for r in scan_profile(profile) if r["run_id"].endswith("f00d"))
        self.assertEqual(forked["task"], "Ship A4")
        self.assertNotIn("SECRET", json.dumps(forked))
        fork.unlink()
        meta.unlink()
        meta.symlink_to(self.root / "elsewhere.json")
        (self.root / "elsewhere.json").write_text(json.dumps({"description": "Followed a link"}))
        path.write_text(path.read_text() + "\n")
        self.assertTrue(scan_profile(profile)[0]["name"].startswith("Claude subagent"))

    def test_claude_subagent_without_description_is_a_placeholder_title(self):
        profile = self.profile("claude", "claude-placeholder", text=True)
        self.log(profile, "p/same.jsonl", self.claude())
        path = self.log(profile, "p/same/subagents/agent-a1b2c3.jsonl", self.claude("a1b2c3"))
        rows = {row["native_id"]: row for row in scan_profile(profile)}
        child = rows["same:agent:a1b2c3"]
        self.assertTrue(child["name"].startswith("Claude subagent"))
        self.assertEqual(child["title_source"], "placeholder")
        self.assertNotIn("title_source", rows["same"])
        meta = path.with_name("agent-a1b2c3.meta.json")
        meta.write_text(json.dumps({"description": "Map retry callers"}))
        path.write_text(path.read_text() + "\n")
        child = next(r for r in scan_profile(profile) if r["native_id"] == "same:agent:a1b2c3")
        self.assertEqual(child["name"], "Map retry callers")
        self.assertNotIn("title_source", child)

    def test_native_title_index_replaces_a_placeholder_title(self):
        profile = self.profile("claude", "claude-indexed", text=True)
        self.log(profile, "p/same/subagents/agent-a1b2c3.jsonl", self.claude("a1b2c3"))
        self.assertEqual(scan_profile(profile)[0]["title_source"], "placeholder")
        indexed = replace(
            claude.HARNESS, titles=lambda root: {"same:agent:a1b2c3": "Indexed title"}
        )
        with patch.dict("spiritboard.sources.HARNESSES", {"claude": indexed}):
            row = scan_profile(profile)[0]
        self.assertEqual(row["name"], "Indexed title")
        self.assertNotIn("title_source", row)
        # The override applies to the returned copy, not the cached observation.
        self.assertEqual(scan_profile(profile)[0]["title_source"], "placeholder")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires named pipes")
    def test_metadata_readers_do_not_block_on_fifo(self):
        for reader, filename in [
            ("subagent_description", "agent-synthetic.meta.json"),
            ("latest_request", "request.jsonl"),
        ]:
            with self.subTest(reader=reader):
                path = self.root / filename
                os.mkfifo(path)
                code = (
                    "from pathlib import Path; import sys; "
                    "from spiritboard.harnesses.claude import "
                    "latest_request, subagent_description; "
                    "from spiritboard.transcripts import Session; "
                    "path = Path(sys.argv[1]); "
                    + (
                        "assert subagent_description("
                        "path.with_name(path.name.replace('.meta.json', '.jsonl'))) == ''"
                        if reader == "subagent_description"
                        else "assert latest_request(Session(str(path), path, 0, 0, 0.0)) == ''"
                    )
                )
                # A separate process bounds both a blocking open and a blocking read.
                result = subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                try:
                    os.write(writer, b'{"description":"Pipe content"}\n')
                    result = subprocess.run(
                        [sys.executable, "-c", code, str(path)],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                finally:
                    os.close(writer)

    def test_metadata_readers_check_descriptor_type_before_reading(self):
        for reader, args in [
            (claude.subagent_description, (self.root / "agent-synthetic.jsonl",)),
            (claude.latest_request, (unit(self.root / "request.jsonl"),)),
        ]:
            with self.subTest(reader=reader.__name__):
                stream = MagicMock()
                stream.read.side_effect = AssertionError("Non-regular file must not be read")
                stream.seek.side_effect = AssertionError("Non-regular file must not be sought")
                opened = MagicMock()
                opened.__enter__.return_value = stream
                with (
                    patch("spiritboard.transcripts.os.open", return_value=123) as open_file,
                    patch("spiritboard.transcripts.os.fdopen", return_value=opened),
                    patch(
                        "spiritboard.transcripts.os.fstat",
                        return_value=SimpleNamespace(st_mode=stat.S_IFIFO, st_size=100),
                    ),
                ):
                    self.assertEqual(reader(*args), "")
                self.assertTrue(open_file.call_args.args[1] & os.O_NONBLOCK)
                stream.read.assert_not_called()
                stream.seek.assert_not_called()

    def test_subagent_metadata_regular_file_size_and_type_boundaries(self):
        path = self.root / "agent-synthetic.jsonl"
        meta = path.with_suffix(".meta.json")
        meta.write_text(json.dumps({"description": "Valid description"}))
        self.assertEqual(claude.subagent_description(path), "Valid description")
        meta.write_bytes(b" " * 65_537)
        self.assertEqual(claude.subagent_description(path), "")
        meta.unlink()
        meta.mkdir()
        self.assertEqual(claude.subagent_description(path), "")
        meta.rmdir()
        target = self.root / "target.json"
        target.write_text(json.dumps({"description": "Linked description"}))
        meta.symlink_to(target)
        self.assertEqual(claude.subagent_description(path), "")

    def test_last_request_rejects_directories_and_symlinks(self):
        target = self.root / "request.jsonl"
        target.write_text(json.dumps({"type": "user", "message": {"content": "Valid"}}) + "\n")
        self.assertEqual(claude.latest_request(unit(target)), "Valid")
        link = self.root / "linked.jsonl"
        link.symlink_to(target)
        self.assertEqual(claude.latest_request(unit(link)), "")
        self.assertEqual(claude.latest_request(unit(self.root)), "")

    def test_idle_activity_is_the_last_reply(self):
        claude = self.profile("claude", "claude-reply", text=True)
        base = {"sessionId": "same", "timestamp": "2026-10-20T08:00:00Z"}
        rows = [
            {**base, "type": "user", "message": {"content": "Fix the test"}},
            {
                **base,
                "type": "assistant",
                "message": {
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "thinking", "thinking": "SECRET thought"},
                        {"type": "text", "text": "Fixed it; the suite passes."},
                    ],
                },
            },
            {**base, "type": "system", "subtype": "turn_duration"},
        ]
        self.log(claude, "p/session.jsonl", rows)
        row = scan_profile(claude)[0]
        self.assertEqual((row["state"], row["activity"]), ("idle", "Fixed it; the suite passes."))
        self.assertNotIn("SECRET", json.dumps(row))
        hidden = SourceProfile(
            claude.id + "-h", "claude", "Hidden", claude.root, include_text=False
        )
        self.assertEqual(scan_profile(hidden)[0]["activity"], "Turn ended · session may resume")
        codex = self.profile("codex", "codex-reply", text=True)
        rows = self.codex()
        rows.append(
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:09Z",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Done: retries are capped at three.",
                },
            }
        )
        self.log(codex, "run.jsonl", rows)
        self.assertEqual(scan_profile(codex)[0]["activity"], "Done: retries are capped at three.")

    def test_codex_response_item_requests_and_commentary_as_activity(self):
        profile = self.profile("codex", "codex-items", text=True)
        rows = self.codex()[:1]
        item = {"type": "response_item", "timestamp": "2026-10-20T08:00:01Z"}
        rows += [
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>SECRET</environment_context>",
                        }
                    ],
                },
            },
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "How are we doing in prod?"}],
                },
            },
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Checking the dashboards"}],
                },
            },
            {**item, "payload": {"type": "reasoning", "summary": []}},
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "SECRET developer notice"}],
                },
            },
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "<turn_aborted>SECRET</turn_aborted>"}
                    ],
                },
            },
            {**item, "payload": {"type": "function_call", "arguments": "SECRET args"}},
        ]
        self.log(profile, "run.jsonl", rows)
        row = scan_profile(profile)[0]
        self.assertEqual(row["task"], "How are we doing in prod?")
        self.assertEqual(
            (row["state"], row["activity"]), ("waiting_tool", "Checking the dashboards")
        )
        self.assertNotIn("SECRET", json.dumps(row))

    def test_last_request_found_before_the_read_tail(self):
        profile = self.profile("codex", "codex-long", text=True)
        filler = {
            "type": "response_item",
            "timestamp": "2026-10-20T08:00:03Z",
            "payload": {"type": "function_call_output", "output": "x" * 4000},
        }
        rows = (
            self.codex()[:1]
            + [filler] * 100
            + [
                {
                    "type": "response_item",
                    "timestamp": "2026-10-20T08:00:02Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Deep request"}],
                    },
                },
            ]
            + [filler] * 200
        )
        path = self.log(profile, "run.jsonl", rows)
        self.assertGreater(path.stat().st_size, 900_000)
        with patch(
            "spiritboard.harnesses.codex.last_request", wraps=transcripts.last_request
        ) as search:
            self.assertEqual(scan_profile(profile)[0]["task"], "Deep request")
            with path.open("a") as stream:
                stream.write(json.dumps(filler) + "\n")
            self.assertEqual(scan_profile(profile)[0]["task"], "Deep request")
        self.assertEqual(search.call_count, 1)

    def test_long_transcript_head_is_not_the_current_request_or_reply(self):
        def item(payload):
            return {
                "type": "response_item",
                "timestamp": "2026-10-20T08:00:03Z",
                "payload": payload,
            }

        def said(role, value):
            kind = "input_text" if role == "user" else "output_text"
            return item(
                {"type": "message", "role": role, "content": [{"type": kind, "text": value}]}
            )

        filler = item({"type": "function_call_output", "output": "x" * 4000})
        codex = self.profile("codex", "codex-head", text=True)
        rows = (
            self.codex()[:1]
            + [said("user", "Opening request"), said("assistant", "Opening reply")]
            + [filler] * 100
            + [said("user", "Latest request")]
            + [filler] * 200
        )
        self.log(codex, "run.jsonl", rows)
        row = scan_profile(codex)[0]
        self.assertEqual((row["task"], row["activity"]), ("Latest request", "Tool result recorded"))

        claude = self.profile("claude", "claude-head", text=True)
        base = {"sessionId": "same", "timestamp": "2026-10-20T08:00:00Z"}
        result = {
            **base,
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "x" * 4000}]},
        }
        rows = (
            [
                {**base, "type": "user", "message": {"content": "Opening request"}},
                {**base, "type": "assistant", "message": {"content": "Opening reply"}},
            ]
            + [result] * 100
            + [{**base, "type": "assistant", "message": {"content": "Latest agent message"}}]
            + [{**base, "type": "user", "message": {"content": "Latest request"}}]
            + [result] * 200
        )
        self.log(claude, "p/session.jsonl", rows)
        row = scan_profile(claude)[0]
        self.assertEqual((row["task"], row["activity"]), ("Latest request", "Latest agent message"))

    def test_codex_app_requests_are_unwrapped(self):
        profile = self.profile("codex", "codex-app", text=True)
        meta = self.codex()[:1]
        cases = {
            "files": [
                "\n# Files mentioned by the user:\n\n## cv.pdf: /private/SECRET/cv.pdf\n"
                "## My request:\nShip the CV",
                "<image name=[Image #1] path=/private/SECRET.png>",
                "</image>",
            ],
            "browser": [
                '\n<in-app-browser-context source="ambient-ui-state">\n# In app browser:\n'
                "SECRET page\n</in-app-browser-context>\n## My request:\nmerge it"
            ],
            "ide": [
                "# Context from my IDE setup:\n\n## Active file: SECRET.py\n\n"
                "## My request for Codex:\nfix the parser"
            ],
        }
        cases["split"] = [
            "# Files mentioned by the user:\n\n## a.pdf: /private/SECRET/a.pdf\n",
            "do it",
        ]
        expected = {
            "files": "Ship the CV",
            "browser": "merge it",
            "ide": "fix the parser",
            "split": "do it",
        }
        for name, parts in cases.items():
            content = [{"type": "input_text", "text": part} for part in parts]
            message = {"type": "message", "role": "user", "content": content}
            self.log(
                profile,
                f"{name}.jsonl",
                [
                    {**meta[0], "payload": {**meta[0]["payload"], "id": name}},
                    {
                        "type": "response_item",
                        "timestamp": "2026-10-20T08:00:01Z",
                        "payload": message,
                    },
                ],
            )
        rows = {row["native_id"]: row for row in scan_profile(profile)}
        self.assertEqual({name: rows[name]["task"] for name in cases}, expected)
        self.assertNotIn("SECRET", json.dumps(list(rows.values())))

    def test_markup_is_not_a_reply_in_any_harness(self):
        claude = self.profile("claude", "claude-markup", text=True)
        base = {"sessionId": "same", "timestamp": "2026-10-20T08:00:00Z"}
        self.log(
            claude,
            "p/session.jsonl",
            [
                {**base, "type": "user", "message": {"content": "Draft the page"}},
                {**base, "type": "assistant", "message": {"content": "Writing the page"}},
                {
                    **base,
                    "type": "assistant",
                    "message": {
                        "content": "<!doctype html><html></html>",
                        "stop_reason": "end_turn",
                    },
                },
            ],
        )
        self.assertEqual(scan_profile(claude)[0]["activity"], "Writing the page")
        codex = self.profile("codex", "codex-markup", text=True)
        rows = self.codex()[:1] + [
            {
                "type": "response_item",
                "timestamp": "2026-10-20T08:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Writing the page"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:02Z",
                "payload": {"type": "task_complete", "last_agent_message": "<!doctype html>"},
            },
        ]
        self.log(codex, "run.jsonl", rows)
        self.assertEqual(scan_profile(codex)[0]["activity"], "Writing the page")

    def test_codex_subagent_is_named_by_its_task_not_the_parent_history(self):
        profile = self.profile("codex", "codex-sub", text=True)
        item = {"type": "response_item", "timestamp": "2026-10-20T08:00:01Z"}
        meta = {
            "id": "child",
            "timestamp": "2026-10-20T08:00:00Z",
            "agent_path": "/root/summary_settings_ui",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent",
                        "agent_path": "/root/summary_settings_ui",
                    }
                }
            },
        }

        def said(role, value):
            kind = "input_text" if role == "user" else "output_text"
            content = [{"type": kind, "text": value}]
            return {**item, "payload": {"type": "message", "role": role, "content": content}}

        assignment = {
            **item,
            "payload": {
                "type": "agent_message",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Message Type: NEW_TASK\nTask name: /root/summary_settings_ui\n"
                        "Sender: /root\nPayload:\n",
                    },
                    {"type": "encrypted_content", "encrypted_content": "SECRET"},
                ],
            },
        }
        call = {**item, "payload": {"type": "function_call", "arguments": "SECRET args"}}
        inherited = [said("user", "Parent request"), said("assistant", "Parent reply")]
        start = [{"type": "session_meta", "payload": meta}, *inherited, assignment, call]
        self.log(profile, "child.jsonl", start)
        row = scan_profile(profile)[0]
        self.assertEqual(
            (row["name"], row["task"], row["activity"]),
            ("summary settings ui", "", "Tool call recorded"),
        )
        self.log(profile, "child.jsonl", [*start, said("assistant", "Wiring the settings form")])
        self.assertEqual(scan_profile(profile)[0]["activity"], "Wiring the settings form")
        hidden = SourceProfile(profile.id + "-h", "codex", "Hidden", profile.root, False)
        self.assertNotIn("summary", scan_profile(hidden)[0]["name"])

    def test_codex_inter_agent_messages_are_not_replies(self):
        profile = self.profile("codex", "codex-agents", text=True)
        item = {"type": "response_item", "timestamp": "2026-10-20T08:00:01Z"}
        envelope = "Message Type: MESSAGE\nTask name: /root\nSender: /root/worker\nPayload:\n"
        rows = self.codex()[:1] + [
            {
                **item,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Splitting the parser work"}],
                },
            },
            {
                **item,
                "payload": {
                    "type": "agent_message",
                    "content": [{"type": "input_text", "text": envelope}],
                },
            },
            {**item, "payload": {"type": "function_call", "arguments": "SECRET args"}},
        ]
        self.log(profile, "run.jsonl", rows)
        self.assertEqual(scan_profile(profile)[0]["activity"], "Splitting the parser work")

    def test_native_titles_name_sessions_without_summaries(self):
        claude = self.profile("claude", "claude-titled", text=True)
        rows = self.claude() + [
            {"type": "ai-title", "aiTitle": "Generated title", "sessionId": "same"},
            {"type": "custom-title", "customTitle": "My rename", "sessionId": "same"},
            {"type": "ai-title", "aiTitle": "Later generated title", "sessionId": "same"},
        ]
        self.log(claude, "p/session.jsonl", rows)
        self.assertEqual(scan_profile(claude)[0]["name"], "My rename")
        hidden = SourceProfile(
            claude.id + "-h", "claude", "Hidden", claude.root, include_text=False
        )
        self.assertNotEqual(scan_profile(hidden)[0]["name"], "My rename")
        codex = self.profile("codex", "codex-titled", text=True)
        self.log(codex, "run.jsonl", self.codex("thread-1"))
        index = Path(codex.root) / "session_index.jsonl"
        index.write_text(json.dumps({"id": "thread-1", "thread_name": "Fix retries"}) + "\n")
        self.assertEqual(scan_profile(codex)[0]["name"], "Fix retries")
        index.write_text(
            json.dumps({"id": "thread-1", "thread_name": "Fix retries for good"}) + "\n"
        )
        os.utime(index, (1, 1))
        self.assertEqual(scan_profile(codex)[0]["name"], "Fix retries for good")

    def test_malformed_partial_tails_and_symlink_skipped(self):
        profile = self.profile()
        self.log(profile, "good.jsonl", self.codex(), b'{bad}\n{"type":"event_msg"')
        outside = self.root / "outside.jsonl"
        outside.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "escaped"}}) + "\n"
        )
        folder = Path(profile.root) / "sessions"
        (folder / "link.jsonl").symlink_to(outside)
        (folder / "dirlink").symlink_to(self.root, target_is_directory=True)
        output = scan_profile(profile)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["native_id"], "same")

    def test_recent_file_limit_and_large_middle_ignored(self):
        profile = self.profile()
        for i in range(3):
            path = self.log(profile, f"{i}.jsonl", self.codex(str(i)))
            os.utime(path, (100 + i, 100 + i))
        self.assertEqual({row["native_id"] for row in scan_profile(profile, limit=2)}, {"1", "2"})
        rows = self.codex("large")
        rows += [{"type": "ignored", "secret": "x" * 900_000}]
        rows += [
            {
                "type": "event_msg",
                "timestamp": "2026-10-20T08:00:10Z",
                "payload": {"type": "task_complete"},
            }
        ]
        self.log(profile, "large.jsonl", rows)
        self.assertEqual(scan_profile(profile, limit=1)[0]["state"], "idle")

    def test_max_age_skips_old_transcripts_without_reading_them(self):
        import time

        profile = self.profile()
        old = self.log(profile, "old.jsonl", self.codex("old"))
        os.utime(old, (time.time() - 10 * 86400,) * 2)
        self.log(profile, "new.jsonl", self.codex("new"))
        coverage = {}
        with patch("spiritboard.harnesses.codex.records", wraps=transcripts.records) as records:
            rows = scan_profile(profile, max_age_days=7, diagnostics=coverage)
        self.assertEqual({row["native_id"] for row in rows}, {"new"})
        self.assertEqual(records.call_count, 1)
        self.assertEqual(coverage["aged_out"], 1)
        self.assertFalse(coverage["truncated"])
        self.assertEqual({row["native_id"] for row in scan_profile(profile)}, {"old", "new"})

    def test_discovery_deduplicates_root_aliases(self):
        (self.root / ".codex-switch/profiles/personal/sessions").mkdir(parents=True)
        (self.root / ".codex-switch/profiles/work/sessions").mkdir(parents=True)
        (self.root / ".codex").symlink_to(
            self.root / ".codex-switch/profiles/personal", target_is_directory=True
        )
        (self.root / ".claude-personal/projects").mkdir(parents=True)
        (self.root / ".claude-work/projects").mkdir(parents=True)
        (self.root / ".claude-alias").symlink_to(
            self.root / ".claude-personal", target_is_directory=True
        )
        profiles = discover_profiles(self.root)
        self.assertEqual(len(profiles), 4)
        self.assertEqual([p.kind for p in profiles].count("codex"), 2)

    def test_named_portable_profiles_and_copies_are_not_account_deduplicated(self):
        from spiritboard.store import Store

        for name, kind in [
            (".codex", "codex"),
            (".codex-client-red", "codex"),
            (".claude", "claude"),
            (".claude-client-blue", "claude"),
        ]:
            root = self.root / name
            (root / ("sessions" if kind == "codex" else "projects")).mkdir(parents=True)
            profile = SourceProfile("temporary", kind, "temporary", str(root))
            self.log(profile, "session.jsonl", self.codex() if kind == "codex" else self.claude())
            (root / "auth.json").write_text("PRIVATE CREDENTIAL FIXTURE")
        (self.root / "unrelated/sessions").mkdir(parents=True)
        (self.root / ".codex-not-a-profile").mkdir()
        with patch.object(
            Path, "read_text", side_effect=AssertionError("Discovery must not read credentials")
        ):
            profiles = discover_profiles(self.root)
        self.assertEqual(len(profiles), 4)
        self.assertEqual(len({p.root for p in profiles}), 4)
        store = Store()
        try:
            monitor = Monitor(profiles, store)
            self.assertFalse(monitor.scan_once()["errors"])
            self.assertEqual(store.snapshot()["total_runs"], 4)
        finally:
            store.close()

    def test_automatic_monitor_discovers_new_profiles_and_retains_missing_warning(self):
        from spiritboard.store import Store

        store = Store()
        monitor = Monitor(None, store)
        try:
            with patch(
                "spiritboard.monitor.discover_profiles",
                side_effect=lambda: discover_profiles(self.root),
            ):
                self.assertEqual(monitor.scan_once()["counts"], {})
                self.assertTrue(monitor.public_state()["automatic_discovery"])
                marker = self.root / ".codex-new/sessions"
                marker.mkdir(parents=True)
                profile = SourceProfile("fixture", "codex", "Fixture", str(marker.parent))
                path = self.log(profile, "new.jsonl", self.codex())
                self.assertFalse(monitor.scan_once()["errors"])
                state = monitor.public_state()
                self.assertEqual(len(state["profiles"]), 1)
                self.assertEqual(state["source_stats"][0]["files_seen"], 1)
                self.assertEqual(store.snapshot()["total_runs"], 1)
                path.unlink()
                marker.rmdir()
                result = monitor.scan_once()
                self.assertEqual(len(monitor.profiles), 1)
                self.assertEqual(len(result["errors"]), 1)
                self.assertTrue(monitor.source_stats[0]["unavailable"])
                self.assertNotIn(str(self.root), json.dumps(monitor.public_state()))
        finally:
            store.close()

    def test_explicit_monitor_does_not_discover_other_profiles(self):
        from spiritboard.store import Store

        store = Store()
        try:
            with patch(
                "spiritboard.monitor.discover_profiles",
                side_effect=AssertionError("Must remain manual"),
            ):
                monitor = Monitor([], store)
                self.assertFalse(monitor.scan_once()["errors"])
                self.assertFalse(monitor.public_state()["automatic_discovery"])
        finally:
            store.close()

    def test_environment_selected_custom_roots_are_discovered(self):
        codex = self.root / "custom-codex-location"
        claude = self.root / "custom-claude-location"
        (codex / "sessions").mkdir(parents=True)
        (claude / "projects").mkdir(parents=True)
        with (
            patch.object(Path, "home", return_value=self.root),
            patch.dict(os.environ, {"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}),
        ):
            profiles = discover_profiles()
        self.assertEqual({p.kind for p in profiles}, {"codex", "claude"})
        self.assertEqual(len(profiles), 2)

    def test_inventory_reports_selection_and_traversal_bounds(self):
        profile = self.profile()
        for i in range(3):
            self.log(profile, str(i) + ".jsonl", self.codex(str(i)))
        diagnostics = {}
        self.assertEqual(len(scan_profile(profile, limit=1, diagnostics=diagnostics)), 1)
        self.assertEqual(diagnostics["files_seen"], 3)
        self.assertEqual(diagnostics["selected_files"], 1)
        self.assertTrue(diagnostics["truncated"])
        self.assertFalse(diagnostics["discovery_limited"])
        self.assertNotIn("root", diagnostics)
        with patch("spiritboard.transcripts.MAX_ENTRIES", 1):
            bounded = scan_inventory(profile)
            self.assertTrue(bounded["entry_budget_hit"])
            self.assertTrue(bounded["discovery_limited"])
        self.log(profile, "nested/deep.jsonl", self.codex("deep"))
        with patch("spiritboard.transcripts.MAX_DEPTH", 0):
            bounded = scan_inventory(profile)
            self.assertTrue(bounded["depth_budget_hit"])
            self.assertTrue(bounded["truncated"])
        (Path(profile.root) / "sessions/alias.jsonl").symlink_to(self.root / "not-followed.jsonl")
        self.assertTrue(scan_inventory(profile)["discovery_limited"])

    def test_archived_only_codex_profile_and_combined_newest_selection(self):
        root = self.root / ".codex-archived"
        archive = root / "archived_sessions"
        archive.mkdir(parents=True)
        old = archive / "old.jsonl"
        old.write_text("".join(json.dumps(r) + "\n" for r in self.codex("old")))
        profiles = discover_profiles(self.root)
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(scan_profile(profile)[0]["native_id"], "old")
        self.assertEqual(scan_inventory(profile)["transcript_trees"], ["archived_sessions"])
        (root / "sessions").mkdir()
        active = self.log(profile, "new.jsonl", self.codex("new"))
        os.utime(old, (1, 1))
        os.utime(active, (2, 2))
        diagnostics = {}
        rows = scan_profile(profile, limit=1, diagnostics=diagnostics)
        self.assertEqual(rows[0]["native_id"], "new")
        self.assertEqual(diagnostics["files_seen"], 2)
        self.assertEqual(diagnostics["tree_count"], 2)
        self.assertEqual(set(diagnostics["transcript_trees"]), {"sessions", "archived_sessions"})
        self.assertTrue(diagnostics["truncated"])
        self.assertEqual({r["native_id"] for r in scan_profile(profile)}, {"new", "old"})
        self.assertNotIn(str(root), json.dumps(diagnostics))

    def test_config_validation(self):
        path = self.root / "sources.json"
        path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {"id": "test", "kind": "codex", "root": "/tmp/fake", "label": "Personal"}
                    ]
                }
            )
        )
        self.assertTrue(load_profiles(path)[0].include_text)
        path.write_text(
            json.dumps(
                [
                    {
                        "id": "test",
                        "kind": "codex",
                        "root": "/tmp/fake",
                        "label": "Personal",
                        "token": "SECRET",
                    }
                ]
            )
        )
        with self.assertRaises(ValueError):
            load_profiles(path)

    def test_cached_observation_uses_current_profile_label(self):
        profile = self.profile()
        self.log(profile, "run.jsonl", self.codex())
        self.assertEqual(scan_profile(profile)[0]["account"], "one")
        renamed = SourceProfile(profile.id, profile.kind, "Renamed", profile.root)
        self.assertEqual(scan_profile(renamed)[0]["account"], "Renamed")

    def test_monitor_source_failure_isolated_and_no_paths_in_errors(self):
        good = self.profile()
        self.log(good, "run.jsonl", self.codex())
        missing = SourceProfile("missing", "claude", "Other", str(self.root / "SECRET"))

        class Store:
            def __init__(self):
                self.rows = []

            def observe(self, value):
                self.rows.append(value)

        store = Store()
        monitor = Monitor([good, missing], store)
        result = monitor.scan_once()
        self.assertEqual(result["counts"], {"one": 1, "missing": 0})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(len(store.rows), 1)
        self.assertNotIn("root", monitor.public_profiles()[0])
        monitor.start()
        monitor.stop()


if __name__ == "__main__":
    unittest.main()
