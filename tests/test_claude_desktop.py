import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus, processes
from spiritboard.focus import Step, Target
from spiritboard.harnesses import claude
from spiritboard.processes import Process
from spiritboard.sources import SourceProfile, scan_profile
from spiritboard.store import Store

CLI_ID = "11111111-2222-4333-8444-555555555555"
OTHER_ID = "22222222-2222-4333-8444-555555555555"
LOCAL_ID = "local_0a1b2c3d-invented"
START = "Tue Oct 20 10:00:00 2026"
APP_CLI = (
    "/invented/Application Support/Claude/claude-code/9.9/abc/claude.app/Contents/MacOS/claude"
)


def proc(pid: int, ppid: int = 1, command: str = "/usr/local/bin/claude") -> Process:
    return Process(pid, ppid, "??", START, command)


class FakePs:
    def __init__(self, table: dict[int, Process]) -> None:
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] == "ps" and argv[1] == "-o":
            process = self.table.get(int(argv[-1]))
            if not process:
                return 1, ""
            return 0, f"{process.ppid:>5} {process.tty:<8} {process.start}     {process.command}\n"
        return 0, ""


class DesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name).resolve()
        self.desktop = self.base / "claude-code-sessions"
        self.org = self.desktop / "account-invented" / "org-invented"
        self.org.mkdir(parents=True)
        self.root = self.base / ".claude"
        (self.root / "projects" / "-invented").mkdir(parents=True)
        patcher = patch.object(claude, "desktop_dir", lambda: self.desktop)
        patcher.start()
        self.addCleanup(patcher.stop)
        darwin = patch.object(claude.sys, "platform", "darwin")
        darwin.start()
        self.addCleanup(darwin.stop)

    def record(self, local: str = LOCAL_ID, **fields: object) -> Path:
        data = {"sessionId": local, "cwd": "/invented", "title": "Desktop title", **fields}
        path = self.org / f"{local}.json"
        path.write_text(json.dumps(data))
        return path

    def transcript(self, session: str = CLI_ID, entrypoint: str | None = None, *titles: dict):
        row = {
            "type": "user",
            "sessionId": session,
            "cwd": "/invented/project",
            "timestamp": "2026-10-20T08:00:00Z",
            "message": {"content": "Invented request"},
        }
        if entrypoint:
            row["entrypoint"] = entrypoint
        rows = [row, *titles]
        path = self.root / "projects" / "-invented" / f"{session}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def scan(self, text: bool = True) -> dict:
        profile = SourceProfile(
            "claude-default", "claude", "Claude · default", str(self.root), text
        )
        return {row["native_id"]: row for row in scan_profile(profile)}

    def test_entrypoint_marks_desktop_sessions(self) -> None:
        self.transcript(CLI_ID, "claude-desktop")
        self.transcript(OTHER_ID, "cli")
        rows = self.scan()
        self.assertEqual(rows[CLI_ID]["app"], "Claude desktop")
        self.assertNotIn("app", rows[OTHER_ID])
        self.transcript(CLI_ID, "claude-desktop-3p")
        self.assertEqual(self.scan()[CLI_ID]["app"], "Claude desktop")

    def test_join_by_cli_session_id_and_title_precedence(self) -> None:
        ai = {"type": "ai-title", "aiTitle": "Generated", "sessionId": CLI_ID}
        custom = {"type": "custom-title", "customTitle": "My rename", "sessionId": CLI_ID}
        self.transcript(CLI_ID, None, ai)
        self.assertEqual(self.scan()[CLI_ID]["name"], "Generated")
        self.record(cliSessionId=CLI_ID)
        row = self.scan()[CLI_ID]
        # The join alone marks it, even without an entrypoint in the transcript.
        self.assertEqual((row["name"], row["app"]), ("Desktop title", "Claude desktop"))
        self.assertNotIn("archived", row)
        self.transcript(CLI_ID, None, ai, custom)
        self.assertEqual(self.scan()[CLI_ID]["name"], "My rename")
        self.assertNotEqual(self.scan(text=False)[CLI_ID]["name"], "Desktop title")

    def test_rename_marks_a_user_title_that_survives_the_desktop_title(self) -> None:
        ai = {"type": "ai-title", "aiTitle": "Generated", "sessionId": CLI_ID}
        custom = {"type": "custom-title", "customTitle": "My rename", "sessionId": CLI_ID}
        self.record(cliSessionId=CLI_ID)
        self.transcript(CLI_ID, None, ai)
        self.assertNotIn("title_source", self.scan()[CLI_ID])
        self.transcript(CLI_ID, None, ai, custom)
        row = self.scan()[CLI_ID]
        self.assertEqual((row["name"], row["title_source"]), ("My rename", "user"))
        self.assertEqual(row["app"], "Claude desktop")
        store = Store()
        self.addCleanup(store.close)
        store.observe(row)
        stored = store.run(row["run_id"])
        self.assertEqual((stored["name"], stored["title_source"]), ("My rename", "user"))
        self.assertNotIn("title_source", self.scan(text=False)[CLI_ID])

    def test_metadata_changes_apply_without_transcript_changes(self) -> None:
        self.transcript(CLI_ID)
        path = self.record(cliSessionId=CLI_ID)
        self.assertNotIn("archived", self.scan()[CLI_ID])
        path.write_text(
            json.dumps(
                {
                    "sessionId": LOCAL_ID,
                    "cliSessionId": CLI_ID,
                    "isArchived": True,
                    "title": "Moved",
                }
            )
        )
        os.utime(path, ns=(1, 1))
        row = self.scan()[CLI_ID]
        self.assertIs(row["archived"], True)
        self.assertEqual(row["name"], "Moved")
        store = Store()
        self.addCleanup(store.close)
        store.observe(row)
        stored = store.run(row["run_id"])
        self.assertIs(stored["archived"], True)
        self.assertEqual(stored["app"], "Claude desktop")
        self.assertNotIn("title_source", stored)

    def test_only_valid_local_records_are_read(self) -> None:
        self.record(cliSessionId=CLI_ID)
        self.record("local_bad", cliSessionId="--evil")
        (self.org / "local_mismatch.json").write_text(
            json.dumps({"sessionId": "local_other", "cliSessionId": OTHER_ID})
        )
        (self.org / "local_big.json").write_text(
            json.dumps({"sessionId": "local_big", "pad": "x" * 70_000})
        )
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"sessionId": "local_link", "cliSessionId": OTHER_ID}))
        (self.org / "local_link.json").symlink_to(outside)
        (self.org / "scheduled-tasks.json").write_text("{}")
        (self.desktop / "account-invented" / "linked").symlink_to(
            self.org, target_is_directory=True
        )
        opened: list[str] = []
        real = claude.small_json

        def spy(path: Path, limit: int = 65_536) -> dict | None:
            opened.append(path.name)
            return real(path, limit)

        with patch.object(claude, "small_json", spy):
            index = claude.desktop_sessions()
        self.assertEqual(list(index), [CLI_ID])
        self.assertEqual(index[CLI_ID].local_id, LOCAL_ID)
        self.assertNotIn("scheduled-tasks.json", opened)
        self.assertNotIn("local_link.json", opened)
        self.assertEqual(opened.count(f"{LOCAL_ID}.json"), 1)
        with patch.object(claude, "small_json", spy):
            claude.desktop_sessions()
        # Unchanged records are not re-read on the next scan.
        self.assertEqual(opened.count(f"{LOCAL_ID}.json"), 1)

    def test_missing_metadata_is_skipped(self) -> None:
        self.transcript(CLI_ID, "claude-desktop")
        with patch.object(claude, "desktop_dir", lambda: self.base / "absent"):
            self.assertEqual(claude.desktop_sessions(), {})
            self.assertEqual(self.scan()[CLI_ID]["app"], "Claude desktop")

    def test_open_app_deep_link_and_fallbacks(self) -> None:
        self.record(cliSessionId=CLI_ID)
        link = ("/usr/bin/open", f"claude://code/continue?session={LOCAL_ID}")
        activate = ("/usr/bin/open", "-b", "com.anthropic.claudefordesktop")
        self.assertEqual(claude.open_app(CLI_ID, "Claude desktop", []), ("Claude", link))
        self.assertEqual(claude.open_app(OTHER_ID, "Claude desktop", []), ("Claude", activate))
        # Ancestry identifies a desktop session even without the transcript marker.
        chain = [proc(5, 4, APP_CLI), proc(4, 1, "/Applications/Claude.app/Contents/MacOS/Claude")]
        self.assertEqual(claude.open_app(CLI_ID, "", chain[:1]), ("Claude", link))
        self.assertEqual(claude.open_app(CLI_ID, "", chain[1:]), ("Claude", link))
        self.assertIsNone(claude.open_app(CLI_ID, "", [proc(5)]))
        self.record(cliSessionId=CLI_ID, isArchived=True)
        self.assertEqual(claude.open_app(CLI_ID, "Claude desktop", []), ("Claude", activate))
        with patch.object(claude.sys, "platform", "linux"):
            self.assertIsNone(claude.open_app(CLI_ID, "Claude desktop", chain))

    def test_open_app_rejects_ids_the_app_would_not_accept(self) -> None:
        for local in ("local_" + "a" * 65, "local_a.b", "local_a_b"):
            with self.subTest(local=local):
                for path in self.org.iterdir():
                    path.unlink()
                self.record(local, cliSessionId=CLI_ID)
                self.assertEqual(claude.desktop_sessions(), {})
                self.assertEqual(
                    claude.open_app(CLI_ID, "Claude desktop", [])[1][-1],
                    "com.anthropic.claudefordesktop",
                )

    def test_resolve_opens_desktop_sessions_in_the_app_never_the_bundled_cli(self) -> None:
        self.record(cliSessionId=CLI_ID)
        (self.root / "sessions").mkdir()
        data = {"pid": 5, "sessionId": CLI_ID, "procStart": START, "entrypoint": "claude-desktop"}
        (self.root / "sessions" / "5.json").write_text(json.dumps(data))
        profile = SourceProfile("claude-default", "claude", "Claude · default", str(self.root))
        run = {
            "mode": "observation",
            "source": "claude",
            "session_id": "claude-default:observed",
            "native_id": CLI_ID,
            "parent_run_id": None,
            "app": "Claude desktop",
        }
        fake = FakePs({5: proc(5, 4, APP_CLI), 4: proc(4)})
        with patch.object(processes, "run", fake):
            target = focus.resolve(run, [profile])
            cli = focus.resolve({**run, "app": None}, [profile])
        link = ("/usr/bin/open", f"claude://code/continue?session={LOCAL_ID}")
        self.assertEqual(target, Target("focus", "Claude", steps=(Step(link),)))
        self.assertEqual(cli, target)
        self.assertEqual(target.public(), {"available": True, "kind": "focus", "label": "Claude"})
        # Resolving never runs anything but ps; opening needs an explicit POST.
        self.assertTrue(all(call[0] == "ps" for call in fake.calls))
        with patch.object(processes, "run", FakePs({})) as stopped:
            resume = focus.resolve(run, [profile])
        self.assertEqual(resume.kind, "resume")
        self.assertIn(f"claude --resume {CLI_ID}", resume.command)
        self.assertTrue(all(call[0] == "ps" for call in stopped.calls))


if __name__ == "__main__":
    unittest.main()
