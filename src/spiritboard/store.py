"""Transactional local event journal and current run projection."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .harnesses import HARNESSES
from .model import STATES, TERMINAL, canonical, now, text, timestamp, validate_event


class Store:
    def __init__(self, path: str | Path = ":memory:", stale_after: float = 15):
        self.lock = threading.RLock()
        self.stale_after = stale_after
        self.started_at = time.time()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                run_id TEXT NOT NULL, producer_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                data TEXT NOT NULL, received_at TEXT NOT NULL, applied INTEGER NOT NULL,
                UNIQUE(producer_id, sequence));
            CREATE INDEX IF NOT EXISTS by_run ON events(run_id, cursor);
            CREATE INDEX IF NOT EXISTS by_received ON events(received_at);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES('stream_id',?)", (str(uuid.uuid4()),)
        )
        self.stream_id = self.db.execute(
            "SELECT value FROM metadata WHERE key='stream_id'"
        ).fetchone()[0]

    def close(self):
        with self.lock:
            self.db.close()

    def _get(self, run_id):
        row = self.db.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, run):
        self.db.execute(
            "INSERT INTO runs VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET data=excluded.data",
            (run["run_id"], canonical(run)),
        )

    def _check_parent(self, run_id, parent, session):
        seen = {run_id}
        while parent:
            if parent in seen:
                raise ValueError("cyclic parent relationship")
            seen.add(parent)
            record = self._get(parent)
            if record is None:
                return
            if record["session_id"] != session:
                raise ValueError("cross-session parent")
            parent = record.get("parent_run_id")

    def _check_children(self, run_id, session):
        for (data,) in self.db.execute("SELECT data FROM runs"):
            child = json.loads(data)
            if child.get("parent_run_id") == run_id and child["session_id"] != session:
                raise ValueError("cross-session child")

    def ingest(self, raw: object) -> dict:
        event = validate_event(raw)
        encoded = canonical(event)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                duplicate = self.db.execute(
                    "SELECT cursor,data FROM events WHERE event_id=?", (event["event_id"],)
                ).fetchone()
                if duplicate:
                    if duplicate[1] != encoded:
                        raise ValueError("conflicting duplicate event")
                    self.db.execute("COMMIT")
                    return {"cursor": duplicate[0], "duplicate": True}
                used = self.db.execute(
                    "SELECT 1 FROM events WHERE producer_id=? AND sequence=?",
                    (event["producer_id"], event["sequence"]),
                ).fetchone()
                if used:
                    raise ValueError("producer sequence already used")
                run = self._get(event["run_id"])
                payload, kind = event["payload"], event["type"]
                received = now()
                if run is None:
                    if kind != "run.started":
                        raise ValueError("run must start before other events")
                    parent = event.get("parent_run_id")
                    self._check_parent(event["run_id"], parent, event["session_id"])
                    self._check_children(event["run_id"], event["session_id"])
                    run = {
                        "run_id": event["run_id"],
                        "session_id": event["session_id"],
                        "parent_run_id": parent,
                        "name": payload["name"],
                        "task": payload.get("task", ""),
                        "activity": "",
                        "summary": "",
                        "source": payload.get("source", "sdk"),
                        "account": payload.get("account", "local"),
                        "project": payload.get("project", ""),
                        "demo": payload.get("demo", False),
                        "state": "working",
                        "inferred": False,
                        "evidence": "explicit SDK lifecycle",
                        "started_at": event["occurred_at"],
                        "ended_at": None,
                        "operations": [],
                        "warnings": [],
                        "producer_id": event["producer_id"],
                        "last_sequence": 0,
                        "mode": "sdk",
                    }
                elif (
                    run["session_id"] != event["session_id"]
                    or run.get("parent_run_id") != event.get("parent_run_id")
                    or run.get("producer_id") != event["producer_id"]
                ):
                    raise ValueError("run identity changed")
                applied = event["sequence"] > run["last_sequence"]
                producer_max = (
                    self.db.execute(
                        "SELECT MAX(sequence) FROM events WHERE producer_id=?",
                        (event["producer_id"],),
                    ).fetchone()[0]
                    or 0
                )
                warnings = run["warnings"]
                if event["sequence"] > producer_max + 1 and "producer sequence gap" not in warnings:
                    warnings.append("producer sequence gap")
                if not applied and "delayed event ignored for current state" not in warnings:
                    warnings.append("delayed event ignored for current state")
                if applied:
                    terminal = run["state"] in TERMINAL
                    if kind == "run.started" and run["last_sequence"]:
                        raise ValueError("run already started")
                    if not terminal:
                        if kind == "run.activity":
                            run["activity"] = payload["text"]
                        elif kind == "run.state_changed":
                            run["state"] = payload["state"]
                            run["activity"] = payload.get("reason", run["activity"])
                        elif kind == "run.finished":
                            run["state"] = payload["state"]
                            run["summary"] = payload.get("summary", "")
                            run["ended_at"] = event["occurred_at"]
                        elif kind.startswith("operation."):
                            operations = run["operations"]
                            operation = next(
                                (
                                    op
                                    for op in operations
                                    if op["operation_id"] == payload["operation_id"]
                                ),
                                None,
                            )
                            if kind == "operation.started":
                                if operation:
                                    raise ValueError("operation already started")
                                if len(operations) >= 200:
                                    finished = next(
                                        (i for i, op in enumerate(operations) if not op["pending"]),
                                        None,
                                    )
                                    if finished is None:
                                        raise ValueError("too many parallel operations")
                                    operations.pop(finished)
                                operations.append(
                                    {
                                        **payload,
                                        "pending": True,
                                        "started_at": event["occurred_at"],
                                        "finished_at": None,
                                    }
                                )
                            else:
                                if not operation or not operation["pending"]:
                                    raise ValueError("no matching active operation")
                                operation.update(
                                    payload, pending=False, finished_at=event["occurred_at"]
                                )
                    elif kind not in {"run.heartbeat", "run.finished"}:
                        warnings.append("post-terminal event did not change current state")
                    run["last_sequence"] = event["sequence"]
                    run["last_seen"] = received
                    run["updated_at"] = received
                run["warnings"] = warnings[-10:]
                row = self.db.execute(
                    "INSERT INTO events(event_id,run_id,producer_id,sequence,data,received_at,"
                    "applied) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        event["event_id"],
                        event["run_id"],
                        event["producer_id"],
                        event["sequence"],
                        encoded,
                        received,
                        int(applied),
                    ),
                )
                self._put(run)
                self.db.execute("COMMIT")
                return {"cursor": row.lastrowid, "duplicate": False, "applied": applied}
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def observe(self, observation: dict) -> bool:
        """Upsert a bounded read-only file observation; never claim a live heartbeat."""
        allowed = {
            "run_id",
            "session_id",
            "parent_run_id",
            "native_id",
            "name",
            "task",
            "activity",
            "source",
            "account",
            "project",
            "state",
            "inferred",
            "evidence",
            "last_seen",
            "started_at",
            "health",
            "app",
            "archived",
        }
        data = {k: v for k, v in observation.items() if k in allowed}
        for key in ("run_id", "session_id", "name", "source", "account"):
            text(data.get(key))
        if data["source"] not in HARNESSES:
            raise ValueError("unknown observation source")
        for key in ("native_id", "task", "activity", "evidence", "project"):
            data[key] = text(data.get(key, ""), 600, empty=True)
        if data.get("parent_run_id") is not None:
            text(data["parent_run_id"])
        if "app" in data:
            text(data["app"], 40)
        if "archived" in data and data["archived"] is not True:
            raise ValueError("invalid archived flag")
        if data.get("state") not in STATES | TERMINAL | {"unknown"}:
            raise ValueError("invalid observed state")
        timestamp(data["last_seen"])
        if data.get("started_at"):
            timestamp(data["started_at"])
        data.update(
            inferred=True,
            mode="observation",
            health="observed",
            demo=False,
            summary="",
            operations=[],
            warnings=[],
            ended_at=None,
        )
        with self.lock:
            previous = self._get(data["run_id"])
            if previous and previous.get("mode") != "observation":
                raise ValueError("observation collides with SDK run")
            if previous and {k: v for k, v in previous.items() if k != "updated_at"} == data:
                return False
            self._check_parent(data["run_id"], data.get("parent_run_id"), data["session_id"])
            self._check_children(data["run_id"], data["session_id"])
            if previous and (
                previous["session_id"] != data["session_id"]
                or previous.get("parent_run_id") != data.get("parent_run_id")
            ):
                raise ValueError("observed identity changed")
            data["updated_at"] = now()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._put(data)
                # Observation events carry only the same filtered display metadata.
                event = {
                    "type": "run.observed",
                    "run_id": data["run_id"],
                    "payload": {
                        "state": data["state"],
                        "text": data["activity"],
                        "evidence": data["evidence"],
                    },
                    "occurred_at": data["last_seen"],
                }
                self.db.execute(
                    "INSERT INTO events(event_id,run_id,producer_id,sequence,data,received_at,"
                    "applied) "
                    "VALUES(?,?,?,?,?,?,1)",
                    (
                        str(uuid.uuid4()),
                        data["run_id"],
                        "observation:" + str(uuid.uuid4()),
                        1,
                        canonical(event),
                        now(),
                    ),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return True

    def run(self, run_id: str) -> dict | None:
        """Server-side record including private native identity; never sent as-is."""
        with self.lock:
            return self._get(run_id)

    def snapshot(self, limit: int = 500) -> dict:
        with self.lock:
            rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM runs")]
            rows.sort(key=lambda r: r.get("last_seen", ""), reverse=True)
            current = time.time()
            for run in rows:
                seen = timestamp(run["last_seen"])
                if run["mode"] == "observation":
                    run["health"] = "observed" if current - seen <= 60 else "stale"
                elif run["state"] in TERMINAL:
                    run["health"] = "unknown"
                elif seen < self.started_at:
                    run["health"] = "unknown"
                else:
                    run["health"] = "fresh" if current - seen <= self.stale_after else "stale"
                for private in ("producer_id", "last_sequence", "native_id"):
                    run.pop(private, None)
            cursor = self.db.execute("SELECT COALESCE(MAX(cursor),0) FROM events").fetchone()[0]
            return {
                "cursor": cursor,
                "stream_id": self.stream_id,
                "runs": rows[:limit],
                "truncated": len(rows) > limit,
                "total_runs": len(rows),
                "server_time": now(),
            }

    def activity(self, window: int = 600, bucket: int = 10) -> dict:
        """Per-run event counts in fixed buckets, oldest first, ending now."""
        count = window // bucket
        end = time.time()
        end_iso = datetime.fromtimestamp(end, UTC).isoformat()
        start_iso = datetime.fromtimestamp(end - window, UTC).isoformat()
        # Native observations are journaled when a scan sees a change; bucket them
        # by transcript time so a startup scan of old files is not a fake burst.
        query = """
            SELECT run_id, CAST((julianday(?) - julianday(
                CASE WHEN producer_id LIKE 'observation:%' THEN json_extract(data, '$.occurred_at')
                     ELSE received_at END)) * 86400 / ? AS INTEGER) AS age, COUNT(*)
            FROM events WHERE received_at >= ? GROUP BY run_id, age"""
        runs: dict[str, list[int]] = {}
        with self.lock:
            rows = self.db.execute(query, (end_iso, bucket, start_iso)).fetchall()
        for run_id, age, total in rows:
            if age is None or not 0 <= age < count:
                continue
            runs.setdefault(run_id, [0] * count)[count - 1 - age] += total
        return {"bucket_seconds": bucket, "buckets": count, "end": end_iso, "runs": runs}

    def events(self, run_id: str, after: int = 0, limit: int = 100) -> dict:
        limit = min(max(limit, 1), 200)
        with self.lock:
            rows = self.db.execute(
                "SELECT cursor,data,received_at,applied FROM events "
                "WHERE run_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                (run_id, after, limit + 1),
            ).fetchall()
        events = [
            {**json.loads(row[1]), "cursor": row[0], "received_at": row[2], "applied": bool(row[3])}
            for row in rows[:limit]
        ]
        return {
            "events": events,
            "next_cursor": events[-1]["cursor"] if events else after,
            "has_more": len(rows) > limit,
        }

    def prune(self, older_than_days: float) -> dict:
        """Explicit deletion of this observer's old inactive history only."""
        if older_than_days <= 0:
            raise ValueError("positive retention required")
        cutoff = time.time() - older_than_days * 86400
        with self.lock:
            runs = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM runs")]
            candidates = {
                r["run_id"]
                for r in runs
                if timestamp(r["last_seen"]) < cutoff
                and (r["state"] in TERMINAL or r["mode"] == "observation")
            }
            # Never orphan a retained descendant by deleting its parent.
            for _ in range(len(runs) + 1):
                blocked = {r.get("parent_run_id") for r in runs if r["run_id"] not in candidates}
                smaller = candidates - blocked
                if smaller == candidates:
                    break
                candidates = smaller
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for run_id in candidates:
                    self.db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
                    self.db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"deleted_runs": len(candidates), "scope": "observer history only"}
