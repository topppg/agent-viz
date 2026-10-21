import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus
from spiritboard.harnesses import HARNESSES, opencode
from spiritboard.harnesses.harness import SESSION_ID
from spiritboard.sources import SourceProfile, discover_profiles, scan_profile
from spiritboard.transcripts import TEXT_HIDDEN

NOW = int(time.time() * 1000)
SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, vcs TEXT, name TEXT);
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT,
    title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER,
    time_archived INTEGER, time_compacting INTEGER, workspace_id TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE account (id TEXT PRIMARY KEY, access_token TEXT);
CREATE TABLE control_account (id TEXT PRIMARY KEY, token TEXT);
INSERT INTO account VALUES ('acc_invented', 'invented-token');
INSERT INTO control_account VALUES ('ctl_invented', 'invented-token');
"""


class Fixture:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.counter = 0

    def session(self, sid, directory="/invented/alpha", title="", parent=None, at=0, archived=None):
        self.conn.execute(
            "INSERT INTO session (id, parent_id, directory, title, time_created, time_updated, "
            "time_archived) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, parent, directory, title, NOW - 60_000 + at, NOW - 60_000 + at, archived),
        )

    def message(self, sid, role, at, parts=(), completed=True):
        self.counter += 1
        mid = f"msg_{self.counter:04d}"
        stamp = NOW - 60_000 + at
        data = {"role": role, "agent": "build", "time": {"created": stamp}}
        if role == "assistant" and completed:
            data["time"]["completed"] = stamp + 1
            data["finish"] = "stop"
        self.conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, sid, stamp, stamp, json.dumps(data))
        )
        for index, part in enumerate(parts):
            self.conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (f"prt_{mid}_{index}", mid, sid, stamp + index, stamp + index, json.dumps(part)),
            )
        return mid

    def commit(self):
        self.conn.commit()


def text_part(value, **extra):
    return {"type": "text", "text": value, **extra}


def tool_part(tool, status):
    return {
        "type": "tool",
        "tool": tool,
        "callID": "call_invented",
        "state": {
            "status": status,
            "title": "invented tool title",
            "input": {"command": "invented-input"},
            "output": "invented-output",
        },
    }


class OpencodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "opencode"
        self.root.mkdir()
        self.db = Fixture(self.root / "opencode.db")
        db = self.db
        db.session("ses_alpha00001", title="Invented alpha task", at=0)
        db.message("ses_alpha00001", "user", 1, [text_part("first invented request")])
        db.message("ses_alpha00001", "assistant", 2, [text_part("first invented reply")])
        db.message(
            "ses_alpha00001",
            "user",
            3,
            [text_part("<file>synthetic</file>", synthetic=True), text_part("latest invented ask")],
        )
        db.message(
            "ses_alpha00001",
            "assistant",
            4,
            [
                {"type": "step-start"},
                {"type": "reasoning", "text": "invented hidden reasoning"},
                tool_part("bash", "completed"),
                text_part("latest invented reply"),
                {"type": "step-finish"},
            ],
        )
        db.session("ses_bravo00001", "/invented/bravo", "Invented bravo", at=10)
        db.message("ses_bravo00001", "user", 11, [text_part("bravo invented ask")])
        db.message("ses_bravo00001", "assistant", 12, [text_part("thinking aloud")], False)
        db.session("ses_child00001", "/invented/bravo", "Child (@general)", "ses_bravo00001", 13)
        db.message("ses_child00001", "user", 14, [text_part("child invented directive")])
        db.message("ses_child00001", "assistant", 15, [tool_part("read", "running")], False)
        db.session("ses_archived01", title="Invented archived", at=20, archived=NOW)
        db.message("ses_archived01", "user", 21, [text_part("archived ask")])
        db.commit()
        self.statements: list[str] = []
        self.uris: list[str] = []
        real = sqlite3.connect

        def traced(*args, **kwargs):
            self.uris.append(args[0])
            conn = real(*args, **kwargs)
            conn.set_trace_callback(self.statements.append)
            return conn

        patcher = patch.object(opencode.sqlite3, "connect", traced)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def profile(self, text=True):
        return SourceProfile("oc", "opencode", "opencode", str(self.root), text)

    def observed(self, text=True):
        sessions, _ = opencode.inventory(self.root, 50)
        return {s.key: opencode.observe(self.profile(text), s) for s in sessions}

    def test_markup_is_not_a_reply(self):
        self.db.session("ses_markup0001", at=40)
        self.db.message("ses_markup0001", "user", 41, [text_part("Draft the page")])
        self.db.message("ses_markup0001", "assistant", 42, [text_part("Writing the page")])
        self.db.message("ses_markup0001", "assistant", 43, [text_part("<!doctype html>")])
        self.db.commit()
        self.assertEqual(self.observed()["ses_markup0001"]["activity"], "Writing the page")

    def test_inventory_excludes_archived_sessions_and_keys_rows_by_session(self):
        sessions, coverage = opencode.inventory(self.root, 50)
        self.assertEqual(
            [s.key for s in sessions], ["ses_child00001", "ses_bravo00001", "ses_alpha00001"]
        )
        self.assertEqual({s.path for s in sessions}, {self.root / "opencode.db"})
        self.assertEqual(coverage["files_seen"], 3)
        self.assertEqual(coverage["archived_skipped"], 1)
        self.assertEqual(coverage["transcript_trees"], ["opencode.db"])
        limited, coverage = opencode.inventory(self.root, 1)
        self.assertEqual(len(limited), 1)
        self.assertTrue(coverage["truncated"])

    def test_signature_changes_when_a_message_or_hot_part_changes(self):
        before = {s.key: (s.size, s.mtime_ns) for s in opencode.inventory(self.root, 50)[0]}
        self.db.conn.execute(
            "UPDATE part SET time_updated = ? WHERE session_id = 'ses_alpha00001'", (NOW + 5,)
        )
        self.db.commit()
        after = {s.key: (s.size, s.mtime_ns) for s in opencode.inventory(self.root, 50)[0]}
        self.assertNotEqual(before["ses_alpha00001"], after["ses_alpha00001"])
        self.assertEqual(before["ses_bravo00001"], after["ses_bravo00001"])

    def test_observation_titles_latest_request_reply_and_states(self):
        runs = self.observed()
        alpha = runs["ses_alpha00001"]
        self.assertEqual(alpha["name"], "Invented alpha task")
        self.assertEqual(alpha["task"], "latest invented ask")
        self.assertEqual(alpha["activity"], "latest invented reply")
        self.assertEqual(alpha["state"], "idle")
        self.assertEqual(alpha["project"], "alpha")
        self.assertEqual(alpha["source"], "opencode")
        bravo = runs["ses_bravo00001"]
        self.assertEqual(bravo["state"], "working")
        self.assertEqual(bravo["activity"], "thinking aloud")
        child = runs["ses_child00001"]
        self.assertEqual(child["parent_run_id"], "oc:ses_bravo00001")
        self.assertEqual((child["state"], child["activity"]), ("working", "Running read"))
        rendered = json.dumps(runs)
        for secret in ("hidden reasoning", "invented-input", "invented-output", "tool title"):
            self.assertNotIn(secret, rendered)

    def test_text_off_hides_titles_requests_replies_and_tool_names(self):
        runs = self.observed(text=False)
        alpha, child = runs["ses_alpha00001"], runs["ses_child00001"]
        self.assertEqual(alpha["name"], "opencode ses_alph")
        self.assertEqual(alpha["task"], TEXT_HIDDEN)
        self.assertEqual(alpha["activity"], "Turn ended · session may resume")
        self.assertEqual(child["activity"], "Tool running")
        rendered = json.dumps(runs)
        for secret in ("Invented", "invented ask", "invented reply", "read"):
            self.assertNotIn(secret, rendered)

    def test_scan_profile_reads_the_database_through_the_registry(self):
        self.assertIs(HARNESSES["opencode"], opencode.HARNESS)
        runs = {r["native_id"]: r for r in scan_profile(self.profile(), 50)}
        self.assertEqual(set(runs), {"ses_alpha00001", "ses_bravo00001", "ses_child00001"})
        self.assertEqual(runs["ses_child00001"]["parent_run_id"], "oc:ses_bravo00001")

    def test_goal_has_title_and_text_messages_only(self):
        session = next(
            s for s in opencode.inventory(self.root, 50)[0] if s.key.endswith("alpha00001")
        )
        goal = opencode.goal(session)
        self.assertEqual(goal.title, "Invented alpha task")
        self.assertEqual(
            [(role, body) for role, body, _ in goal.messages],
            [
                ("user", "first invented request"),
                ("assistant", "first invented reply"),
                ("user", "latest invented ask"),
                ("assistant", "latest invented reply"),
            ],
        )
        self.assertEqual(opencode.latest_request(session), "latest invented ask")

    def test_v2_only_database_is_skipped_as_a_diagnostic(self):
        other = Path(self.temp.name) / "v2"
        other.mkdir()
        conn = sqlite3.connect(other / "opencode.db")
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, data TEXT)")
        conn.commit()
        conn.close()
        sessions, coverage = opencode.inventory(other, 50)
        self.assertEqual(sessions, [])
        self.assertTrue(coverage["unsupported_schema"])
        self.assertIsNone(opencode.resume(other, "ses_alpha00001"))

    def test_credential_tables_are_never_queried_and_the_database_opens_read_only(self):
        self.observed()
        session = opencode.inventory(self.root, 50)[0][0]
        opencode.goal(session)
        opencode.latest_request(session)
        opencode.resume(self.root, "ses_alpha00001")
        with patch.object(opencode, "_processes", return_value={"/invented/alpha": [42]}):
            opencode.live_sessions(self.root)
            opencode.session_pid(self.root, "ses_alpha00001")
        self.assertTrue(self.statements)
        for statement in self.statements:
            self.assertNotRegex(statement.lower(), r"\b(control_)?account(_state)?\b")
        self.assertTrue(
            all(uri.startswith("file:") and uri.endswith("?mode=ro") for uri in self.uris)
        )
        with self.assertRaises(OSError), opencode.connect(self.root) as conn:
            conn.execute("DELETE FROM session")
        with self.assertRaises(OSError), opencode.connect(self.root) as conn:
            conn.execute("SELECT * FROM account")

    def test_liveness_counts_only_the_newest_top_level_session_per_live_directory(self):
        self.db.session("ses_bravo00000", "/invented/bravo", "Older bravo", at=5)
        self.db.commit()
        dirs = {"/invented/bravo": [4242]}
        with patch.object(opencode, "_processes", return_value=dirs):
            self.assertEqual(
                opencode.live_sessions(self.root),
                frozenset({"ses_bravo00001", "ses_child00001"}),
            )
            self.assertEqual(opencode.session_pid(self.root, "ses_bravo00001"), (True, 4242))
            self.assertEqual(opencode.session_pid(self.root, "ses_child00001"), (True, 4242))
            # An older session in a live directory might still be open elsewhere.
            self.assertEqual(opencode.session_pid(self.root, "ses_bravo00000"), (False, None))
            self.assertEqual(opencode.session_pid(self.root, "ses_alpha00001"), (True, None))
        with patch.object(opencode, "_processes", return_value=None):
            self.assertIsNone(opencode.live_sessions(self.root))
            self.assertEqual(opencode.session_pid(self.root, "ses_alpha00001"), (False, None))
        with patch.object(opencode, "_processes", return_value={}):
            self.assertEqual(opencode.live_sessions(self.root), frozenset())

    def test_process_lookup_groups_top_level_opencode_processes_by_cwd(self):
        ps = (
            "  100     1 /usr/local/bin/opencode\n"
            "  101   100 /usr/local/lib/opencode-darwin-arm64/bin/opencode\n"
            "  200     1 /bin/zsh\n"
        )
        lsof = "p100\nfcwd\nn/invented/bravo\n"
        calls = []

        def run(argv, env=None):
            calls.append(argv)
            return (0, ps) if argv[0] == "ps" else (0, lsof)

        with (
            patch.object(opencode.shutil, "which", return_value="/usr/sbin/lsof"),
            patch.object(opencode.processes, "run", side_effect=run),
            patch.object(opencode.os.path, "realpath", side_effect=lambda p: p),
        ):
            self.assertEqual(opencode._processes(), {"/invented/bravo": [100]})
        self.assertEqual(calls[1][-1], "100")
        with patch.object(opencode.shutil, "which", return_value=None):
            self.assertIsNone(opencode._processes())

    def test_resume_command_and_focus_target_for_a_closed_session(self):
        self.assertTrue(SESSION_ID.fullmatch("ses_alpha00001"))
        command = opencode.resume(self.root, "ses_alpha00001")
        db = self.root / "opencode.db"
        self.assertEqual(
            command, f"cd /invented/alpha && OPENCODE_DB={db} opencode -s ses_alpha00001"
        )
        run = {
            "mode": "observation",
            "source": "opencode",
            "native_id": "ses_alpha00001",
            "session_id": "oc:observed",
        }
        with patch.object(opencode, "_processes", return_value={}):
            target = focus.resolve(run, [self.profile()])
        self.assertEqual((target.kind, target.command), ("resume", command))

    def test_discovery_finds_default_and_channel_databases(self):
        home = Path(self.temp.name) / "home"
        data = home / ".local" / "share" / "opencode"
        data.mkdir(parents=True)
        for name in ("opencode.db", "opencode-beta.db", "opencode.db-wal"):
            (data / name).write_bytes(b"")
        labels = {p.label: p for p in discover_profiles(home) if p.kind == "opencode"}
        self.assertEqual(set(labels), {"opencode", "opencode · beta"})
        self.assertEqual(labels["opencode"].root, str((data / "opencode.db").resolve()))
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/invented/xdg"}):
            self.assertEqual(opencode.home_roots(home)[0][1], data / "opencode.db")


if __name__ == "__main__":
    unittest.main()
