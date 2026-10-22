import copy
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from spiritboard.model import now
from spiritboard.store import Store, keep_pruned


def event(
    sequence=1,
    kind="run.started",
    payload=None,
    run="run-a",
    session="session-a",
    producer="producer-a",
    parent=None,
):
    value = dict(
        schema_version=1,
        event_id=f"{producer}:{sequence}",
        session_id=session,
        run_id=run,
        producer_id=producer,
        sequence=sequence,
        occurred_at=now(),
        type=kind,
        payload={"name": "Same display name"} if payload is None else payload,
    )
    if parent:
        value["parent_run_id"] = parent
    return value


def observation(run="observed-a", account="account-a", source="codex", **changes):
    result = dict(
        run_id=run,
        session_id=run,
        name="Native session",
        source=source,
        account=account,
        state="working",
        last_seen=now(),
        activity="Tool activity observed",
        evidence="Read-only synthetic observation",
        native_id="private-native-id",
    )
    result.update(changes)
    return result


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_exact_duplicate_is_idempotent_and_conflict_rolls_back(self):
        value = event()
        first = self.store.ingest(value)
        duplicate = self.store.ingest(value)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["cursor"], duplicate["cursor"])
        changed = copy.deepcopy(value)
        changed["payload"]["name"] = "Conflict"
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.store.ingest(changed)
        self.assertEqual(self.store.snapshot()["cursor"], 1)
        self.assertEqual(self.store.snapshot()["runs"][0]["name"], "Same display name")

    def test_producer_sequence_collision_is_rejected(self):
        self.store.ingest(event())
        changed = event()
        changed["event_id"] = "different-id"
        with self.assertRaisesRegex(ValueError, "sequence already used"):
            self.store.ingest(changed)
        self.store.ingest(event(2, "run.heartbeat", {}))
        self.assertEqual(self.store.snapshot()["cursor"], 2)

    def test_terminal_run_never_revives_and_child_remains_independent(self):
        self.store.ingest(event())
        self.store.ingest(event(2, run="child", parent="run-a"))
        self.store.ingest(event(3, "run.finished", {"state": "completed", "summary": "Done"}))
        self.store.ingest(event(4, "run.state_changed", {"state": "working"}))
        self.store.ingest(event(5, "run.activity", {"text": "Must not replace terminal state"}))
        rows = {row["run_id"]: row for row in self.store.snapshot()["runs"]}
        self.assertEqual(rows["run-a"]["state"], "completed")
        self.assertEqual(rows["run-a"]["summary"], "Done")
        self.assertNotEqual(rows["run-a"]["activity"], "Must not replace terminal state")
        self.assertEqual(rows["child"]["state"], "working")

    def test_delayed_event_is_journaled_without_overwriting_new_state(self):
        self.store.ingest(event())
        self.store.ingest(event(4, "run.activity", {"text": "Newest"}))
        delayed = self.store.ingest(event(2, "run.activity", {"text": "Delayed"}))
        self.assertFalse(delayed["applied"])
        row = self.store.snapshot()["runs"][0]
        self.assertEqual(row["activity"], "Newest")
        self.assertIn("producer sequence gap", row["warnings"])
        self.assertIn("delayed event ignored for current state", row["warnings"])
        self.assertEqual(len(self.store.events("run-a")["events"]), 3)

    def test_child_can_arrive_before_parent_but_cycles_are_rejected(self):
        self.store.ingest(event(run="child", parent="parent"))
        with self.assertRaisesRegex(ValueError, "cyclic"):
            self.store.ingest(event(2, run="parent", parent="child"))
        self.store.ingest(event(2, run="parent"))
        rows = {row["run_id"]: row for row in self.store.snapshot()["runs"]}
        self.assertEqual(rows["child"]["parent_run_id"], "parent")

    def test_cross_session_parent_rejected_in_both_arrival_orders(self):
        self.store.ingest(event(run="parent"))
        with self.assertRaisesRegex(ValueError, "cross-session"):
            self.store.ingest(event(2, run="child", parent="parent", session="other"))
        self.store.ingest(event(2, run="early-child", parent="late-parent"))
        with self.assertRaisesRegex(ValueError, "cross-session"):
            self.store.ingest(event(3, run="late-parent", session="other"))

    def test_same_display_names_and_session_ids_do_not_merge_runs(self):
        self.store.ingest(event())
        self.store.ingest(event(2, run="run-b"))
        self.assertEqual(self.store.snapshot()["total_runs"], 2)

    def test_identity_cannot_change(self):
        self.store.ingest(event())
        for changed in [
            event(2, "run.heartbeat", {}, session="other"),
            event(2, "run.heartbeat", {}, parent="new-parent"),
            event(2, "run.heartbeat", {}, producer="other"),
        ]:
            with self.assertRaisesRegex(ValueError, "identity changed"):
                self.store.ingest(changed)

    def test_stream_identity_persists_but_new_database_has_new_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite"
            first = Store(path)
            identity = first.snapshot()["stream_id"]
            first.close()
            second = Store(path)
            self.assertEqual(second.snapshot()["stream_id"], identity)
            second.close()
            independent = Store()
            self.assertNotEqual(independent.snapshot()["stream_id"], identity)
            independent.close()

    def test_prune_never_orphans_retained_descendants_or_deletes_active_sdk(self):
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        with patch("spiritboard.store.now", return_value=old):
            self.store.ingest(event(run="old-done"))
            self.store.ingest(event(2, "run.finished", {"state": "completed"}, run="old-done"))
            self.store.ingest(event(3, run="old-parent"))
            self.store.ingest(event(4, "run.finished", {"state": "completed"}, run="old-parent"))
            self.store.ingest(event(5, run="still-active"))
        self.store.ingest(event(6, run="child", parent="old-parent"))
        self.store.observe(observation(run="old-observation", last_seen=old))
        result = self.store.prune(7)
        self.assertEqual(result["deleted_runs"], 2)
        rows = {r["run_id"] for r in self.store.snapshot()["runs"]}
        self.assertEqual(rows, {"old-parent", "child", "still-active"})
        self.assertEqual(self.store.events("old-done")["events"], [])

    def test_local_name_survives_observations_and_leaves_with_the_run(self):
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.store.observe(observation(run="named", last_seen=old))
        self.assertEqual(self.store.rename("named", " My\n  name "), "My name")
        self.store.observe(observation(run="named", last_seen=old, name="Native again"))
        run = self.store.snapshot()["runs"][0]
        self.assertEqual((run["name"], run["local_name"]), ("Native again", "My name"))
        self.assertNotIn("local_name", self.store.run("named"))
        with self.assertRaises(KeyError):
            self.store.rename("missing", "x")
        for bad in ("x" * 121, "a\x00b", 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.rename("named", bad)
        self.store.prune(7)
        self.store.observe(observation(run="named"))
        self.assertNotIn("local_name", self.store.snapshot()["runs"][0])

    def test_parallel_operations_keep_independent_outcomes(self):
        self.store.ingest(event())
        self.store.ingest(
            event(2, "operation.started", {"operation_id": "a", "kind": "tool", "label": "Read A"})
        )
        self.store.ingest(
            event(3, "operation.started", {"operation_id": "b", "kind": "tool", "label": "Read B"})
        )
        self.store.ingest(
            event(
                4,
                "operation.finished",
                {"operation_id": "a", "outcome": "error", "detail": "Invented error"},
            )
        )
        operations = self.store.snapshot()["runs"][0]["operations"]
        self.assertEqual(operations[0]["outcome"], "error")
        self.assertFalse(operations[0]["pending"])
        self.assertTrue(operations[1]["pending"])
        with self.assertRaisesRegex(ValueError, "no matching active operation"):
            self.store.ingest(
                event(5, "operation.finished", {"operation_id": "missing", "outcome": "success"})
            )
        self.store.ingest(
            event(5, "operation.finished", {"operation_id": "b", "outcome": "success"})
        )

    def test_observations_preserve_four_profiles_and_strip_private_fields(self):
        for index, source in enumerate(["codex", "codex", "claude", "claude"]):
            value = observation(
                run=f"profile-{index}:same-native-id",
                source=source,
                account=f"account-{index}",
                raw_prompt="Private",
                auth_token="Private",
            )
            self.assertTrue(self.store.observe(value))
            self.assertFalse(self.store.observe(value))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["total_runs"], 4)
        self.assertEqual(snapshot["cursor"], 4)
        for row in snapshot["runs"]:
            self.assertTrue(row["inferred"])
            self.assertEqual(row["mode"], "observation")
            self.assertNotIn("native_id", row)
            self.assertNotIn("raw_prompt", row)
            self.assertNotIn("auth_token", row)

    def test_observation_title_source_is_validated_and_follows_latest(self):
        for source in ("user", "placeholder"):
            run = f"titled-{source}"
            self.assertTrue(self.store.observe(observation(run, title_source=source)))
            self.assertEqual(self.store.run(run)["title_source"], source)
        self.store.observe(observation("titled-user"))
        self.assertNotIn("title_source", self.store.run("titled-user"))
        for invalid in ("ai", "custom", "", None, True):
            with self.assertRaisesRegex(ValueError, "invalid title source"):
                self.store.observe(observation("titled-bad", title_source=invalid))
        self.assertIsNone(self.store.run("titled-bad"))

    def test_observation_cycles_cross_session_and_sdk_collision_rejected(self):
        self.store.ingest(event())
        with self.assertRaisesRegex(ValueError, "collides"):
            self.store.observe(observation(run="run-a"))
        self.store.observe(observation(run="child", session_id="tree", parent_run_id="parent"))
        with self.assertRaisesRegex(ValueError, "cross-session"):
            self.store.observe(observation(run="parent", session_id="another-tree"))
        with self.assertRaisesRegex(ValueError, "cyclic"):
            self.store.observe(observation(run="parent", session_id="tree", parent_run_id="child"))

    def test_snapshot_limits_history_cursors_and_detached_data(self):
        self.store.ingest(event())
        self.store.ingest(event(2, "run.activity", {"text": "First"}))
        self.store.ingest(event(3, "run.activity", {"text": "Second"}))
        first = self.store.events("run-a", limit=2)
        self.assertTrue(first["has_more"])
        second = self.store.events("run-a", after=first["next_cursor"], limit=2)
        self.assertEqual(len(second["events"]), 1)
        self.assertFalse(second["has_more"])
        self.store.ingest(event(4, run="run-b"))
        snapshot = self.store.snapshot(limit=1)
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["total_runs"], 2)
        snapshot["runs"][0]["name"] = "Not persisted"
        self.assertNotIn("Not persisted", [run["name"] for run in self.store.snapshot()["runs"]])

    def test_restart_makes_sdk_liveness_unknown_and_old_observations_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = Store(path, stale_after=15)
            store.ingest(event())
            self.assertEqual(store.snapshot()["runs"][0]["health"], "fresh")
            old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            store.observe(observation(last_seen=old))
            store.close()
            restarted = Store(path)
            try:
                rows = {run["run_id"]: run for run in restarted.snapshot()["runs"]}
                self.assertEqual(rows["run-a"]["health"], "unknown")
                self.assertEqual(rows["observed-a"]["health"], "stale")
                self.assertEqual(len(restarted.events("run-a")["events"]), 1)
            finally:
                restarted.close()

    def test_staleness_does_not_invent_a_terminal_state(self):
        self.store.ingest(event())
        with patch("spiritboard.store.time.time", return_value=self.store.started_at + 100):
            row = self.store.snapshot()["runs"][0]
        self.assertEqual(row["health"], "stale")
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["ended_at"])

    def test_unknown_run_activity_and_second_start_rejected(self):
        with self.assertRaisesRegex(ValueError, "must start"):
            self.store.ingest(event(1, "run.activity", {"text": "Too early"}))
        self.store.ingest(event())
        with self.assertRaisesRegex(ValueError, "already started"):
            self.store.ingest(event(2))

    def test_activity_buckets_recent_events_per_run(self):
        self.store.ingest(event())
        self.store.ingest(event(2, "run.activity", {"text": "Working"}))
        self.store.ingest(event(1, run="run-b", producer="producer-b"))
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.store.observe(observation(run="old-native", last_seen=old))
        self.store.observe(observation(run="new-native"))
        activity = self.store.activity(window=600, bucket=10)
        self.assertEqual(activity["buckets"], 60)
        self.assertEqual(sum(activity["runs"]["run-a"]), 2)
        self.assertEqual(activity["runs"]["run-a"][-1], 2)
        self.assertEqual(sum(activity["runs"]["run-b"]), 1)
        self.assertEqual(sum(activity["runs"]["new-native"]), 1)
        self.assertNotIn("old-native", activity["runs"])


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


class TimeTravelTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.base = time.time() - 3600

    def tearDown(self):
        self.store.close()

    def ingest_at(self, offset, value):
        with patch("spiritboard.store.now", return_value=iso(self.base + offset)):
            self.store.ingest(value)

    def board(self, offset):
        return {r["run_id"]: r for r in self.store.snapshot_at(self.base + offset)["runs"]}

    def test_sdk_runs_replay_their_journal_up_to_the_moment(self):
        self.ingest_at(0, event())
        self.ingest_at(100, event(2, "run.activity", {"text": "Reading files"}))
        self.ingest_at(200, event(3, "run.state_changed", {"state": "waiting_user"}))
        self.ingest_at(250, event(1, run="run-b", producer="producer-b"))
        self.ingest_at(300, event(4, "run.finished", {"state": "completed", "summary": "Done"}))
        before = self.store.snapshot_at(self.base - 1)
        self.assertEqual(before["runs"], [])
        self.assertEqual(before["at"], iso(self.base - 1))
        early = self.board(5)
        self.assertEqual((early["run-a"]["state"], early["run-a"]["activity"]), ("working", ""))
        self.assertEqual(early["run-a"]["health"], "fresh")
        self.assertEqual(self.board(90)["run-a"]["health"], "stale")
        middle = self.board(150)
        self.assertEqual(middle["run-a"]["activity"], "Reading files")
        self.assertEqual(middle["run-a"]["last_seen"], iso(self.base + 100))
        self.assertNotIn("run-b", middle)
        waiting = self.board(260)
        self.assertEqual(waiting["run-a"]["state"], "waiting_user")
        self.assertIn("run-b", waiting)
        done = self.board(310)["run-a"]
        self.assertEqual((done["state"], done["summary"]), ("completed", "Done"))
        self.assertNotIn("producer_id", done)

    def test_observations_show_their_latest_recorded_state(self):
        def seen(offset, **changes):
            last = iso(self.base + offset)
            changes.setdefault("started_at", iso(self.base))
            self.store.observe(observation(last_seen=last, **changes))

        seen(10, state="working", activity="Running tests", archived=True)
        seen(500, state="idle", activity="Turn ended", archived=True)
        seen(700, run="observed-late", started_at=iso(self.base + 600))
        self.assertEqual(self.board(5), {})
        first = self.board(100)["observed-a"]
        self.assertEqual((first["state"], first["activity"]), ("working", "Running tests"))
        self.assertEqual(first["last_seen"], iso(self.base + 10))
        self.assertEqual(first["health"], "stale")
        self.assertNotIn("archived", first)
        later = self.board(550)
        self.assertEqual(later["observed-a"]["state"], "idle")
        self.assertEqual(later["observed-a"]["health"], "observed")
        self.assertNotIn("observed-late", later)
        self.assertIn("observed-late", self.board(800))

    def test_activity_window_and_end(self):
        self.ingest_at(-7200, event())
        self.ingest_at(-60, event(2, "run.activity", {"text": "Earlier"}))
        self.ingest_at(0, event(3, "run.activity", {"text": "Later"}))
        with patch("spiritboard.store.time.time", return_value=self.base + 30):
            day = self.store.activity(86400, 1200)
            hour = self.store.activity(3600, 60, end=self.base - 30)
        self.assertEqual((day["buckets"], day["bucket_seconds"]), (72, 1200))
        self.assertEqual(sum(day["runs"]["run-a"]), 3)
        self.assertEqual(day["runs"]["run-a"][-1], 2)
        self.assertEqual(hour["end"], iso(self.base - 30))
        self.assertEqual(sum(hour["runs"]["run-a"]), 1)
        self.assertEqual(hour["runs"]["run-a"][-1], 1)

    def test_retention_prunes_on_a_schedule(self):
        clock = [time.time()]
        month = 30 * 86400
        self.store.observe(observation(run="ancient", last_seen=iso(clock[0] - 40 * 86400)))
        self.store.observe(observation(run="aging", last_seen=iso(clock[0] - month + 5400)))
        self.store.observe(observation(run="fresh"))
        with patch("spiritboard.store.now", return_value=iso(clock[0] - 40 * 86400)):
            self.store.ingest(event(run="active-sdk"))
        kept = []

        class Hourly:
            def __init__(self):
                self.waits = []

            def wait(self, seconds):
                kept.append({r["run_id"] for r in store.snapshot()["runs"]})
                self.waits.append(seconds)
                clock[0] += seconds
                return len(self.waits) == 3

        store, stopping = self.store, Hourly()
        with patch("spiritboard.store.time.time", side_effect=lambda: clock[0]):
            keep_pruned(self.store, 30, stopping)
        self.assertEqual(stopping.waits, [3600, 3600, 3600])
        self.assertEqual(kept[0], {"aging", "fresh", "active-sdk"})
        self.assertEqual(kept[1], {"aging", "fresh", "active-sdk"})
        self.assertEqual(kept[2], {"fresh", "active-sdk"})

    def test_retention_stops_quietly_after_close(self):
        self.store.close()
        stopping = Mock()
        stopping.wait.return_value = True
        keep_pruned(self.store, 30, stopping)
        stopping.wait.assert_called_once_with(3600)
        self.store = Store()
