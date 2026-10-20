"""Small, bounded event contract. No prompts or tool payloads are accepted."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

MAX_EVENT_BYTES = 32_768
STATES = {"working", "waiting_tool", "waiting_child", "waiting_user", "idle"}
TERMINAL = {"completed", "failed", "cancelled"}
TYPES = {
    "run.started": {"name", "task", "source", "account", "project", "demo"},
    "run.activity": {"text", "provenance"},
    "run.state_changed": {"state", "reason"},
    "run.heartbeat": set(),
    "run.finished": {"state", "summary"},
    "operation.started": {"operation_id", "kind", "label"},
    "operation.finished": {"operation_id", "outcome", "detail"},
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def timestamp(value: object) -> float:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("timestamp must be a bounded ISO string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        result = dt.timestamp()
        if not math.isfinite(result):
            raise ValueError("invalid timestamp")
        return result
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid timestamp") from exc


def text(value: object, limit: int = 256, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid or oversized text field")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("control characters are not accepted")
    return value


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(data: bytes) -> object:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON field")
            out[key] = value
        return out

    def invalid(_):
        raise ValueError("non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def validate_event(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    allowed = {
        "schema_version",
        "event_id",
        "session_id",
        "run_id",
        "parent_run_id",
        "producer_id",
        "sequence",
        "occurred_at",
        "type",
        "payload",
    }
    required = allowed - {"parent_run_id"}
    if set(raw) - allowed or not required.issubset(raw):
        raise ValueError("invalid event fields")
    if len(canonical(raw).encode()) > MAX_EVENT_BYTES:
        raise ValueError("event too large")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported schema version")
    for key in ("event_id", "session_id", "run_id", "producer_id"):
        text(raw[key])
    if raw.get("parent_run_id") is not None:
        text(raw["parent_run_id"])
        if raw["parent_run_id"] == raw["run_id"]:
            raise ValueError("self parent")
    if type(raw["sequence"]) is not int or not 1 <= raw["sequence"] < 2**53:
        raise ValueError("invalid sequence")
    timestamp(raw["occurred_at"])
    kind = raw["type"]
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError("unknown event type")
    payload = raw["payload"]
    if not isinstance(payload, dict) or set(payload) - TYPES[kind]:
        raise ValueError("invalid payload fields")
    for key, value in payload.items():
        if key == "demo":
            if type(value) is not bool:
                raise ValueError("demo must be boolean")
        else:
            text(
                value,
                600 if key in {"task", "text", "summary", "detail", "reason"} else 256,
                empty=True,
            )
    if kind == "run.started":
        text(payload.get("name"), 240)
    elif kind == "run.activity":
        text(payload.get("text"), 600, empty=True)
        if payload.get("provenance", "application") not in {"application", "adapter"}:
            raise ValueError("invalid activity provenance")
    elif kind == "run.state_changed":
        if payload.get("state") not in STATES:
            raise ValueError("invalid execution state")
    elif kind == "run.finished":
        if payload.get("state") not in TERMINAL:
            raise ValueError("invalid terminal state")
    elif kind.startswith("operation."):
        text(payload.get("operation_id"))
        if kind == "operation.started":
            if payload.get("kind") not in {"tool", "model"}:
                raise ValueError("invalid operation kind")
            text(payload.get("label"), 240)
        elif payload.get("outcome") not in {"success", "error"}:
            raise ValueError("invalid operation outcome")
    return json.loads(canonical(raw))
