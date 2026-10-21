"""The media workload stays synthetic and exercises real event projections."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

from spiritboard import store as store_module
from spiritboard.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/demo_scene.py"
SPEC = importlib.util.spec_from_file_location("capture_scene", SCRIPT)
assert SPEC and SPEC.loader
scene_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scene_module
SPEC.loader.exec_module(scene_module)


def test_busy_scene_is_synthetic_multiharness_and_nested(tmp_path: Path) -> None:
    store = Store(tmp_path / "demo.sqlite3", stale_after=120)
    try:
        at = time.time()
        original_clock = store_module.now
        scene = scene_module.seed_scene(store, at)
        assert store_module.now is original_clock
        runs = store.snapshot()["runs"]
        assert len(runs) == 48
        assert all(run["demo"] and run["mode"] == "sdk" for run in runs)
        assert {run["source"] for run in runs} == {
            "Codex",
            "Claude Code",
            "opencode",
            "Pi",
            "DeepSeek",
            "SDK",
        }
        for source in ("Codex", "Claude Code"):
            assert {run["account"] for run in runs if run["source"] == source} == {
                "work",
                "personal",
            }
        assert sum(run["state"] == "waiting_user" for run in runs) == 6
        by_id = {run["run_id"]: run for run in runs}
        assert any(
            run["parent_run_id"] and by_id[run["parent_run_id"]]["parent_run_id"] for run in runs
        )
        chart = store.activity(end=at + 1)
        assert len(chart["runs"]) == 48
        assert all(sum(values) > 10 for values in chart["runs"].values())
        assert all(sum(values[slot] for values in chart["runs"].values()) for slot in range(60))
        assert all(not run["warnings"] for run in runs)
        before = store.snapshot()["cursor"]
        scene.tick(0, time.time())
        assert store.snapshot()["cursor"] == before + 8
        assert sum(run["state"] == "waiting_user" for run in store.snapshot()["runs"]) == 6
    finally:
        store.close()
