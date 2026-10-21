"""Bounded, no-follow readers and sanitizers shared by the harness adapters.

Readers open files nonblocking without following links and verify regular files
before reading, so links, pipes and special files never stall or widen a scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sources import SourceProfile

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 512 * 1024
REQUEST_SEARCH_BYTES = 32 * 1024 * 1024
MAX_SMALL_FILE = 64 * 1024
TEXT_HIDDEN = "Text hidden · enable include_text for this profile"
MAX_ENTRIES = 30_000
MAX_DEPTH = 7
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class Session:
    """One session unit in a harness's storage; ``key`` is stable while it exists."""

    key: str
    path: Path
    size: int
    mtime_ns: int
    mtime: float


def walk_jsonl(
    trees: list[Path], limit: int, max_age_days: float | None = None
) -> tuple[list[Session], dict]:
    """Newest ``.jsonl`` files under ``trees`` within entry/depth budgets, plus coverage."""
    cutoff_ns = time.time_ns() - int(max_age_days * 86400e9) if max_age_days else None
    diagnostics = {
        "files_seen": 0,
        "selected_files": 0,
        "truncated": False,
        "scan_limit": limit,
        "discovery_limited": False,
        "entry_budget_hit": False,
        "depth_budget_hit": False,
        "unreadable_directories": 0,
        "symlinks_skipped": 0,
        "transcript_trees": [p.name for p in trees],
        "tree_count": len(trees),
        "max_age_days": max_age_days,
        "aged_out": 0,
    }
    pending = []
    for path in trees:
        if path.is_symlink():
            diagnostics["symlinks_skipped"] += 1
        else:
            pending.append((path, 0))
    files = []
    visited = 0
    while pending and visited < MAX_ENTRIES:
        folder, depth = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        diagnostics["entry_budget_hit"] = True
                        break
                    if entry.is_symlink():
                        diagnostics["symlinks_skipped"] += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            diagnostics["depth_budget_hit"] = True
                    elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if cutoff_ns is not None and info.st_mtime_ns < cutoff_ns:
                                diagnostics["aged_out"] += 1
                                continue
                            path = Path(entry.path)
                            files.append(
                                Session(
                                    str(path), path, info.st_size, info.st_mtime_ns, info.st_mtime
                                )
                            )
                        except OSError:
                            pass
        except OSError:
            if folder in trees and len(trees) == 1:
                raise
            diagnostics["unreadable_directories"] += 1
            continue
    if pending:
        diagnostics["entry_budget_hit"] = True
    selected = sorted(files, key=lambda session: session.mtime_ns, reverse=True)[:limit]
    limited = bool(
        diagnostics["entry_budget_hit"]
        or diagnostics["depth_budget_hit"]
        or diagnostics["unreadable_directories"]
        or diagnostics["symlinks_skipped"]
    )
    diagnostics.update(
        files_seen=len(files),
        selected_files=len(selected),
        discovery_limited=limited,
        truncated=limited or len(files) > len(selected),
    )
    return selected, diagnostics


def existing_trees(root: Path, names: tuple[str, ...]) -> list[Path]:
    trees = [root / name for name in names if (root / name).is_dir()]
    if not trees:
        raise OSError("Transcript directory is unavailable")
    return trees


def records(path: Path) -> list[dict[str, Any]]:
    """Read only a bounded header/tail; ignore partial lines and large records."""
    fd = os.open(path, READ_FLAGS)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return []
        size = info.st_size
        header = stream.read(HEAD_BYTES)
        if size <= HEAD_BYTES:
            blocks = [header]
        elif size <= HEAD_BYTES + TAIL_BYTES:
            blocks = [header + stream.read(TAIL_BYTES)]
        else:
            # Drop incomplete boundaries, not just malformed JSON fragments.
            header = header.rsplit(b"\n", 1)[0] + b"\n"
            stream.seek(max(0, size - TAIL_BYTES - 1))
            tail = stream.read(TAIL_BYTES + 1)
            tail = tail.split(b"\n", 1)[-1]
            blocks = [header, tail]
    result = []
    for block in blocks:
        for line in block.splitlines(keepends=True):
            # A producer can be midway through writing an otherwise valid JSON.
            if not line.endswith(b"\n"):
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    result.append(item)
            except (ValueError, UnicodeError, RecursionError):
                continue
    return result


def last_request(path: Path, marker: bytes, parse: Callable[[dict[str, Any]], str]) -> str:
    """The latest request ``parse`` finds before the bounded tail, scanning backwards.

    Only lines containing ``marker`` (after normalizing ``": "``) are decoded.
    """
    chunk = 1024 * 1024
    try:
        fd = os.open(path, READ_FLAGS)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return ""
            end = info.st_size
            floor, carry = max(0, end - REQUEST_SEARCH_BYTES), b""
            while end > floor:
                start = max(floor, end - chunk)
                stream.seek(start)
                lines = (stream.read(end - start) + carry).split(b"\n")
                # The first piece may start mid-line; it is completed by the next chunk.
                carry, body = (lines[0], lines[1:]) if start > 0 else (b"", lines)
                for line in reversed(body):
                    if marker not in line.replace(b'": "', b'":"'):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    text = parse(row) if isinstance(row, dict) else ""
                    if text:
                        return text
                end = start
    except OSError:
        return ""
    return ""


def small_json(path: Path, limit: int = MAX_SMALL_FILE) -> dict[str, object] | None:
    """A bounded regular JSON object file, or None."""
    try:
        with os.fdopen(os.open(path, READ_FLAGS), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            raw = file.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def safe(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:limit]


def redact(value: object, limit: int = 1200) -> str:
    """Text bound for a provider: control characters, secrets, emails and paths removed."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    value = re.sub(r"(?i)\bBearer\s+\S+", "[redacted token]", value)
    value = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|AIza[A-Za-z0-9_-]{15,})",
        "[redacted key]",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[\"']?[^\s\"',;}]+",
        "[redacted credential]",
        value,
    )
    value = re.sub(
        r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[redacted email]",
        value,
    )
    value = re.sub(r"(?<![\w:])(?:[A-Za-z]:[\\/]|~?/)[^\s\"'<>),;]*", "[local path]", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def native_id(value: Any) -> str:
    # Native IDs, unlike labels, must not carry free-form content or paths.
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else ""
    )


def ns(profile: SourceProfile, native: str) -> str:
    value = f"{profile.id}:{native}"
    return (
        value
        if len(value) <= 180
        else f"{profile.id}:sha256-{hashlib.sha256(native.encode()).hexdigest()[:32]}"
    )


def utc_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        return None


def text(message: Any, limit: int = 160) -> str:
    if isinstance(message, str):
        return safe(message, limit)
    if isinstance(message, list):
        return safe(
            " ".join(
                x.get("text", "")
                for x in message
                if isinstance(x, dict)
                and x.get("type") in {"text", "input_text", "output_text"}
                and isinstance(x.get("text"), str)
            ),
            limit,
        )
    return ""


def request_text(message: object, limit: int = 160) -> str:
    """The user's latest request; wrapped tool, shell or instruction echoes are skipped."""
    value = text(message, 20_000)
    # Forked Claude subagents get shared boilerplate first; their directive follows it.
    _, fork, directive = value.partition("</fork-boilerplate>")
    if fork:
        value = re.sub(r"^\s*Your directive:\s*", "", directive)
    return "" if value.lstrip().startswith(("<", "# AGENTS.md")) else safe(value, limit)


def valid_cwd(value: object) -> str:
    if (
        isinstance(value, str)
        and value.startswith("/")
        and len(value) <= 1024
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        return value
    return ""


def observation(
    profile: SourceProfile, name: str, native: str, project: str, started: str | None
) -> dict:
    # An observer collection is NOT a vendor conversation/session. Its identity
    # stays fixed when ancestor transcripts enter or leave a bounded scan window.
    return {
        "run_id": ns(profile, native),
        "session_id": f"{profile.id}:observed",
        "parent_run_id": None,
        "native_id": native,
        "name": f"{name} {native[:8]}",
        "task": TEXT_HIDDEN,
        "activity": "Transcript observed",
        "source": profile.kind,
        "account": profile.label,
        "project": project,
        "state": "unknown",
        "inferred": True,
        "evidence": "Read-only transcript; no process liveness proof",
        "last_seen": started,
        "started_at": started,
        "health": "observed",
    }


def mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).isoformat()
