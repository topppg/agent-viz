"""Invented, capture-only workload delivered through the real event validator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

from spiritboard.store import Store

PROFILES = (
    ("Codex", "work"),
    ("Claude Code", "work"),
    ("opencode", "local"),
    ("Pi", "local"),
    ("DeepSeek", "sdk"),
    ("SDK", "pipeline"),
    ("Codex", "personal"),
    ("Claude Code", "personal"),
)
PROJECTS = (
    "Aurora API",
    "Atlas frontend",
    "Orbit search",
    "Cinder CLI",
    "Prism data",
    "Beacon release",
    "Lumen docs",
    "Nova tests",
)
TASKS = (
    "Coordinating a synthetic release across independent workers",
    "Reviewing retry handling and timeout boundaries",
    "Running deterministic integration fixtures",
    "Checking permissions and safe defaults",
    "Updating quick-start examples and API notes",
    "Comparing benchmark output against a local baseline",
)
ACTIVITIES = (
    "Reviewing retry boundaries before the next test pass",
    "Running 24 invented fixture checks · no external services",
    "Tracing the synthetic queue through a nested worker",
    "Checking the diff for missing failure paths",
    "Refreshing example output and validating links",
    "Comparing deterministic samples across two test runs",
)


@dataclass
class Scene:
    store: Store
    sequence: int = 0

    def emit(self, index: int, kind: str, payload: dict, at: float) -> None:
        self.sequence += 1
        profile, worker = divmod(index, 6)
        event = {
            "schema_version": 1,
            "event_id": f"synthetic-event-{self.sequence}",
            "session_id": f"synthetic-session-{profile}",
            "run_id": f"synthetic-run-{index}",
            "producer_id": "synthetic-capture",
            "sequence": self.sequence,
            "occurred_at": datetime.fromtimestamp(at, UTC).isoformat(),
            "type": kind,
            "payload": payload,
        }
        if worker:
            parent = profile * 6 + (1 if worker == 3 else 0)
            event["parent_run_id"] = f"synthetic-run-{parent}"
        self.store.ingest(event)

    def tick(self, step: int, at: float) -> None:
        for profile in range(len(PROFILES)):
            worker = (step + profile) % 6
            if worker == 4 and profile < 6:
                worker = 2
            self.emit(
                profile * 6 + worker, "run.activity", {"text": ACTIVITIES[(step + profile) % 6]}, at
            )


def seed_scene(store: Store, at: float) -> Scene:
    """Seed only a caller-owned temporary store, before any HTTP thread exists."""
    scene = Scene(store)
    roles = ("coordinator", "API review", "test worker", "edge-case worker", "docs", "benchmark")
    with patch("spiritboard.store.now") as clock:
        clock.return_value = datetime.fromtimestamp(at - 599, UTC).isoformat()
        for profile, (source, account) in enumerate(PROFILES):
            for worker, role in enumerate(roles):
                scene.emit(
                    profile * 6 + worker,
                    "run.started",
                    {
                        "name": f"{PROJECTS[profile]} · {role}",
                        "task": TASKS[worker],
                        "source": source,
                        "account": account,
                        "project": PROJECTS[profile],
                        "demo": True,
                    },
                    at - 599,
                )
        for bucket in range(60):
            instant = at - 595 + bucket * 10
            clock.return_value = datetime.fromtimestamp(instant, UTC).isoformat()
            for index in range(48):
                if (index * 7 + bucket * 3) % 11 < 3 + bucket % 6:
                    scene.emit(
                        index, "run.activity", {"text": ACTIVITIES[(index + bucket) % 6]}, instant
                    )
        clock.return_value = datetime.fromtimestamp(at, UTC).isoformat()
        for index in range(48):
            scene.emit(index, "run.activity", {"text": ACTIVITIES[index % 6]}, at)
            if index % 6 == 4 and index // 6 < 6:
                scene.emit(
                    index,
                    "run.state_changed",
                    {"state": "waiting_user", "reason": "Approve the invented example wording?"},
                    at,
                )
    return scene
