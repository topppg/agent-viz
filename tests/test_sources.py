import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_viz.monitor import Monitor
from agent_viz.sources import (
    SourceProfile,
    discover_profiles,
    load_profiles,
    scan_inventory,
    scan_profile,
)


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
        from agent_viz.store import Store

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
        with patch(
            "agent_viz.sources._records",
            wraps=__import__("agent_viz.sources", fromlist=["_records"])._records,
        ) as records:
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
        from agent_viz.store import Store

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
        from agent_viz.store import Store

        store = Store()
        monitor = Monitor(None, store)
        try:
            with patch(
                "agent_viz.monitor.discover_profiles",
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
        from agent_viz.store import Store

        store = Store()
        try:
            with patch(
                "agent_viz.monitor.discover_profiles",
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
        with patch("agent_viz.sources.MAX_ENTRIES", 1):
            bounded = scan_inventory(profile)
            self.assertTrue(bounded["entry_budget_hit"])
            self.assertTrue(bounded["discovery_limited"])
        self.log(profile, "nested/deep.jsonl", self.codex("deep"))
        with patch("agent_viz.sources.MAX_DEPTH", 0):
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
