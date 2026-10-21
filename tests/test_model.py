import unittest

from spiritboard.model import now, parse_json, timestamp, validate_event


def event(kind="run.started", payload=None, **changes):
    value = dict(
        schema_version=1,
        event_id="event-1",
        session_id="session-1",
        run_id="run-1",
        producer_id="producer-1",
        sequence=1,
        occurred_at=now(),
        type=kind,
        payload={"name": "Synthetic run"} if payload is None else payload,
    )
    value.update(changes)
    return value


class ModelTests(unittest.TestCase):
    def test_valid_event_is_independent_copy(self):
        original = event()
        validated = validate_event(original)
        self.assertEqual(original, validated)
        validated["payload"]["name"] = "Changed"
        self.assertEqual(original["payload"]["name"], "Synthetic run")

    def test_strict_json_rejects_duplicate_nonfinite_and_malformed(self):
        for raw in [
            b'{"x":1,"x":2}',
            b'{"payload":{"x":1,"x":2}}',
            b'{"x": NaN}',
            b'{"x": Infinity}',
            b'{"x": -Infinity}',
            b'{"x":',
            b"{} {}",
            b"\xff",
        ]:
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeDecodeError)):
                parse_json(raw)

    def test_unknown_fields_and_prompt_payloads_are_rejected(self):
        cases = [
            event(extra="not allowed"),
            event(payload={"name": "Run", "prompt": "Private"}),
            event(payload={"name": "Run", "tool_arguments": {}}),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_event(value)

    def test_schema_sequence_and_required_fields(self):
        cases = [
            event(schema_version=True),
            event(schema_version=2),
            event(sequence=True),
            event(sequence=0),
            event(sequence=2**53),
            event(sequence=1.0),
            event(run_id=""),
            event(producer_id="x" * 257),
        ]
        missing = event()
        del missing["event_id"]
        cases.append(missing)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_event(value)

    def test_timestamps_require_timezone_and_finite_valid_dates(self):
        for value in ["2026-10-20T12:00:00", "not-a-date", "2026-99-99T00:00:00Z", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                timestamp(value)
        self.assertIsInstance(timestamp("2026-10-20T12:00:00Z"), float)

    def test_text_limits_control_characters_and_self_parent(self):
        cases = [
            event(payload={"name": "x" * 241}),
            event(payload={"name": "bad\x00"}),
            event(parent_run_id="run-1"),
            event(payload={"name": "Run", "task": "x" * 601}),
            event(payload={"name": "Run", "demo": "true"}),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_event(value)

    def test_each_event_type_enforces_its_contract(self):
        valid = [
            event("run.activity", {"text": "Checking", "provenance": "adapter"}),
            event("run.state_changed", {"state": "working"}),
            event("run.heartbeat", {}),
            event("run.finished", {"state": "completed"}),
            event("operation.started", {"operation_id": "op", "kind": "tool", "label": "Read"}),
            event("operation.finished", {"operation_id": "op", "outcome": "success"}),
        ]
        for value in valid:
            with self.subTest(kind=value["type"]):
                validate_event(value)
        invalid = [
            event("unknown", {}),
            event("run.activity", {"text": "x", "provenance": "guessed"}),
            event("run.state_changed", {"state": "running"}),
            event("run.finished", {"state": "working"}),
            event("run.heartbeat", {"state": "working"}),
            event("operation.started", {"operation_id": "op", "kind": "write", "label": "Write"}),
            event("operation.finished", {"operation_id": "op", "outcome": "completed"}),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_event(value)
