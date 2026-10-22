import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus, processes
from spiritboard.harnesses import HARNESSES, dsh
from spiritboard.processes import Process
from spiritboard.sources import SourceProfile, discover_profiles, scan_profile
from spiritboard.transcripts import TEXT_HIDDEN

PARENT = "session-11111111-2222-4333-8444-555555555555"
CHILD = "session-22222222-2222-4333-8444-555555555555"
OTHER = "session-33333333-2222-4333-8444-555555555555"
T0 = 1_792_000_000_000
START = "Tue Oct 20 10:00:00 2026"
DECODER = dsh.decoder
REAL_POPEN = subprocess.Popen
SECRETS = ("SECRET reasoning", "SECRET argument", "SECRET result", "SECRET title prompt")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def plain(stream):
    yield stream.read()


PLAIN = ("plain", plain)


def zstd_compress(data: bytes) -> bytes | None:
    try:
        from compression import zstd

        return zstd.compress(data)
    except ImportError:
        pass
    binary = shutil.which("zstd")
    if not binary:
        return None
    return subprocess.run([binary, "-q", "-c"], input=data, capture_output=True, check=True).stdout


def header(native, cwd="/invented/alpha", parent=None):
    row = {"type": "session", "version": 4, "id": native, "createdAt": T0, "cwd": cwd}
    row.update(isSeeded=False, delegationDepth=1 if parent else 0, agentPreset="default")
    if parent:
        row.update(parentSession=parent, origin="subagent")
    return row


def event(seq, kind, data=None):
    return {"type": kind, "seq": seq, "time": T0 + seq * 1000, "data": data or {}}


def user(seq, text, kind="user"):
    data = {"role": "user", "content": [{"type": "text", "text": text}], "source": {"kind": kind}}
    return event(seq, "user/message", data)


def assistant(seq, *blocks):
    message = {"role": "assistant", "content": list(blocks)}
    return event(seq, "assistant/message", {"turn": 1, "step": 1, "message": message})


def turn_end(seq, kind="completed"):
    return event(seq, "turn/end", {"turn": 1, "reason": {"kind": kind}})


def conversation(native=PARENT):
    return [
        header(native),
        user(1, "SECRET title prompt", "dsh-session-title-llm"),
        user(2, "Fix the flaky login test"),
        event(3, "turn/start", {"turn": 1}),
        assistant(
            4,
            {"type": "reasoning", "text": "SECRET reasoning"},
            {"type": "tool-call", "id": "c1", "name": "shell", "arguments": "SECRET argument"},
        ),
        event(5, "tool/call", {"callId": "c1", "name": "shell"}),
        event(6, "tool/result", {"callId": "c1", "content": "SECRET result"}),
        assistant(7, {"type": "text", "text": "The test is fixed."}),
        event(8, "session/title", {"title": "Login test", "messageSeqs": [2], "source": {}}),
        turn_end(9),
    ]


def lines(rows, extra=b""):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows) + extra


class DshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / ".dsh"
        (self.root / "sessions").mkdir(parents=True)
        patcher = patch.object(dsh, "decoder", return_value=PLAIN)
        self.decoder = patcher.start()
        self.addCleanup(patcher.stop)

    def profile(self, text=True):
        return SourceProfile("dsh-test", "dsh", "DeepSeek Harness", str(self.root), text)

    def write(self, native, rows=None, raw=None):
        folder = self.root / "sessions" / "--invented-alpha--" / native
        folder.mkdir(parents=True, exist_ok=True)
        (folder / dsh.LOCK).touch()
        path = folder / dsh.LOG
        path.write_bytes(raw if raw is not None else lines(rows or conversation(native)))
        return path

    def observe(self, native=PARENT, rows=None, text=True):
        self.write(native, rows)
        found = scan_profile(self.profile(text))
        return next(run for run in found if run["native_id"] == native)

    def test_long_log_head_is_not_the_current_request_or_reply(self):
        filler = [
            event(10 + i, "tool/result", {"callId": "c", "content": "x" * 4000}) for i in range(300)
        ]
        opening = [
            header(PARENT),
            user(1, "Opening request"),
            assistant(2, {"type": "text", "text": "Opening reply"}),
        ]
        run = self.observe(
            rows=opening + filler[:100] + [user(400, "Latest request")] + filler[100:]
        )
        self.assertEqual((run["task"], run["activity"]), ("Latest request", "Tool result recorded"))

    def test_markup_is_not_a_reply(self):
        rows = conversation()
        rows.insert(-2, assistant(7, {"type": "text", "text": "<!doctype html><html></html>"}))
        self.assertEqual(self.observe(rows=rows)["activity"], "The test is fixed.")

    def test_registered_and_discovered(self):
        self.assertIs(HARNESSES["dsh"], dsh.HARNESS)
        home = self.root.parent
        found = [p for p in discover_profiles(home) if p.kind == "dsh"]
        self.assertEqual([p.label for p in found], ["DeepSeek Harness"])
        self.assertEqual(Path(found[0].root), self.root.resolve())
        self.assertIsNone(dsh.HARNESS.resume(self.root, PARENT))

    def test_completed_session_is_idle_with_reply_and_no_leaks(self):
        run = self.observe()
        self.assertEqual(run["state"], "idle")
        self.assertEqual(run["task"], "Fix the flaky login test")
        self.assertEqual(run["activity"], "The test is fixed.")
        self.assertEqual(run["name"], "Login test")
        self.assertEqual(run["project"], "alpha")
        self.assertEqual(run["started_at"], iso(T0))
        self.assertEqual(run["last_seen"], iso(T0 + 9000))
        self.assertIsNone(run["parent_run_id"])
        goal = dsh.goal(dsh.inventory(self.root, 10)[0][0])
        dumped = json.dumps(run) + repr(goal)
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)
        self.assertEqual(goal.title, "Login test")
        self.assertEqual([m[0] for m in goal.messages], ["user", "assistant"])

    def test_states(self):
        base = conversation()[:3]
        cases = {
            "working": [*base, event(3, "turn/start", {"turn": 1})],
            "waiting_tool": [*base, assistant(4, {"type": "tool-call", "id": "c", "name": "x"})],
            "waiting_user": [
                *base,
                event(3, "turn/start"),
                event(4, "approval/asked", {"id": "a1", "toolName": "shell"}),
                event(5, "tool/call", {"callId": "c"}),
            ],
            "idle": [*base, event(3, "turn/start"), turn_end(4, "interrupted")],
        }
        for state, rows in cases.items():
            with self.subTest(state):
                parsed = dsh.parse(self.profile(), rows, 0.0)
                self.assertEqual(parsed["state"], state)
        decided = [
            *cases["waiting_user"],
            event(6, "approval/decided", {"id": "a1", "outcome": "approved"}),
        ]
        self.assertEqual(dsh.parse(self.profile(), decided, 0.0)["state"], "working")
        aborted = [*cases["waiting_user"], turn_end(6, "aborted")]
        self.assertEqual(dsh.parse(self.profile(), aborted, 0.0)["state"], "idle")
        self.assertIsNone(dsh.parse(self.profile(), base[1:], 0.0))

    def test_text_hidden(self):
        run = self.observe(text=False)
        self.assertEqual(run["task"], TEXT_HIDDEN)
        self.assertEqual(run["activity"], "Turn ended · session may resume")
        self.assertEqual(run["name"], "DeepSeek 11111111")
        dumped = json.dumps(run)
        for value in (*SECRETS, "Fix the flaky", "The test is fixed", "Login test"):
            self.assertNotIn(value, dumped)

    def test_subagent_links_to_parent_and_keeps_its_prompt(self):
        self.write(PARENT)
        rows = [
            header(CHILD, parent=PARENT),
            user(1, "Audit the login module", "parent"),
            event(2, "turn/start"),
            user(3, "Also check logout", "parent"),
        ]
        self.write(CHILD, rows)
        runs = {run["native_id"]: run for run in scan_profile(self.profile())}
        child = runs[CHILD]
        self.assertEqual(child["parent_run_id"], runs[PARENT]["run_id"])
        self.assertEqual(child["name"], "DeepSeek subagent 22222222")
        self.assertEqual(child["title_source"], "placeholder")
        self.assertNotIn("title_source", runs[PARENT])
        self.assertEqual(child["task"], "Audit the login module")
        self.assertEqual(child["state"], "working")
        self.assertEqual(dsh.HARNESS.owner(CHILD), CHILD)

    def test_projection_cache_title_wins(self):
        cache = self.root / dsh.PROJCACHE
        cache.mkdir(parents=True)
        title = {"ver": 1, "seq": 9, "val": "Cached"}
        record = {"version": 1, "record": {"rows": {"title": title}}}
        (cache / f"{PARENT}.json").write_text(json.dumps(record))
        (cache / f"{OTHER}.json").write_text("{not json")
        self.assertEqual(dsh.titles(self.root), {PARENT: "Cached"})
        self.assertEqual(self.observe()["name"], "Cached")
        self.assertNotIn("Cached", json.dumps(self.observe(text=False)))

    def test_titled_subagent_is_not_a_placeholder(self):
        rows = [header(CHILD, parent=PARENT), user(1, "Audit the login module", "parent")]
        titled = [*rows, event(2, "session/title", {"title": "Login audit", "source": {}})]
        run = self.observe(CHILD, titled)
        self.assertEqual(run["name"], "Login audit")
        self.assertNotIn("title_source", run)
        cache = self.root / dsh.PROJCACHE
        cache.mkdir(parents=True)
        record = {"record": {"rows": {"title": {"ver": 1, "seq": 1, "val": "Cached audit"}}}}
        (cache / f"{CHILD}.json").write_text(json.dumps(record))
        run = self.observe(CHILD, rows)
        self.assertEqual(run["name"], "Cached audit")
        self.assertNotIn("title_source", run)

    def test_archived_sessions_only_left_out_of_live_storage(self):
        self.write(PARENT)
        self.write(OTHER)
        workspace = {"global": {"archivedSessionIds": [OTHER]}, "tables": {"workspaces": {}}}
        (self.root / dsh.WORKSPACES).parent.mkdir(parents=True)
        (self.root / dsh.WORKSPACES).write_text(json.dumps(workspace))

        def names(archived):
            return {s.path.parent.name for s in dsh.inventory(self.root, 10, None, archived)[0]}

        self.assertEqual(names(True), {PARENT, OTHER})
        self.assertEqual(names(False), {PARENT})

    def test_links_and_missing_logs_are_skipped(self):
        target = self.write(PARENT)
        link = self.root / "sessions" / "--invented-alpha--" / OTHER
        link.mkdir()
        (link / dsh.LOG).symlink_to(target)
        (self.root / "sessions" / "--invented-alpha--" / CHILD).mkdir()
        sessions, coverage = dsh.inventory(self.root, 10)
        self.assertEqual([s.path.parent.name for s in sessions], [PARENT])
        self.assertTrue(coverage["discovery_limited"])
        self.assertEqual(coverage["decoder"], "plain")
        with self.assertRaises(OSError):
            dsh.inventory(self.root / "missing", 10)

    def test_torn_last_line_is_ignored(self):
        rows = conversation()[:8]
        self.write(PARENT, raw=lines(rows, b'{"type":"turn/end","seq":9,"da'))
        [run] = scan_profile(self.profile())
        self.assertEqual((run["state"], run["activity"]), ("working", "The test is fixed."))

    def test_latest_request_beyond_the_bounded_window(self):
        filler = "x" * 2000
        rows = [header(PARENT), user(1, "Earlier request"), user(2, "Deep request")]
        rows += [event(3 + i, "system/message", {"text": filler}) for i in range(600)]
        rows += [user(700, "", "dsh-session-title-llm"), event(701, "turn/start")]
        run = self.observe(rows=rows)
        self.assertEqual(run["task"], "Deep request")
        self.assertEqual(run["state"], "working")

    def test_decoded_size_is_bounded(self):
        chunks = []
        with (
            patch.object(dsh, "REQUEST_SEARCH_BYTES", 10),
            patch.object(dsh, "decoder", return_value=("big", lambda s: iter([b"a" * 8] * 5))),
        ):
            path = self.write(PARENT)
            self.assertTrue(dsh._decode(path, chunks.append))
        self.assertEqual(b"".join(chunks), b"a" * 10)

    def test_without_a_decoder_the_session_is_listed_without_text(self):
        self.write(PARENT)
        self.decoder.return_value = None
        diagnostics = {}
        [run] = scan_profile(self.profile(), diagnostics=diagnostics)
        self.assertEqual(run["native_id"], PARENT)
        self.assertEqual(run["activity"], dsh.NO_DECODER)
        self.assertEqual(run["task"], TEXT_HIDDEN)
        self.assertEqual(run["state"], "unknown")
        self.assertEqual(diagnostics["decoder"], "unavailable")
        self.assertTrue(diagnostics["text_unavailable"])
        session = dsh.inventory(self.root, 10)[0][0]
        self.assertEqual(dsh.latest_request(session), "")
        self.assertEqual(dsh.goal(session).messages, ())

    def test_decoder_prefers_stdlib_then_cli(self):
        with patch.object(dsh, "_STDLIB", plain):
            self.assertEqual(DECODER(), ("compression.zstd", plain))
        with patch.object(dsh, "_STDLIB", None):
            with patch.object(dsh.shutil, "which", return_value=None):
                self.assertIsNone(DECODER())
            with patch.object(dsh.shutil, "which", return_value="/invented/zstd"):
                self.assertEqual(DECODER()[0], "zstd")


class LivenessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / ".dsh"
        self.locks = {}
        for native in (PARENT, CHILD, OTHER):
            session = self.root / "sessions" / "--invented-alpha--" / native
            session.mkdir(parents=True)
            (session / dsh.LOG).write_bytes(b"")
            (session / dsh.LOCK).touch()
            self.locks[native] = session / dsh.LOCK
        which = patch.object(dsh.shutil, "which", return_value="/usr/sbin/lsof")
        which.start()
        self.addCleanup(which.stop)
        flock = patch("fcntl.flock", side_effect=AssertionError("never lock a DSH session"))
        flock.start()
        self.addCleanup(flock.stop)

    def lsof(self, *holders):
        return "".join(f"p{pid}\nc{command}\nf9\nn{self.locks[n]}\n" for pid, command, n in holders)

    def test_live_sessions_from_held_locks(self):
        output = self.lsof((501, "DeepSeek Harness", PARENT), (502, "tail", OTHER))
        profile = SourceProfile("dsh-test", "dsh", "DeepSeek Harness", str(self.root))
        with patch.object(processes, "run", return_value=(0, output)):
            with patch.object(processes, "process", return_value=None):
                self.assertEqual(focus.live_sessions(profile), frozenset({PARENT}))
        with patch.object(processes, "run", return_value=(1, "")):
            self.assertEqual(focus.live_sessions(profile), frozenset())
        with patch.object(processes, "run", return_value=None):
            self.assertIsNone(focus.live_sessions(profile))

    def test_node_runtime_inside_the_app_counts(self):
        app = "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness"
        node = Process(503, 1, "??", START, app)
        with (
            patch.object(processes, "run", return_value=(0, self.lsof((503, "node", CHILD)))),
            patch.object(processes, "process", return_value=node),
        ):
            self.assertEqual(dsh.live_sessions(self.root), frozenset({CHILD}))

    def test_session_pid(self):
        cases = [
            ((0, self.lsof((501, "DeepSeek Harness", PARENT))), (True, 501)),
            ((1, ""), (True, None)),
            ((0, self.lsof((502, "vim", PARENT))), (False, None)),
            (None, (False, None)),
        ]
        for result, expected in cases:
            with (
                self.subTest(expected),
                patch.object(processes, "run", return_value=result),
                patch.object(processes, "process", return_value=None),
            ):
                self.assertEqual(dsh.session_pid(self.root, PARENT), expected)
        self.assertEqual(dsh.session_pid(self.root, "../etc"), (False, None))
        missing = "session-44444444-2222-4333-8444-555555555555"
        self.assertEqual(dsh.session_pid(self.root, missing), (False, None))

    def test_focus_without_a_live_process_offers_nothing(self):
        profile = SourceProfile("dsh-test", "dsh", "DeepSeek Harness", str(self.root))
        run = {
            "mode": "observation",
            "source": "dsh",
            "native_id": PARENT,
            "session_id": "dsh-test:observed",
        }
        with patch.object(processes, "run", return_value=(1, "")):
            self.assertEqual(focus.resolve(run, [profile]).kind, "unsupported")
        with (
            patch.object(processes, "run", return_value=(0, self.lsof((501, "DeepSeek", PARENT)))),
            patch.object(focus, "adapter", return_value=focus.Target("focus", "DeepSeek")) as hit,
        ):
            self.assertEqual(focus.resolve(run, [profile]).kind, "focus")
        hit.assert_called_once()
        self.assertEqual(hit.call_args.args[0], 501)


@unittest.skipUnless(zstd_compress(b"x\n"), "no zstd encoder in this runtime")
class CompressedTests(unittest.TestCase):
    def setUp(self):
        self.data = lines(conversation())
        self.frame = zstd_compress(self.data)
        self.decoders = []
        if dsh._STDLIB:
            self.decoders.append(("stdlib", dsh._STDLIB))
        if shutil.which("zstd"):
            binary = shutil.which("zstd")
            self.decoders.append(("cli", lambda stream: dsh._cli_chunks(binary, stream)))

    def decode(self, decoder, raw):
        with tempfile.NamedTemporaryFile() as file:
            file.write(raw)
            file.flush()
            with open(file.name, "rb") as stream:
                return b"".join(decoder(stream))

    def test_round_trip_multi_frame_and_torn_frame(self):
        if not self.decoders:
            self.skipTest("no zstd decoder in this runtime")
        for name, decoder in self.decoders:
            with self.subTest(name):
                self.assertEqual(self.decode(decoder, self.frame), self.data)
                self.assertEqual(self.decode(decoder, self.frame * 2), self.data * 2)
                torn = self.decode(decoder, self.frame + self.frame[: len(self.frame) // 2])
                self.assertTrue(torn.startswith(self.data))
                self.assertEqual(self.decode(decoder, b"not zstd at all"), b"")

    def test_observe_a_real_compressed_session(self):
        if not dsh.decoder():
            self.skipTest("no zstd decoder in this runtime")
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "sessions" / "--invented-alpha--" / PARENT
            session.mkdir(parents=True)
            (session / dsh.LOG).write_bytes(self.frame + self.frame[:-7])
            profile = SourceProfile("dsh-z", "dsh", "DeepSeek Harness", folder)
            [run] = scan_profile(profile)
        self.assertEqual((run["state"], run["name"]), ("idle", "Login test"))


class CliDecoderTests(unittest.TestCase):
    def test_cli_failure_and_timeout_yield_nothing(self):
        stream = io.BytesIO(b"")
        with patch.object(dsh.subprocess, "Popen", side_effect=OSError):
            self.assertEqual(list(dsh._cli_chunks("/invented/zstd", stream)), [])
        if not os.path.exists("/bin/sleep"):
            self.skipTest("no sleep binary")
        with (
            tempfile.TemporaryFile() as file,
            patch.object(dsh, "DECODE_SECONDS", 0.2),
            patch.object(dsh.subprocess, "Popen") as popen,
        ):
            popen.side_effect = lambda argv, **kw: REAL_POPEN(["/bin/sleep", "5"], **kw)
            self.assertEqual(list(dsh._cli_chunks("/invented/zstd", file)), [])


if __name__ == "__main__":
    unittest.main()
