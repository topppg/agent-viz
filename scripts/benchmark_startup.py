"""Measure fresh synthetic collector launches; never discover native profiles."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


def health_ready(data: Path, timeout: float) -> bool:
    try:
        metadata = json.loads((data / "instance.json").read_text())
        parsed = urlsplit(metadata["url"])
        if parsed.hostname != "127.0.0.1" or parsed.scheme != "http" or not parsed.port:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            body = json.loads(response.read(8193))
            return (
                response.status == 200
                and body.get("ok") is True
                and body.get("service") == "spiritboard"
                and body.get("instance_id") == metadata["instance_id"]
            )
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return False


def wait_ready(process: subprocess.Popen, data: Path, started: float, timeout: float) -> float:
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Collector exited before becoming healthy")
        remaining = deadline - time.perf_counter()
        if remaining > 0 and health_ready(data, min(0.1, remaining)):
            return time.perf_counter() - started
        time.sleep(min(0.01, max(0, deadline - time.perf_counter())))
    raise RuntimeError("Collector did not become healthy before the deadline")


def rss_mib(pid: int) -> float:
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    kib = int(result.stdout.strip())
    if kib <= 0:
        raise RuntimeError("Process RSS was unavailable")
    return kib / 1024


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def sample(timeout: float, settle: float) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="spiritboard-benchmark-") as temporary:
        root = Path(temporary)
        environment = {
            key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "LANG"}
        }
        command = [
            sys.executable,
            "-m",
            "spiritboard",
            "serve",
            "--demo",
            "--demo-delay",
            "0",
            "--no-discovery",
            "--no-open",
            "--port",
            "0",
            "--data-dir",
            str(root / "board"),
        ]
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            elapsed = wait_ready(process, root / "board", started, timeout)
            time.sleep(settle)
            observations = []
            for _ in range(5):
                if process.poll() is not None:
                    raise RuntimeError("Collector exited during RSS observation")
                observations.append(rss_mib(process.pid))
                time.sleep(0.1)
            return {
                "health_ready_ms": elapsed * 1000,
                "observed_rss_mib": statistics.median(observations),
            }
        finally:
            stop(process)


def summarize(values: list[float]) -> dict[str, float]:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def machine() -> dict[str, str | int | None]:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        cpu = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--settle", type=float, default=1)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 < args.timeout <= 60 or not 0 <= args.settle <= 30:
        parser.error("use 1–100 samples, timeout (0, 60], and settle [0, 30] seconds")
    if sys.platform not in {"darwin", "linux"}:
        parser.error("RSS measurement currently supports macOS and Linux ps only")
    samples = [sample(args.timeout, args.settle) for _ in range(args.samples)]
    print(
        json.dumps(
            {
                "machine": machine(),
                "samples": samples,
                "settle_seconds": args.settle,
                "health_ready_ms": summarize([row["health_ready_ms"] for row in samples]),
                "observed_rss_mib": summarize([row["observed_rss_mib"] for row in samples]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
