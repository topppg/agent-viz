"""Synthetic checks for the measurement helper; no real profiles or model calls."""

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "benchmark_startup", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_startup.py"
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_rss_uses_live_process_kib_not_peak() -> None:
    with patch.object(benchmark.subprocess, "run", return_value=Mock(stdout="  20480\n")) as run:
        assert benchmark.rss_mib(123) == 20
    assert run.call_args.args[0] == ["ps", "-o", "rss=", "-p", "123"]


def test_readiness_rejects_early_exit(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exited before"):
        benchmark.wait_ready(Mock(poll=lambda: 1), tmp_path, benchmark.time.perf_counter(), 1)


def test_readiness_deadline_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="deadline"):
        benchmark.wait_ready(Mock(poll=lambda: None), tmp_path, 0, 0.01)


def test_missing_metadata_is_not_ready(tmp_path: Path) -> None:
    assert not benchmark.health_ready(tmp_path, 0.1)


def test_readiness_requires_matching_instance(tmp_path: Path) -> None:
    (tmp_path / "instance.json").write_text(
        json.dumps({"url": "http://127.0.0.1:12345", "instance_id": "synthetic-instance"})
    )
    response = Mock(status=200)
    response.read.return_value = json.dumps(
        {"ok": True, "service": "spiritboard", "instance_id": "other-instance"}
    ).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    with patch.object(benchmark.http.client, "HTTPConnection", return_value=connection):
        assert not benchmark.health_ready(tmp_path, 0.1)
        response.read.return_value = json.dumps(
            {"ok": True, "service": "spiritboard", "instance_id": "synthetic-instance"}
        ).encode()
        assert benchmark.health_ready(tmp_path, 0.1)
    assert connection.close.call_count == 2


def test_measurement_failure_still_stops_owned_process() -> None:
    process = Mock()
    with (
        patch.object(benchmark.subprocess, "Popen", return_value=process),
        patch.object(benchmark, "wait_ready", side_effect=RuntimeError("not ready")),
        patch.object(benchmark, "stop") as stop,
        pytest.raises(RuntimeError, match="not ready"),
    ):
        benchmark.sample(1, 0)
    stop.assert_called_once_with(process)


def test_unresponsive_process_is_killed() -> None:
    process = Mock(poll=lambda: None)
    process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 3), None]
    benchmark.stop(process)
    process.kill.assert_called_once()


def test_summary_preserves_range() -> None:
    assert benchmark.summarize([3, 1, 2]) == {"median": 2, "min": 1, "max": 3}
