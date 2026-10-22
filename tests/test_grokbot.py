import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritboard import focus
from spiritboard.harnesses import HARNESSES, grokbot
from spiritboard.sources import SourceProfile, discover_profiles, scan_profile
from spiritboard.transcripts import TEXT_HIDDEN

ACCOUNT = "auth0%invented-account-7f3e"
ALPHA = "11111111-2222-4333-8444-555555555555"
BETA = "22222222-2222-4333-8444-555555555555"
HIDDEN = "33333333-2222-4333-8444-555555555555"
NOW = int(time.time() * 1000)
HOUR = 3_600_000


def key(kind: str, native: str = "") -> str:
    base = f"sand.client.slice.account.{ACCOUNT}."
    return base + ("roster.last-roster" if kind == "roster" else f"transcript.replicas.{native}")


def agent(native, name, *, at=NOW, hidden=False):
    return {
        "id": native,
        "name": name,
        "description": "An invented helper",
        "title": "",
        "path": f"/box/{native}/workspace",
        "createdAt": at - 24 * HOUR,
        "updatedAt": at,
        "lastActivityAt": at,
        "lastEntry": {"kind": "text", "text": "invented preview"},
        "isHiddenFromSidebar": hidden,
        "harness": "temporal",
    }


def user(seq, text, at, streaming=False):
    return {
        "id": f"m{seq}",
        "kind": "message",
        "role": "user",
        "content": text,
        "richText": text,
        "isStreaming": streaming,
        "clientNonce": f"n{seq}",
        "seq": seq,
        "timestampMs": at,
    }


def reply(seq, text, at, **extra):
    message = {"type": "text", "content": text}
    return {"id": f"s{seq}", "kind": "send-message", "message": message, "seq": seq} | {
        "timestampMs": at,
        **extra,
    }


def widget(seq, prompt, at, **extra):
    message = {"type": "widget", "widget": {"prompt": prompt, "options": ["Yes", "No"]}}
    return {"id": f"w{seq}", "kind": "send-message", "message": message, "seq": seq} | {
        "timestampMs": at,
        **extra,
    }


def event(seq, at):
    return {"id": f"e{seq}", "kind": "event", "event": {"type": "name-changed"}, "seq": seq} | {
        "timestampMs": at
    }


class GrokBotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.root = self.home / "Library" / "Application Support" / "Grok Bot"
        self.cache = self.root / grokbot.CACHE
        self.cache.mkdir(parents=True)
        grokbot._VERSIONS.clear()

    def blob(self, name, value=None, version=1, raw=None):
        path = self.cache / grokbot.blob_name(name)
        data = raw if raw is not None else json.dumps({"schemaVersion": version, "value": value})
        path.write_text(data)
        return path

    def roster(self, *rows, version=4):
        return self.blob(key("roster"), {"rows": list(rows)}, version)

    def replica(self, native, *entries, version=1):
        value = {"entries": list(entries), "epochHint": None, "persistedAt": NOW}
        return self.blob(key("replica", native), value, version)

    def profile(self, text=True):
        return SourceProfile("grok-test", "grokbot", "Grok Bot", str(self.root), text)

    def scan(self, text=True):
        diagnostics = {}
        rows = scan_profile(self.profile(text), diagnostics=diagnostics)
        return {row["native_id"]: row for row in rows}, diagnostics

    def test_blob_names_round_trip_base32_keys(self):
        name = grokbot.blob_name(key("roster"))
        self.assertRegex(name, r"^[a-z2-7]+\.blob$")
        self.assertEqual(grokbot.blob_key(name), key("roster"))
        self.assertEqual(grokbot.blob_key("not base32!.blob"), "")
        self.assertEqual(grokbot.blob_key(name.removesuffix(".blob")), "")

    def test_discovery_on_macos_and_environment_override(self):
        with patch.object(sys, "platform", "darwin"):
            found = [p for p in discover_profiles(self.home) if p.kind == "grokbot"]
        self.assertEqual(
            [(p.label, Path(p.root)) for p in found], [("Grok Bot", self.root.resolve())]
        )
        other = self.home / "elsewhere"
        (other / grokbot.CACHE).mkdir(parents=True)
        with (
            patch.object(Path, "home", return_value=self.home / "empty"),
            patch.dict(os.environ, {"GROK_BOT_HOME": str(other)}),
        ):
            configured = [p for p in discover_profiles() if p.kind == "grokbot"]
        self.assertEqual([p.label for p in configured], ["Grok Bot · configured"])
        self.assertFalse(grokbot.is_root(self.home))

    def test_non_macos_discovers_nothing(self):
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(grokbot.home_roots(self.home), [])
            self.assertFalse([p for p in discover_profiles(self.home) if p.kind == "grokbot"])

    def test_inventory_lists_roster_agents_newest_first_within_bounds(self):
        self.roster(
            agent(ALPHA, "Alpha", at=NOW - HOUR),
            agent(BETA, "Beta", at=NOW),
            agent(HIDDEN, "Hidden", at=NOW, hidden=True),
            agent("44444444-2222-4333-8444-555555555555", "Old", at=NOW - 30 * 24 * HOUR),
        )
        self.replica(ALPHA, user(1, "Plan the trip", NOW - HOUR))
        sessions, coverage = grokbot.inventory(self.root, 10, 7, False)
        self.assertEqual([s.key for s in sessions], [BETA, ALPHA])
        self.assertEqual(coverage["archived_skipped"], 1)
        self.assertEqual(coverage["aged_out"], 1)
        self.assertEqual(sessions[0].size, 0)
        self.assertGreater(sessions[1].size, 0)
        everything, _ = grokbot.inventory(self.root, 10, None, True)
        self.assertEqual(len(everything), 4)
        limited, coverage = grokbot.inventory(self.root, 1, None, True)
        self.assertEqual(len(limited), 1)
        self.assertTrue(coverage["truncated"])
        with self.assertRaises(OSError):
            grokbot.inventory(self.home / "missing", 10, None, True)

    def test_observation_maps_request_reply_and_your_turn(self):
        self.roster(agent(ALPHA, "Travel planner", at=NOW - 60_000))
        self.replica(
            ALPHA,
            user(1, "Old request", NOW - 3 * HOUR),
            reply(2, "Old answer", NOW - 3 * HOUR + 1000),
            user(3, "Find a hotel in Lisbon", NOW - 120_000),
            reply(4, "Looking now", NOW - 100_000, requestId="r1"),
            reply(5, "Booked the Alfama hotel", NOW - 60_000, requestId="r1"),
            event(6, NOW - 60_000),
        )
        runs, coverage = self.scan()
        run = runs[ALPHA]
        self.assertEqual(run["name"], "Travel planner")
        self.assertEqual(run["task"], "Find a hotel in Lisbon")
        self.assertEqual(run["activity"], "Booked the Alfama hotel")
        self.assertEqual(run["state"], "idle")
        self.assertEqual(run["source"], "grokbot")
        self.assertEqual(run["project"], "")
        self.assertIsNone(run["parent_run_id"])
        self.assertEqual(run["run_id"], f"grok-test:{ALPHA}")
        self.assertTrue(run["last_seen"].startswith(time.strftime("%Y", time.gmtime())))
        self.assertLess(run["started_at"], run["last_seen"])
        self.assertEqual(coverage["observations"], 1)
        self.assertFalse(coverage["unsupported_schema"])

    def test_recent_request_or_streaming_is_working_and_old_request_is_idle(self):
        self.roster(agent(ALPHA, "A"), agent(BETA, "B"), agent(HIDDEN, "C"))
        self.replica(ALPHA, reply(1, "Hello", NOW - HOUR), user(2, "Summarize my inbox", NOW))
        self.replica(BETA, user(1, "Draft a note", NOW - 2 * HOUR, streaming=True))
        self.replica(HIDDEN, user(1, "Anyone there?", NOW - 2 * HOUR))
        runs, _ = self.scan()
        self.assertEqual(runs[ALPHA]["state"], "working")
        self.assertEqual(runs[ALPHA]["task"], "Summarize my inbox")
        self.assertEqual(runs[BETA]["state"], "working")
        self.assertEqual(runs[HIDDEN]["state"], "idle")
        self.assertEqual(runs[HIDDEN]["activity"], "No reply recorded")

    def test_unanswered_question_needs_the_user(self):
        self.roster(agent(ALPHA, "A"), agent(BETA, "B"))
        self.replica(ALPHA, user(1, "Pick a venue", NOW - 5000), widget(2, "Which city?", NOW))
        self.replica(
            BETA,
            user(1, "Pick a venue", NOW - 5000),
            widget(2, "Which city?", NOW - 4000, respondedValue="Porto"),
            reply(3, "Porto it is", NOW),
        )
        runs, _ = self.scan()
        self.assertEqual(runs[ALPHA]["state"], "waiting_user")
        self.assertEqual(runs[ALPHA]["activity"], "Which city?")
        self.assertEqual(runs[BETA]["state"], "idle")
        hidden, _ = self.scan(text=False)
        self.assertEqual(hidden[ALPHA]["activity"], "Question asked")
        self.replica(ALPHA, widget(1, "Which city?", NOW - 48 * HOUR))
        runs, _ = self.scan()
        self.assertEqual(runs[ALPHA]["state"], "idle")

    def test_text_hidden_profile_keeps_generic_labels(self):
        self.roster(agent(ALPHA, "Private name"))
        self.replica(ALPHA, user(1, "Private request", NOW - 5000), reply(2, "Private reply", NOW))
        runs, _ = self.scan(text=False)
        run = runs[ALPHA]
        self.assertEqual(run["task"], TEXT_HIDDEN)
        self.assertEqual(run["activity"], "Reply recorded")
        self.assertEqual(run["name"], f"Grok Bot {ALPHA[:8]}")
        self.assertNotIn("Private", json.dumps(run))

    def test_hidden_agent_is_archived_and_agent_without_transcript_is_listed(self):
        self.roster(agent(ALPHA, "Gone", hidden=True), agent(BETA, "Fresh"))
        self.replica(ALPHA, reply(1, "Done", NOW))
        runs, _ = self.scan()
        self.assertTrue(runs[ALPHA]["archived"])
        self.assertEqual(runs[BETA]["state"], "unknown")
        self.assertEqual(runs[BETA]["activity"], "No transcript cached locally")
        self.assertNotIn("archived", runs[BETA])

    def test_unexpected_schema_versions_are_skipped_with_a_warning(self):
        self.roster(agent(ALPHA, "A"), agent(BETA, "B"))
        self.replica(ALPHA, reply(1, "Fine", NOW))
        self.replica(BETA, {"kind": "mystery"}, version=2)
        runs, coverage = self.scan()
        self.assertEqual(set(runs), {ALPHA})
        self.assertTrue(coverage["unsupported_schema"])
        self.assertEqual(coverage["unsupported_blobs"], 1)
        self.roster(agent(ALPHA, "A"), version=5)
        runs, coverage = self.scan()
        self.assertEqual(runs, {})
        self.assertTrue(coverage["unsupported_schema"])
        self.blob(key("roster"), ["not", "an", "object"], version=4)
        self.assertEqual(grokbot.inventory(self.root, 10, None, True)[0], [])

    def test_malformed_and_mid_write_blobs_are_tolerated(self):
        self.roster(agent(ALPHA, "A"), agent(BETA, "B"))
        self.replica(ALPHA, reply(1, "Fine", NOW))
        self.blob(key("replica", BETA), raw='{"schemaVersion": 1, "value": {"entries": [')
        (self.cache / "zz!!.blob").write_text("{}")
        (self.cache / grokbot.blob_name("sand.client.slice.ui-layout")).write_text("{")
        runs, coverage = self.scan()
        self.assertEqual(set(runs), {ALPHA})
        self.assertEqual(coverage["read_errors"], 0)
        self.assertEqual(coverage["unsupported_files"], 1)
        self.blob(key("roster"), raw="{")
        sessions, coverage = grokbot.inventory(self.root, 10, None, True)
        self.assertEqual((sessions, coverage["unreadable_blobs"]), ([], 1))

    def test_account_id_is_never_exposed(self):
        self.roster(agent(ALPHA, "A"))
        self.replica(ALPHA, user(1, "Hi", NOW - 5000), reply(2, "Hello", NOW))
        runs, coverage = self.scan()
        sessions, _ = grokbot.inventory(self.root, 10, None, True)
        exposed = json.dumps([runs, coverage, [s.key for s in sessions]])
        self.assertNotIn("invented-account", exposed)
        self.assertNotIn("auth0", exposed)

    def test_liveness_is_unknown_and_the_agent_opens_in_the_app_but_never_resumes(self):
        self.roster(agent(ALPHA, "A"))
        self.replica(ALPHA, reply(1, "Hello", NOW))
        harness = HARNESSES["grokbot"]
        self.assertIsNone(harness.live_sessions(self.root))
        self.assertEqual(harness.session_pid(self.root, ALPHA), (False, None))
        self.assertIsNone(harness.resume(self.root, ALPHA))
        run = {
            "mode": "observation",
            "source": "grokbot",
            "native_id": ALPHA,
            "session_id": "grok-test:observed",
        }
        with patch.object(sys, "platform", "darwin"):
            target = focus.resolve(run, [self.profile()])
        self.assertEqual((target.kind, target.label), ("focus", "Grok Bot"))
        link = f"grokbot://app/v1/agent?id={ALPHA}"
        self.assertEqual([step.argv for step in target.steps], [("/usr/bin/open", link)])
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(focus.resolve(run, [self.profile()]).kind, "unsupported")

    def test_agent_link_rejects_ids_the_app_would_refuse(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertIsNone(grokbot.open_app("a/b", "", []))
            self.assertIsNone(grokbot.open_app("x" * 129, "", []))
            self.assertIsNone(grokbot.open_app("id?x=1", "", []))

    def test_goal_and_latest_request(self):
        self.roster(agent(ALPHA, "Planner"))
        self.replica(
            ALPHA,
            user(1, "Plan a dinner", NOW - 5000),
            widget(2, "Which day?", NOW - 4000),
            reply(3, "Friday works", NOW),
        )
        session = grokbot.inventory(self.root, 10, None, True)[0][0]
        goal = grokbot.goal(session)
        self.assertEqual(goal.title, "Planner")
        self.assertEqual(
            [(role, content) for role, content, _ in goal.messages],
            [("user", "Plan a dinner"), ("assistant", "Friday works")],
        )
        self.assertEqual(grokbot.latest_request(session), "Plan a dinner")


if __name__ == "__main__":
    unittest.main()
