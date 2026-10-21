"""Local process lookups through fixed ``ps``/``lsof`` argv lists, never a shell."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

TIMEOUT = 3.0
PS_LINE = re.compile(
    r"\s*(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.+)"
)
PROCESS_START = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?:[1-9]|[12]\d|3[01]) (?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d \d{4}"
)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    tty: str
    start: str
    command: str


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str] | None:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.returncode, done.stdout


def utc_env() -> dict[str, str]:
    # UTC and the C locale match the procStart format Claude Code records.
    return {**os.environ, "TZ": "UTC", "LC_ALL": "C"}


def process(pid: int) -> Process | None:
    """None when no such process; OSError when ps itself could not answer."""
    argv = ["ps", "-o", "ppid=", "-o", "tty=", "-o", "lstart=", "-o", "comm=", "-p", str(pid)]
    result = run(argv, utc_env())
    if result is None:
        raise OSError("process lookup failed")
    lines = [line for line in result[1].splitlines() if line.strip()]
    if result[0] == 1 and not lines:
        return None
    match = PS_LINE.fullmatch(lines[0]) if lines else None
    if result[0] != 0 or not match:
        raise OSError("process lookup failed")
    start = " ".join(match[3].split())
    return Process(pid, int(match[1]), match[2], start, match[4].strip())


def process_start(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if PROCESS_START.fullmatch(normalized) else None
