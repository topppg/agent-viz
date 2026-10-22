import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus, processes
from spiritboard.harnesses import HARNESSES, pi
from spiritboard.sources import SourceProfile, discover_profiles, scan_profile
from spiritboard.transcripts import TEXT_HIDDEN, Session

SESSION_ID = "019a0000-1111-7222-8333-444444444444"
OTHER_ID = "019a0000-5555-7666-8777-888888888888"
CWD = "/invented/project"


def stamp(second: int) -> str:
    return f"2026-10-20T10:00:{second:02d}.000Z"


class Transcript:
    """Builds a synthetic Pi session: a header line, then entries linked by parentId."""

    def __init__(self, session_id=SESSION_ID, cwd=CWD, parent_session=None):
        head = {"type": "session", "version": 3, "id": session_id, "timestamp": stamp(0)}
        head["cwd"] = cwd
        if parent_session:
            head["parentSession"] = parent_session
        self.rows = [head]
        self.leaf = None
        self.count = 0

    def add(self, entry_type="message", parent="leaf", **fields):
        self.count += 1
        entry_id = f"e{self.count:07d}"
        row = {
            "type": entry_type,
            "id": entry_id,
            "parentId": self.leaf if parent == "leaf" else parent,
            "timestamp": stamp(self.count),
            **fields,
        }
        self.rows.append(row)
        self.leaf = entry_id
        return entry_id

    def user(self, content, parent="leaf"):
        return self.add(parent=parent, message={"role": "user", "content": content})

    def assistant(self, blocks, stop="stop"):
        message = {"role": "assistant", "content": blocks, "stopReason": stop}
        return self.add(message=message)

    def tool_result(self, output="secret tool output"):
        content = [{"type": "text", "text": output}]
        return self.add(message={"role": "toolResult", "toolName": "bash", "content": content})

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        return path


def say(value):
    return [{"type": "text", "text": value}]


def file_name(session_id=SESSION_ID, second=0):
    return f"2026-10-20T10-00-{second:02d}-000Z_{session_id}.jsonl"


class PiTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "agent"
        self.folder = self.root / "sessions" / pi.encoded(CWD)
        self.profile = SourceProfile("pi-test", "pi", "Pi · test", str(self.root))
        self.hidden = SourceProfile("pi-hidden", "pi", "Pi · hidden", str(self.root), False)

    def observe(self, transcript, profile=None, name=None):
        path = transcript.write(self.folder / (name or file_name()))
        info = path.stat()
        session = Session(str(path), path, info.st_size, info.st_mtime_ns, info.st_mtime)
        return pi.observe(profile or self.profile, session)

    def test_markup_is_not_a_reply(self):
        transcript = Transcript()
        transcript.user("Draft the page")
        transcript.assistant(say("Writing the page"), stop="toolUse")
        transcript.assistant(say("<!doctype html><html></html>"))
        self.assertEqual(self.observe(transcript)["activity"], "Writing the page")

    def test_registered_and_encoded_folder_matches_pi(self):
        self.assertIs(HARNESSES["pi"], pi.HARNESS)
        self.assertEqual(pi.encoded("/Users/a/b c"), "--Users-a-b c--")

    def test_title_from_session_info_else_first_request(self):
        t = Transcript()
        t.user("Fix the flaky invented test")
        t.assistant(say("Done."))
        self.assertEqual(self.observe(t)["name"], "Fix the flaky invented test")
        self.assertNotIn("title_source", self.observe(t))
        t.add("session_info", name="Invented rename")
        t.add("session_info", name="Final invented name")
        run = self.observe(t)
        self.assertEqual((run["name"], run["title_source"]), ("Final invented name", "user"))
        self.assertNotIn("title_source", self.observe(t, self.hidden))
        t.add("session_info", name="")
        self.assertEqual(self.observe(t)["name"], "Fix the flaky invented test")
        self.assertNotIn("title_source", self.observe(t))

    def test_state_transitions(self):
        t = Transcript()
        t.add("model_change", provider="invented", modelId="m1")
        t.user("Do the invented thing")
        run = self.observe(t)
        self.assertEqual((run["state"], run["task"]), ("working", "Do the invented thing"))
        self.assertEqual(run["started_at"], "2026-10-20T10:00:00+00:00")
        t.assistant(say("Checking.") + [{"type": "toolCall", "name": "bash"}], "toolUse")
        self.assertEqual(self.observe(t)["state"], "waiting_tool")
        t.tool_result()
        run = self.observe(t)
        self.assertEqual((run["state"], run["activity"]), ("working", "Checking."))
        t.assistant(say("All finished."))
        run = self.observe(t)
        self.assertEqual((run["state"], run["activity"]), ("idle", "All finished."))
        # Usage and labels after a turn do not reopen it.
        t.add("usage", kind="cache_warm")
        self.assertEqual(self.observe(t)["state"], "idle")
        t.user("Another request")
        t.assistant([], "aborted")
        self.assertEqual(self.observe(t)["state"], "idle")
        t.user("Retry")
        t.assistant(say("Provider failed."), "error")
        run = self.observe(t)
        self.assertEqual((run["state"], run["task"]), ("idle", "Retry"))
        self.assertEqual(run["last_seen"], f"2026-10-20T10:00:{t.count:02d}+00:00")

    def test_active_branch_after_rewind(self):
        t = Transcript()
        first = t.user("Original request")
        t.assistant(say("Old reply on abandoned branch"))
        t.user("Abandoned follow-up")
        t.assistant(say("Abandoned answer"))
        t.add("branch_summary", parent=first, fromId=t.leaf, summary="Tried another way")
        t.assistant(say("Rewound reply"), "toolUse")
        run = self.observe(t)
        self.assertEqual(run["task"], "Original request")
        self.assertEqual(run["state"], "waiting_tool")
        self.assertEqual(run["activity"], "Rewound reply")
        context = pi.goal_context(t.rows)
        self.assertEqual([m[0] for m in context.messages], ["user", "assistant"])
        self.assertNotIn("Abandoned", json.dumps(context.messages))
        # A new root after resetLeaf starts a branch with no ancestors.
        t.user("Edited first message", parent=None)
        self.assertEqual(self.observe(t)["task"], "Edited first message")

    def test_no_thinking_or_tool_content_leaks(self):
        t = Transcript()
        t.user("Invented request")
        t.assistant(
            [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "toolCall", "name": "bash", "arguments": {"command": "cat secret"}},
            ],
            "toolUse",
        )
        t.tool_result("tool result body")
        t.assistant([{"type": "thinking", "thinking": "more private reasoning"}])
        run = self.observe(t)
        dumped = json.dumps(run) + json.dumps(pi.goal_context(t.rows).messages)
        for private in ("private reasoning", "cat secret", "tool result body"):
            self.assertNotIn(private, dumped)
        self.assertEqual(run["activity"], "Turn ended · session may resume")

    def test_include_text_false_hides_everything(self):
        t = Transcript()
        t.user("Private request")
        t.add("session_info", name="Private title")
        t.assistant(say("Private reply"))
        run = self.observe(t, self.hidden)
        self.assertEqual(run["task"], TEXT_HIDDEN)
        self.assertEqual(run["name"], f"Pi {SESSION_ID[:8]}")
        self.assertNotIn("Private", json.dumps(run))
        self.assertEqual(scan_profile(self.hidden)[0]["task"], TEXT_HIDDEN)

    def test_fork_header_is_not_a_subagent_and_path_not_exposed(self):
        t = Transcript(parent_session="/invented/agent/sessions/x/original.jsonl")
        t.user("Forked work")
        run = self.observe(t)
        self.assertIsNone(run["parent_run_id"])
        self.assertNotIn("original.jsonl", json.dumps(run))
        self.assertEqual(run["project"], "project")

    def test_rejects_non_pi_files(self):
        self.folder.mkdir(parents=True)
        (self.folder / file_name()).write_text('{"type":"user","sessionId":"abc"}\n')
        self.assertEqual(scan_profile(self.profile), [])

    def test_latest_request_follows_active_branch_beyond_tail(self):
        t = Transcript()
        first = t.user("Branch request")
        t.assistant(say("ok"))
        t.user("Abandoned latest request")
        t.add("branch_summary", parent=first, fromId=t.leaf, summary="x")
        for _ in range(30):
            t.assistant(say("y" * 40_000), "toolUse")
            t.tool_result("z")
        path = t.write(self.folder / file_name())
        info = path.stat()
        session = Session(str(path), path, info.st_size, info.st_mtime_ns, info.st_mtime)
        self.assertEqual(pi.observe(self.profile, session)["task"], TEXT_HIDDEN)
        self.assertEqual(pi.latest_request(session), "Branch request")
        self.assertEqual(scan_profile(self.profile)[0]["task"], "Branch request")

    def test_discovery_default_and_environment_relocation(self):
        home = self.base / "home"
        (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
        custom_agent = self.base / "custom-agent"
        (custom_agent / "sessions").mkdir(parents=True)
        flat = self.base / "flat-sessions"
        t = Transcript()
        t.user("Flat request")
        t.write(flat / file_name())
        env = {"PI_CODING_AGENT_DIR": str(custom_agent), "PI_CODING_AGENT_SESSION_DIR": str(flat)}
        with patch.object(Path, "home", return_value=home), patch.dict(os.environ, env):
            profiles = [p for p in discover_profiles() if p.kind == "pi"]
        with patch.dict(os.environ, env):
            explicit = [p for p in discover_profiles(home) if p.kind == "pi"]
        roots = {Path(p.root) for p in profiles}
        self.assertEqual(
            roots,
            {(home / ".pi/agent").resolve(), custom_agent.resolve(), flat.resolve()},
        )
        self.assertEqual(len(explicit), 1)
        flat_profile = next(p for p in profiles if Path(p.root) == flat.resolve())
        self.assertEqual(flat_profile.label, "Pi · configured sessions")
        self.assertEqual(scan_profile(flat_profile)[0]["task"], "Flat request")
        self.assertFalse(pi.is_root(self.base / "missing"))

    def test_liveness_from_pi_process_cwd(self):
        old = Transcript(OTHER_ID)
        old.user("Older")
        old_path = old.write(self.folder / file_name(OTHER_ID, 1))
        t = Transcript()
        t.user("Newer")
        path = t.write(self.folder / file_name(SESSION_ID, 2))
        os.utime(old_path, (1_000, 1_000))
        ps = (0, "  101 pi\n  102 /usr/local/bin/pi --model x\n  103 vim pi\n  104 node pi\n")
        lsof = (0, f"p101\nfcwd\nn{CWD}\np102\nfcwd\nn/invented/elsewhere\n")

        def system(answer_ps=ps, answer_lsof=lsof):
            def run(argv, env=None):
                return answer_ps if argv[0] == "ps" else answer_lsof

            return run

        with (
            patch.object(processes, "run", system()),
            patch.object(pi.shutil, "which", return_value="/usr/sbin/lsof"),
        ):
            self.assertEqual(pi.pi_processes(), {101: CWD, 102: "/invented/elsewhere"})
            self.assertEqual(pi.live_sessions(self.root), frozenset({SESSION_ID}))
            self.assertEqual(pi.session_pid(self.root, SESSION_ID), (True, 101))
            self.assertEqual(pi.session_pid(self.root, OTHER_ID), (True, None))
            self.assertEqual(
                pi.session_pid(self.root, "019a0000-0000-7000-8000-0000000000ff"), (False, None)
            )
        two = (0, f"p101\nfcwd\nn{CWD}\np102\nfcwd\nn{CWD}\n")
        with (
            patch.object(processes, "run", system(answer_lsof=two)),
            patch.object(pi.shutil, "which", return_value="/usr/sbin/lsof"),
        ):
            self.assertEqual(pi.session_pid(self.root, SESSION_ID), (False, None))
        with patch.object(processes, "run", system(answer_ps=(0, "  7 zsh\n"))):
            self.assertEqual(pi.live_sessions(self.root), frozenset())
            profile = {**self.run_for(path), "session_id": f"{self.profile.id}:observed"}
            target = focus.resolve(profile, [self.profile])
            self.assertEqual(target.kind, "resume")
            self.assertEqual(target.command, pi.resume_command(path.resolve(), CWD))
        with patch.object(processes, "run", return_value=None):
            self.assertIsNone(pi.live_sessions(self.root))
            self.assertEqual(pi.session_pid(self.root, SESSION_ID), (False, None))
        with (
            patch.object(processes, "run", system()),
            patch.object(pi.shutil, "which", return_value=None),
        ):
            self.assertIsNone(pi.live_sessions(self.root))

    def run_for(self, path):
        info = path.stat()
        session = Session(str(path), path, info.st_size, info.st_mtime_ns, info.st_mtime)
        return {**pi.observe(self.profile, session), "mode": "observation"}

    def test_resume_command_is_quoted(self):
        t = Transcript(cwd="/invented/odd dir")
        t.user("x")
        folder = self.root / "sessions" / pi.encoded("/invented/odd dir")
        path = t.write(folder / file_name())
        self.assertEqual(
            pi.resume(self.root, SESSION_ID),
            f"cd '/invented/odd dir' && pi --session '{path}'",
        )
        self.assertIsNone(pi.resume(self.root, OTHER_ID))


if __name__ == "__main__":
    unittest.main()
