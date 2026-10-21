import builtins
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus, processes
from spiritboard.focus import Step, Target
from spiritboard.harnesses import claude, codex
from spiritboard.processes import Process
from spiritboard.sources import SourceProfile

CLAUDE_ID = "11111111-2222-4333-8444-555555555555"
OTHER_ID = "22222222-2222-4333-8444-555555555555"
CODEX_ID = "019a0000-aaaa-7bbb-8ccc-dddddddddddd"
START = "Tue Oct 20 10:00:00 2026"


class FakeSystem:
    """Answers ps/lsof argv lists from invented processes; records every call."""

    def __init__(self, processes=None, lsof=None, env_output="", args_output="", ps_ok=True):
        self.processes = processes or {}
        self.lsof = lsof
        self.env_output = env_output
        self.args_output = args_output
        self.ps_ok = ps_ok
        self.calls = []

    def __call__(self, argv, env=None):
        self.calls.append((list(argv), env))
        if argv[:2] == ["ps", "eww"]:
            return 0, self.env_output
        if argv[:2] == ["ps", "-ww"]:
            return 0, self.args_output
        if argv[0] == "ps" and not self.ps_ok:
            return None
        if argv[0] == "ps":
            process = self.processes.get(int(argv[-1]))
            if not process:
                return 1, ""
            return 0, f"{process.ppid:>5} {process.tty:<8} {process.start}     {process.command}\n"
        if argv[0].endswith("lsof"):
            return self.lsof
        return 0, ""


def proc(pid, ppid=1, command="/usr/local/bin/claude", tty="ttys003", start=START):
    return Process(pid, ppid, tty, start, command)


def write_session(root, pid, session_id, proc_start=START):
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": session_id, "cwd": "/invented/project"}
    if proc_start is not None:
        data["procStart"] = proc_start
    (root / "sessions" / f"{pid}.json").write_text(json.dumps(data))


class ClaudeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / ".claude-work"
        self.addCleanup(self.folder.cleanup)

    def test_live_stale_and_reused_pids(self):
        write_session(self.root, 111, CLAUDE_ID)
        write_session(self.root, 222, OTHER_ID)
        write_session(
            self.root, 333, "33333333-2222-4333-8444-555555555555", "Mon Oct 19 01:00:00 2026"
        )
        system = FakeSystem({111: proc(111), 333: proc(333)})
        with patch.object(processes, "run", system):
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (True, 111))
            self.assertEqual(claude.session_pid(self.root, OTHER_ID), (True, None))
            self.assertEqual(
                claude.session_pid(self.root, "33333333-2222-4333-8444-555555555555"), (True, None)
            )
            self.assertEqual(claude.session_pid(self.root, "missing-0000"), (True, None))
        self.assertIn("TZ", system.calls[0][1])
        self.assertEqual(system.calls[0][1]["TZ"], "UTC")

    def test_pid_mismatch_and_missing_directory(self):
        (self.root / "sessions").mkdir(parents=True)
        (self.root / "sessions" / "111.json").write_text(
            json.dumps({"pid": 999, "sessionId": CLAUDE_ID})
        )
        with patch.object(processes, "run", FakeSystem({111: proc(111), 999: proc(999)})):
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))
            self.assertEqual(claude.session_pid(self.root / "absent", CLAUDE_ID), (False, None))

    def test_unreadable_or_unattributable_session_metadata_is_unknown(self):
        write_session(self.root, 111, CLAUDE_ID)
        path = self.root / "sessions" / "111.json"
        for raw in ("not json", "{}", '{"pid": 111}', '{"pid": 111, "sessionId": 5}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with patch.object(processes, "run", FakeSystem({111: proc(111)})):
                    self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))
        with patch.object(claude, "small_json", return_value=None):
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))

    def test_missing_proc_start_and_ps_failure_are_not_trusted(self):
        write_session(self.root, 111, CLAUDE_ID, proc_start=None)
        with patch.object(processes, "run", FakeSystem({111: proc(111)})):
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))
        write_session(self.root, 111, CLAUDE_ID)
        with patch.object(processes, "run", FakeSystem({111: proc(111)}, ps_ok=False)):
            with self.assertRaises(OSError):
                claude.session_pid(self.root, CLAUDE_ID)

    def test_process_lookup_error_is_not_confirmed_absence(self):
        write_session(self.root, 111, CLAUDE_ID)
        with patch.object(processes, "run", return_value=(2, "")):
            with self.assertRaises(OSError):
                claude.session_pid(self.root, CLAUDE_ID)

    def test_unverifiable_start_is_unknown_but_absent_pid_is_stopped(self):
        for recorded in (None, "", "not a process timestamp"):
            with self.subTest(recorded=recorded):
                write_session(self.root, 111, CLAUDE_ID, proc_start=recorded)
                with patch.object(processes, "run", FakeSystem({111: proc(111)})):
                    self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))
                with patch.object(processes, "run", FakeSystem()):
                    self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (True, None))

    def test_unverifiable_observed_process_start_is_unknown(self):
        write_session(self.root, 111, CLAUDE_ID)
        with patch.object(
            processes, "run", FakeSystem({111: proc(111, start="Bad Bad 20 10:00:00 2026")})
        ):
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (False, None))

    def test_key_files_are_never_opened(self):
        write_session(self.root, 111, CLAUDE_ID)
        key = self.root / "sessions" / "111.abcdef.key"
        key.write_text("invented secret")
        (self.root / "sessions" / "111.json.key").write_text("invented secret")
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def guard_open(path, *args, **kwargs):
            opened.append(str(path))
            assert not str(path).endswith(".key")
            return real_open(path, *args, **kwargs)

        def guard_os_open(path, *args, **kwargs):
            opened.append(str(path))
            assert not str(path).endswith(".key")
            return real_os_open(path, *args, **kwargs)

        with (
            patch.object(processes, "run", FakeSystem({111: proc(111)})),
            patch("builtins.open", guard_open),
            patch("os.open", guard_os_open),
        ):
            self.assertEqual(claude.session_pid(self.root, OTHER_ID), (True, None))
            self.assertEqual(claude.session_pid(self.root, CLAUDE_ID), (True, 111))
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if p.endswith(".key")])


class CodexResolutionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "profiles" / "work"
        day = self.root / "sessions" / "2026" / "10" / "20"
        day.mkdir(parents=True)
        self.rollout = day / f"rollout-2026-10-20T10-00-00-{CODEX_ID}.jsonl"
        meta = {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/invented/my app"}}
        self.rollout.write_text(json.dumps(meta) + "\n")

    def test_rollout_holder_found_by_lsof(self):
        system = FakeSystem({4242: proc(4242, command="/opt/bin/codex")}, lsof=(0, "4242\n"))
        with (
            patch.object(processes, "run", system),
            patch.object(focus.shutil, "which", return_value="/usr/sbin/lsof"),
        ):
            self.assertEqual(codex.find_rollout(self.root, CODEX_ID), self.rollout)
            self.assertIsNone(codex.find_rollout(self.root, CODEX_ID[:-4]))
            self.assertEqual(codex.rollout_pid(self.root, self.rollout), (True, 4242))
        self.assertIn(
            ["/usr/sbin/lsof", "-t", "--", str(self.rollout)], [c[0] for c in system.calls]
        )

    def test_not_open_unknown_and_archived(self):
        with patch.object(focus.shutil, "which", return_value="/usr/sbin/lsof"):
            with patch.object(processes, "run", FakeSystem(lsof=(1, ""))):
                self.assertEqual(codex.rollout_pid(self.root, self.rollout), (True, None))
            with patch.object(processes, "run", FakeSystem(lsof=None)):
                self.assertEqual(codex.rollout_pid(self.root, self.rollout), (False, None))
            editor = FakeSystem({7: proc(7, command="/usr/bin/vim")}, lsof=(0, "7\n"))
            with patch.object(processes, "run", editor):
                self.assertEqual(codex.rollout_pid(self.root, self.rollout), (False, None))
            with patch.object(processes, "run", FakeSystem(lsof=(1, ""))):
                gone = self.rollout.with_name("gone.jsonl")
                self.assertEqual(codex.rollout_pid(self.root, gone), (False, None))
        with patch.object(focus.shutil, "which", return_value=None):
            self.assertEqual(codex.rollout_pid(self.root, self.rollout), (False, None))
        archived = self.root / "archived_sessions" / self.rollout.name
        archived.parent.mkdir()
        with patch.object(processes, "run", side_effect=AssertionError("no process lookup")):
            self.assertEqual(codex.rollout_pid(self.root, archived), (True, None))


class EnvironmentTests(unittest.TestCase):
    def test_only_selected_valid_keys_and_last_occurrence(self):
        args = "/usr/bin/claude --flag TMUX_PANE=%9 __CFBundleIdentifier=com.invented.app"
        output = (
            f"{args} OPENROUTER_API_KEY=sk-or-invented "
            "CMUX_WORKSPACE_ID=--evil CMUX_PANEL_ID=ABCD-1234 TMUX_PANE=%3 "
            "ITERM_SESSION_ID=w0t1p0:not-a-guid TERM_PROGRAM=iTerm.app HOME=/invented\n"
        )
        with patch.object(focus.sys, "platform", "darwin"):
            with patch.object(processes, "run", FakeSystem(env_output=output, args_output=args)):
                env = focus._environment(1)
            with patch.object(processes, "run", FakeSystem(env_output=output, args_output="/x")):
                self.assertEqual(focus._environment(1), {})
        self.assertEqual(
            env, {"CMUX_PANEL_ID": "ABCD-1234", "TMUX_PANE": "%3", "TERM_PROGRAM": "iTerm.app"}
        )


class AdapterTests(unittest.TestCase):
    def pick(self, env, chain=None, platform="darwin", sockets=True, which=None, cli="/cmux"):
        chain = chain or [proc(10)]
        tools = {"tmux": "/bin/tmux", **(which or {})}
        with (
            patch.object(focus, "_lineage", return_value=chain),
            patch.object(focus, "_environment", return_value=env),
            patch.object(focus.sys, "platform", platform),
            patch.object(focus, "_is_socket", return_value=sockets),
            patch.object(focus, "_cmux_cli", return_value=cli),
            patch.object(focus.shutil, "which", side_effect=lambda name: tools.get(name)),
        ):
            return focus.adapter(10)

    def test_cmux_panel_first(self):
        target = self.pick(
            {
                "CMUX_WORKSPACE_ID": "WS-1",
                "CMUX_PANEL_ID": "PANEL-1",
                "CMUX_SOCKET_PATH": "/tmp/cmux.sock",
                "TMUX": "/tmp/tmux-1/default,1,0",
                "TMUX_PANE": "%2",
            }
        )
        self.assertEqual(target.label, "cmux")
        self.assertEqual(target.steps[0].argv, ("/cmux", "select-workspace", "--workspace", "WS-1"))
        self.assertEqual(
            target.steps[1].argv,
            ("/cmux", "focus-panel", "--panel", "PANEL-1", "--workspace", "WS-1"),
        )
        self.assertFalse(any(s.required for s in target.steps[2:]))
        self.assertEqual(target.env, (("CMUX_SOCKET_PATH", "/tmp/cmux.sock"),))
        self.assertEqual(target.public(), {"available": True, "kind": "focus", "label": "cmux"})

    def test_tmux_pane_then_terminal_app(self):
        env = {
            "TMUX": "/tmp/tmux-1/default,1,0",
            "TMUX_PANE": "%7",
            "__CFBundleIdentifier": "com.googlecode.iterm2",
            "ITERM_SESSION_ID": "w0t0p0:AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
        }
        target = self.pick(env)
        self.assertEqual(target.label, "tmux")
        argv = [s.argv for s in target.steps]
        self.assertIn(("/bin/tmux", "-S", "/tmp/tmux-1/default", "select-pane", "-t", "%7"), argv)
        self.assertEqual(argv[-1], ("/usr/bin/open", "-b", "com.googlecode.iterm2"))
        self.assertEqual(self.pick(env, sockets=False).label, "iTerm")

    def test_iterm_terminal_and_generic(self):
        guid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        target = self.pick({"ITERM_SESSION_ID": f"w0t0p0:{guid}"})
        self.assertEqual(target.label, "iTerm")
        self.assertEqual(target.steps[0].argv[0], "/usr/bin/osascript")
        self.assertEqual(target.steps[0].argv[-2:], (guid, "/dev/ttys003"))
        target = self.pick({"TERM_PROGRAM": "Apple_Terminal"})
        self.assertEqual(target.label, "Terminal")
        self.assertEqual(target.steps[0].argv[-1], "/dev/ttys003")
        self.assertEqual(
            self.pick({"TERM_PROGRAM": "Apple_Terminal"}, [proc(10, tty="??")]), focus.UNSUPPORTED
        )
        target = self.pick(
            {"TERM_PROGRAM": "vscode", "__CFBundleIdentifier": "com.microsoft.VSCode"}
        )
        self.assertEqual(target.label, "VS Code")
        self.assertEqual(target.steps, (Step(("/usr/bin/open", "-b", "com.microsoft.VSCode")),))

    def test_ancestor_app_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "ChatGPT.app"
            (app / "Contents").mkdir(parents=True)
            chain = [proc(10, 9, f"{app}/Contents/Resources/codex-cli/codex", "??"), proc(9)]
            target = self.pick({}, chain)
        self.assertEqual(target.label, "ChatGPT")
        self.assertEqual(target.steps[0].argv, ("/usr/bin/open", "-a", str(app)))

    def test_linux_tmux_window_or_unsupported(self):
        env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1", "WINDOWID": "42"}
        target = self.pick(env, platform="linux", which={"wmctrl": "/bin/wmctrl"})
        self.assertEqual(target.label, "tmux")
        self.assertEqual(target.steps[-1].argv, ("/bin/wmctrl", "-i", "-a", "0x2a"))
        target = self.pick({"WINDOWID": "42"}, platform="linux", which={"wmctrl": "/bin/wmctrl"})
        self.assertEqual(target.label, "window")
        self.assertEqual(
            self.pick({"__CFBundleIdentifier": "com.apple.Terminal"}, platform="linux"),
            focus.UNSUPPORTED,
        )
        with patch.object(focus, "_lineage", return_value=[]):
            self.assertEqual(focus.adapter(10), focus.UNSUPPORTED)


class ResumeAndResolveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name).resolve()

    def test_resume_commands_for_default_and_custom_roots(self):
        home = self.home
        self.assertEqual(
            claude.resume_command(home / ".claude", CLAUDE_ID, "/invented/my app"),
            f"cd '/invented/my app' && CLAUDE_CONFIG_DIR={home / '.claude'} "
            f"claude --resume {CLAUDE_ID}",
        )
        self.assertEqual(
            claude.resume_command(home / ".claude-work", CLAUDE_ID, ""),
            f"CLAUDE_CONFIG_DIR={home / '.claude-work'} claude --resume {CLAUDE_ID}",
        )
        self.assertEqual(
            codex.resume_command(home / ".codex", CODEX_ID, ""),
            f"CODEX_HOME={home / '.codex'} codex resume {CODEX_ID}",
        )
        self.assertEqual(
            codex.resume_command(home / "odd dir", CODEX_ID, "/p"),
            f"cd /p && CODEX_HOME='{home / 'odd dir'}' codex resume {CODEX_ID}",
        )

    def profile(self, kind, name):
        return SourceProfile(f"{kind}-test", kind, "Test", str(self.home / name))

    def run_for(self, profile, native, **extra):
        return {
            "mode": "observation",
            "source": profile.kind,
            "session_id": f"{profile.id}:observed",
            "native_id": native,
            "parent_run_id": None,
            **extra,
        }

    def test_eligibility(self):
        profile = self.profile("claude", ".claude-work")
        self.assertEqual(focus.resolve(None, [profile]), focus.UNSUPPORTED)
        sdk = {"mode": "sdk", "source": "sdk", "session_id": "s", "native_id": CLAUDE_ID}
        self.assertEqual(focus.resolve(sdk, [profile]), focus.UNSUPPORTED)
        self.assertEqual(focus.resolve(self.run_for(profile, CLAUDE_ID), []), focus.UNSUPPORTED)
        self.assertEqual(
            focus.resolve(self.run_for(profile, "--evil"), [profile]), focus.UNSUPPORTED
        )
        wrong = {**self.run_for(profile, CLAUDE_ID), "source": "codex"}
        self.assertEqual(focus.resolve(wrong, [profile]), focus.UNSUPPORTED)

    def test_claude_live_subagent_focuses_parent_and_stopped_offers_resume(self):
        profile = self.profile("claude", ".claude-work")
        root = Path(profile.root)
        write_session(root, 111, CLAUDE_ID)
        project = root / "projects" / "-invented-project"
        project.mkdir(parents=True)
        (project / f"{OTHER_ID}.jsonl").write_text(json.dumps({"cwd": "/invented/project"}) + "\n")
        live = Target("focus", "cmux")
        with (
            patch.object(processes, "run", FakeSystem({111: proc(111)})),
            patch.object(focus, "adapter", return_value=live) as adapter,
        ):
            run = self.run_for(profile, f"{CLAUDE_ID}:agent:abc123", parent_run_id="p")
            self.assertEqual(focus.resolve(run, [profile]), live)
            adapter.assert_called_once_with(111, [proc(111)])
            target = focus.resolve(self.run_for(profile, OTHER_ID), [profile])
            stopped_sub = self.run_for(profile, f"{OTHER_ID}:agent:abc123", parent_run_id="p")
            self.assertEqual(focus.resolve(stopped_sub, [profile]), focus.UNSUPPORTED)
        self.assertEqual(target.kind, "resume")
        self.assertEqual(
            target.command,
            f"cd /invented/project && CLAUDE_CONFIG_DIR={root} claude --resume {OTHER_ID}",
        )
        self.assertEqual(target.public()["command"], target.command)

    def test_claude_without_sessions_directory_is_not_resumable(self):
        profile = self.profile("claude", ".claude")
        (Path(profile.root) / "projects").mkdir(parents=True)
        self.assertEqual(
            focus.resolve(self.run_for(profile, CLAUDE_ID), [profile]), focus.UNSUPPORTED
        )

    def test_claude_live_pid_without_trusted_start_never_offers_resume(self):
        profile = self.profile("claude", ".claude")
        for recorded in (None, "", "not a process timestamp"):
            with self.subTest(recorded=recorded):
                write_session(Path(profile.root), 111, CLAUDE_ID, proc_start=recorded)
                with (
                    patch.object(processes, "run", FakeSystem({111: proc(111)})),
                    patch.object(focus, "adapter") as adapter,
                ):
                    self.assertEqual(
                        focus.resolve(self.run_for(profile, CLAUDE_ID), [profile]),
                        focus.UNSUPPORTED,
                    )
                adapter.assert_not_called()

    def test_claude_untrusted_session_metadata_never_offers_resume(self):
        profile = self.profile("claude", ".claude")
        root = Path(profile.root)
        write_session(root, 111, CLAUDE_ID)
        path = root / "sessions" / "111.json"
        for raw in (
            json.dumps({"pid": 999, "sessionId": CLAUDE_ID, "procStart": START}),
            "not json",
            "{}",
        ):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with patch.object(processes, "run", FakeSystem({111: proc(111)})):
                    self.assertEqual(
                        focus.resolve(self.run_for(profile, CLAUDE_ID), [profile]),
                        focus.UNSUPPORTED,
                    )
        with patch.object(claude, "small_json", return_value=None):
            self.assertEqual(
                focus.resolve(self.run_for(profile, CLAUDE_ID), [profile]), focus.UNSUPPORTED
            )

    def test_claude_process_lookup_error_never_offers_resume(self):
        profile = self.profile("claude", ".claude")
        write_session(Path(profile.root), 111, CLAUDE_ID)
        with patch.object(processes, "run", return_value=(2, "")):
            self.assertEqual(
                focus.resolve(self.run_for(profile, CLAUDE_ID), [profile]), focus.UNSUPPORTED
            )

    def test_codex_stopped_resume_and_unknown(self):
        profile = self.profile("codex", ".codex")
        day = Path(profile.root) / "sessions" / "2026" / "10" / "20"
        day.mkdir(parents=True)
        meta = {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/invented/app"}}
        (day / f"rollout-x-{CODEX_ID}.jsonl").write_text(json.dumps(meta) + "\n")
        run = self.run_for(profile, CODEX_ID)
        with patch.object(focus.shutil, "which", return_value="/usr/sbin/lsof"):
            with patch.object(processes, "run", FakeSystem(lsof=(1, ""))):
                target = focus.resolve(run, [profile])
            with patch.object(processes, "run", FakeSystem(lsof=(2, ""))):
                self.assertEqual(focus.resolve(run, [profile]), focus.UNSUPPORTED)
        self.assertEqual(
            target.command,
            f"cd /invented/app && CODEX_HOME={profile.root} codex resume {CODEX_ID}",
        )
        missing = self.run_for(profile, "019a1111-aaaa-7bbb-8ccc-dddddddddddd")
        self.assertEqual(focus.resolve(missing, [profile]), focus.UNSUPPORTED)


class FocusTests(unittest.TestCase):
    def test_required_and_optional_steps(self):
        calls = []

        def fake(argv, env=None):
            calls.append((argv, env))
            return (1, "ignored output") if argv[0] == "optional" else (0, "")

        target = Target(
            "focus",
            "cmux",
            steps=(Step(("optional",), required=False), Step(("required",))),
            env=(("CMUX_SOCKET_PATH", "/tmp/s"),),
        )
        with patch.object(processes, "run", fake):
            self.assertTrue(focus.focus(target))
            self.assertEqual(calls[-1][1]["CMUX_SOCKET_PATH"], "/tmp/s")
            failing = Target("focus", "x", steps=(Step(("optional",)),))
            self.assertFalse(focus.focus(failing))
            self.assertFalse(focus.focus(Target("resume", "resume", command="claude")))
        self.assertEqual(len(calls), 3)

    def test_run_uses_argv_without_shell(self):
        with patch.object(processes.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, "out"
            self.assertEqual(processes.run(["ps", "-p", "1"]), (0, "out"))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ps", "-p", "1"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], processes.TIMEOUT)
        with patch.object(processes.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(processes.run(["missing"]))


if __name__ == "__main__":
    unittest.main()


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

    def test_claude_live_sessions_need_matching_process_start(self):
        root = self.base / ".claude-work"
        write_session(root, 111, CLAUDE_ID)
        write_session(root, 222, OTHER_ID)
        (root / "sessions" / "111.secret.key").write_text("SECRET")
        profile = SourceProfile("claude-work", "claude", "Claude · work", str(root))
        ps_output = f"  111 {START}\n  222 Mon Oct 19 01:00:00 2026\n"
        with patch.object(processes, "run", return_value=(0, ps_output)) as run:
            self.assertEqual(focus.live_sessions(profile), frozenset({CLAUDE_ID}))
        self.assertEqual(run.call_args.args[0][-1], "111,222")
        self.assertEqual(run.call_args.args[1]["TZ"], "UTC")
        with patch.object(processes, "run", return_value=None):
            self.assertIsNone(focus.live_sessions(profile))
        missing = SourceProfile("claude-none", "claude", "None", str(self.base / "absent"))
        self.assertIsNone(focus.live_sessions(missing))

    def test_claude_live_sessions_with_missing_or_invalid_start_are_unknown(self):
        root = self.base / ".claude-work"
        profile = SourceProfile("claude-work", "claude", "Claude test", str(root))
        for recorded in (None, "", "not a process timestamp"):
            with self.subTest(recorded=recorded):
                write_session(root, 111, CLAUDE_ID, proc_start=recorded)
                with patch.object(processes, "run", return_value=(0, f"111 {START}\n")):
                    self.assertIsNone(focus.live_sessions(profile))
                with patch.object(processes, "run", return_value=(1, "")):
                    self.assertEqual(focus.live_sessions(profile), frozenset())

    def test_claude_live_sessions_unverifiable_process_output_is_unknown(self):
        root = self.base / ".claude-work"
        profile = SourceProfile("claude-work", "claude", "Claude test", str(root))
        write_session(root, 111, CLAUDE_ID)
        for result in ((2, ""), (0, "unparseable output\n"), (0, "")):
            with self.subTest(result=result), patch.object(processes, "run", return_value=result):
                self.assertIsNone(focus.live_sessions(profile))

    def test_claude_live_sessions_symlink_metadata_is_unknown(self) -> None:
        root = self.base / ".claude-work"
        write_session(root, 111, CLAUDE_ID)
        record = root / "sessions" / "111.json"
        target = self.base / "outside-session.json"
        record.replace(target)
        record.symlink_to(target)
        profile = SourceProfile("claude-work", "claude", "Claude test", str(root))
        with (
            patch.object(claude, "small_json", side_effect=AssertionError("Do not read links")),
            patch.object(processes, "run", side_effect=AssertionError("No complete inventory")),
        ):
            self.assertIsNone(focus.live_sessions(profile))

    def test_codex_live_sessions_from_held_thread_locks(self):
        root = self.base / "codex-work"
        locks = root / "thread-writer-locks"
        locks.mkdir(parents=True)
        for name in (
            "01a1aaaa-0000-7000-8000-000000000001",
            "01a1bbbb-0000-7000-8000-000000000002",
        ):
            (locks / f"{name}.lock").touch()
        (locks / ".coordination.lock").touch()
        profile = SourceProfile("codex-work", "codex", "Codex · work", str(root))
        lsof = (
            f"p3233\nccodex\nf12\nn{locks}/01a1aaaa-0000-7000-8000-000000000001.lock\n"
            f"p4000\nctail\nf3\nn{locks}/01a1bbbb-0000-7000-8000-000000000002.lock\n"
        )
        with patch.object(focus.shutil, "which", return_value="/usr/sbin/lsof"):
            with patch.object(processes, "run", return_value=(0, lsof)):
                live = focus.live_sessions(profile)
            self.assertEqual(live, frozenset({"01a1aaaa-0000-7000-8000-000000000001"}))
            with patch.object(processes, "run", return_value=(1, "")):
                self.assertEqual(focus.live_sessions(profile), frozenset())
        with patch.object(focus.shutil, "which", return_value=None):
            self.assertIsNone(focus.live_sessions(profile))
        older = SourceProfile("codex-old", "codex", "Old", str(self.base / "no-locks"))
        self.assertIsNone(focus.live_sessions(older))
