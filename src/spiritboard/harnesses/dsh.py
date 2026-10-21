"""DeepSeek Harness: zstd-compressed session logs and ``session.lock`` files under ``sessions/``."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING

from .. import processes
from ..transcripts import (
    HEAD_BYTES,
    MAX_ENTRIES,
    READ_FLAGS,
    READ_GAP,
    REQUEST_SEARCH_BYTES,
    TAIL_BYTES,
    TEXT_HIDDEN,
    Session,
    mtime_iso,
    native_id,
    ns,
    observation,
    redact,
    reply_text,
    request_text,
    safe,
    small_json,
    utc_time,
    valid_cwd,
)
from .harness import Goal, Harness

if TYPE_CHECKING:
    from ..sources import SourceProfile

TREE = "sessions"
LOG = "session.v4.jsonl.zstd"
LOCK = "session.lock"
SESSION_DIR = re.compile(r"session-[0-9A-Za-z][0-9A-Za-z-]{7,70}")
PROJCACHE = Path("storages") / "session_projcache" / "sessions"
WORKSPACES = Path("storages") / "workspace.json"
MAX_STORAGE_FILE = 4 * 1024 * 1024
CHUNK = 256 * 1024
DECODE_SECONDS = 5.0
# A parent's prompt is a subagent's request; titles, tools and checkpoints are not.
REQUEST_SOURCES = {"user", "parent"}
NO_DECODER = "Transcript compressed · no zstd decoder (Python 3.14+ or the zstd CLI)"
# Reads an open, verified regular file and yields decompressed chunks.
Decoder = Callable[[IO[bytes]], Iterator[bytes]]
_TITLES: dict[str, tuple[tuple[int, int], str]] = {}


def _stdlib_decoder() -> Decoder | None:
    try:
        from compression import zstd
    except ImportError:
        return None

    def chunks(stream: IO[bytes]) -> Iterator[bytes]:
        decompressor = zstd.ZstdDecompressor()
        while data := stream.read(CHUNK):
            while True:
                try:
                    out = decompressor.decompress(data, CHUNK)
                except zstd.ZstdError:
                    return
                data = b""
                if out:
                    yield out
                if decompressor.eof:
                    # A log can hold several frames; the next starts in unused_data.
                    data, decompressor = decompressor.unused_data, zstd.ZstdDecompressor()
                    if not data:
                        break
                elif decompressor.needs_input:
                    break

    return chunks


_STDLIB = _stdlib_decoder()


def _cli_chunks(binary: str, stream: IO[bytes]) -> Iterator[bytes]:
    deadline = time.monotonic() + DECODE_SECONDS
    try:
        child = subprocess.Popen(
            [binary, "-d", "-c", "-q"],
            stdin=stream.fileno(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return
    assert child.stdout is not None
    try:
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([child.stdout], [], [], left)[0]:
                break
            data = os.read(child.stdout.fileno(), CHUNK)
            if not data:
                break
            yield data
    finally:
        child.kill()
        child.wait()
        child.stdout.close()


def decoder() -> tuple[str, Decoder] | None:
    """The stdlib decoder on 3.14+, else the ``zstd`` CLI, else None."""
    if _STDLIB:
        return "compression.zstd", _STDLIB
    binary = shutil.which("zstd")
    if binary:
        return "zstd", lambda stream: _cli_chunks(binary, stream)
    return None


def _decode(path: Path, sink: Callable[[bytes], None]) -> bool:
    """Feed up to REQUEST_SEARCH_BYTES of decompressed log to ``sink``; False with no decoder."""
    found = decoder()
    if found is None:
        return False
    with os.fdopen(os.open(path, READ_FLAGS), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return True
        chunks, total = found[1](stream), 0
        try:
            for chunk in chunks:
                chunk = chunk[: REQUEST_SEARCH_BYTES - total]
                total += len(chunk)
                sink(chunk)
                if total >= REQUEST_SEARCH_BYTES:
                    break
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()
    return True


def _parse_lines(block: bytes) -> list[dict]:
    result = []
    for line in block.splitlines(keepends=True):
        # A torn final frame or a write in progress leaves an incomplete line.
        if not line.endswith(b"\n"):
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def records(path: Path) -> list[dict] | None:
    """Rows from a bounded head and tail of the decompressed log; None without a decoder."""
    head, tail = bytearray(), bytearray()
    total = 0

    def sink(chunk: bytes) -> None:
        nonlocal total
        total += len(chunk)
        room = HEAD_BYTES - len(head)
        head.extend(chunk[:room])
        tail.extend(chunk[room:])
        del tail[: max(0, len(tail) - TAIL_BYTES - 1)]

    if not _decode(path, sink):
        return None
    if total <= HEAD_BYTES + TAIL_BYTES:
        return _parse_lines(bytes(head + tail))
    # Drop incomplete boundaries, not just malformed JSON fragments.
    return [
        *_parse_lines(bytes(head).rsplit(b"\n", 1)[0] + b"\n"),
        {"type": READ_GAP},
        *_parse_lines(bytes(tail).split(b"\n", 1)[-1]),
    ]


def home_roots(home: Path) -> list[tuple[str, Path]]:
    return [("DeepSeek Harness", home / ".dsh")]


def is_root(root: Path) -> bool:
    return (root / TREE).is_dir()


def _archived(root: Path) -> set[str]:
    data = small_json(root / WORKSPACES, MAX_STORAGE_FILE) or {}
    state = data.get("global")
    ids = state.get("archivedSessionIds") if isinstance(state, dict) else None
    return {x for x in ids if isinstance(x, str)} if isinstance(ids, list) else set()


def _session_dirs(root: Path, diagnostics: dict | None = None) -> Iterator[Path]:
    """Session folders under ``sessions/<project>/``, within the shared entry budget."""
    tree = root / TREE
    if tree.is_symlink():
        raise OSError("Session directory is a link")
    visited = 0
    with os.scandir(tree) as projects:
        folders = [Path(e.path) for e in projects if e.is_dir(follow_symlinks=False)]
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        if diagnostics is not None:
                            diagnostics["entry_budget_hit"] = True
                        return
                    if entry.is_symlink():
                        if diagnostics is not None:
                            diagnostics["symlinks_skipped"] += 1
                    elif SESSION_DIR.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                        yield Path(entry.path)
        except OSError:
            if diagnostics is not None:
                diagnostics["unreadable_directories"] += 1


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    found = decoder()
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
        "transcript_trees": [TREE],
        "tree_count": 1,
        "max_age_days": max_age_days,
        "aged_out": 0,
        "decoder": found[0] if found else "unavailable",
        "text_unavailable": found is None,
    }
    hidden = set() if archived else _archived(root)
    sessions = []
    for folder in _session_dirs(root, diagnostics):
        if folder.name in hidden:
            continue
        path = folder / LOG
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            diagnostics["symlinks_skipped"] += stat.S_ISLNK(info.st_mode)
            continue
        if cutoff_ns is not None and info.st_mtime_ns < cutoff_ns:
            diagnostics["aged_out"] += 1
            continue
        sessions.append(Session(str(path), path, info.st_size, info.st_mtime_ns, info.st_mtime))
    selected = sorted(sessions, key=lambda s: s.mtime_ns, reverse=True)[:limit]
    limited = bool(
        diagnostics["entry_budget_hit"]
        or diagnostics["unreadable_directories"]
        or diagnostics["symlinks_skipped"]
    )
    diagnostics.update(
        files_seen=len(sessions),
        selected_files=len(selected),
        discovery_limited=limited,
        truncated=limited or len(sessions) > len(selected),
    )
    return selected, diagnostics


def _time(value: object) -> str | None:
    """Epoch milliseconds (DSH's own format) or an ISO string, as UTC ISO."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and 0 < value < 1e14:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    return utc_time(value)


def _data(row: dict) -> dict:
    data = row.get("data")
    return data if isinstance(data, dict) else {}


def _request(row: dict, limit: int = 160) -> str:
    data = _data(row)
    source = data.get("source")
    if row.get("type") != "user/message" or not isinstance(source, dict):
        return ""
    return request_text(data.get("content"), limit) if source.get("kind") in REQUEST_SOURCES else ""


def _content(row: dict) -> list:
    message = _data(row).get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _short(native: str) -> str:
    return native.removeprefix("session-")[:8]


def parse(profile: SourceProfile, rows: list[dict], mtime: float) -> dict | None:
    header = rows[0] if rows and rows[0].get("type") == "session" else None
    native = native_id(header.get("id")) if header else ""
    if not header or not native:
        return None
    started = _time(header.get("createdAt"))
    cwd = valid_cwd(header.get("cwd"))
    out = observation(profile, "DeepSeek", native, safe(Path(cwd).name, 80), started)
    out["name"] = f"DeepSeek {_short(native)}"
    parent = native_id(header.get("parentSession"))
    agent = header.get("origin") == "subagent" and parent and parent != native
    if agent:
        out["parent_run_id"] = ns(profile, parent)
        out["name"] = f"DeepSeek subagent {_short(native)}"
    pending: set[str] = set()
    last_reply = title = ""
    prompt_seen = False
    for row in rows[1:]:
        stamp = _time(row.get("time"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind, data = row.get("type"), _data(row)
        if kind == READ_GAP:
            # The header's request and reply predate the unread middle; the scan searches back.
            # A subagent keeps its first prompt, which is the task its parent gave it.
            last_reply = ""
            if not agent:
                out["task"] = TEXT_HIDDEN
            continue
        state, activity, evidence = None, None, str(kind)
        if kind == "turn/start":
            state, activity = "working", "Turn started"
        elif kind == "turn/end":
            # Every end reason (completed, blocked, interrupted, aborted, …) leaves it idle.
            reason = data.get("reason")
            end = safe(reason.get("kind"), 40) if isinstance(reason, dict) else ""
            pending.clear()
            state, activity = "idle", last_reply or "Turn ended · session may resume"
            evidence = f"turn/end {end}".strip()
        elif kind == "approval/asked":
            pending.add(str(data.get("id")))
            state, activity = "waiting_user", "Approval requested"
        elif kind == "approval/decided":
            pending.discard(str(data.get("id")))
            state, activity = "working", last_reply or "Approval decided"
        elif kind == "user/message":
            request = _request(row, 400 if agent else 160) if profile.include_text else ""
            if request and not (agent and prompt_seen):
                out["task"], prompt_seen, last_reply = request, True, ""
            state, activity = "working", "User turn recorded"
        elif kind == "assistant/message":
            content = _content(row)
            if profile.include_text:
                last_reply = reply_text(content) or last_reply
            if any(isinstance(x, dict) and x.get("type") == "tool-call" for x in content):
                state, activity = "waiting_tool", last_reply or "Tool call recorded"
            else:
                state, activity = "working", last_reply or "Model activity recorded"
        elif kind == "tool/call":
            state, activity = "waiting_tool", last_reply or "Tool call recorded"
        elif kind == "tool/result":
            state, activity = "working", last_reply or "Tool result recorded"
        elif kind == "session/title":
            title = safe(data.get("title"), 120) or title
        if state:
            if pending and state != "idle":
                state, activity = "waiting_user", "Approval requested"
            out.update(
                state=state,
                activity=activity,
                evidence=f"DeepSeek Harness log: {evidence}; inferred session state",
            )
    if profile.include_text and title:
        out["name"] = title
    out["last_seen"] = out["last_seen"] or mtime_iso(mtime)
    return out


def observe(profile: SourceProfile, session: Session) -> dict | None:
    rows = records(session.path)
    if rows is not None:
        return parse(profile, rows, session.mtime)
    # Without a decoder the session is still listed, so liveness and focus work.
    native = session.path.parent.name
    if not SESSION_DIR.fullmatch(native):
        return None
    out = observation(profile, "DeepSeek", native, "", None)
    out.update(
        name=f"DeepSeek {_short(native)}",
        activity=NO_DECODER,
        evidence="DeepSeek Harness log not decoded; no zstd decoder available",
        last_seen=mtime_iso(session.mtime),
    )
    return out


def latest_request(session: Session) -> str:
    found, carry = "", b""

    def sink(chunk: bytes) -> None:
        nonlocal found, carry
        lines = (carry + chunk).split(b"\n")
        carry = lines.pop()
        for line in lines:
            if b'"user/message"' not in line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            found = (_request(row) if isinstance(row, dict) else "") or found

    try:
        _decode(session.path, sink)
    except OSError:
        return ""
    return found


def titles(root: Path) -> dict[str, str]:
    """Session titles from the app's projection cache, re-read only when a file changes."""
    folder = root / PROJCACHE
    try:
        with os.scandir(folder) as entries:
            names = [
                e.name
                for e in entries
                if e.name.endswith(".json")
                and SESSION_DIR.fullmatch(e.name.removesuffix(".json"))
                and e.is_file(follow_symlinks=False)
            ][:MAX_ENTRIES]
    except OSError:
        return {}
    found: dict[str, str] = {}
    for name in names:
        path = folder / name
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        signature = (info.st_size, info.st_mtime_ns)
        cached = _TITLES.get(str(path))
        if cached and cached[0] == signature:
            title = cached[1]
        else:
            data = small_json(path, MAX_STORAGE_FILE) or {}
            value: object = data
            for key in ("record", "rows", "title", "val"):
                value = value.get(key) if isinstance(value, dict) else None
            title = safe(value, 120)
            _TITLES[str(path)] = (signature, title)
        if title:
            found[name.removesuffix(".json")] = title
    return found


def _holders(locks: list[str]) -> list[tuple[int, str, str]] | None:
    """(pid, command, lock path) for each process holding a lock; None when lsof fails."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    result = processes.run([lsof, "-F", "pcn", "--", *locks])
    if result is None or result[0] not in {0, 1}:
        return None
    held, pid, command = [], 0, ""
    for line in result[1].splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid, command = int(line[1:]), ""
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and pid:
            held.append((pid, command, line[1:]))
    return held


def _is_dsh(pid: int, command: str) -> bool:
    # The app, its helpers and its CLI all run the DeepSeek Harness executable.
    if "deepseek" in command.lower():
        return True
    try:
        process = processes.process(pid)
    except OSError:
        return False
    return process is not None and "deepseek harness" in process.command.lower()


def live_sessions(root: Path) -> frozenset[str] | None:
    """The writer holds ``session.lock`` with flock while a session is open for writing."""
    try:
        locks = [
            str(folder / LOCK)
            for folder in _session_dirs(root)
            if (folder / LOCK).is_file() and not (folder / LOCK).is_symlink()
        ]
    except OSError:
        return None
    if not locks:
        return frozenset()
    held = _holders(locks)
    if held is None:
        return None
    return frozenset(
        Path(path).parent.name
        for pid, command, path in held
        if pid != os.getpid() and Path(path).name == LOCK and _is_dsh(pid, command)
    )


def session_pid(root: Path, session_id: str) -> tuple[bool, int | None]:
    if not SESSION_DIR.fullmatch(session_id):
        return False, None
    try:
        folder = next((f for f in _session_dirs(root) if f.name == session_id), None)
    except OSError:
        return False, None
    lock = folder / LOCK if folder else None
    if lock is None or lock.is_symlink() or not lock.is_file():
        return False, None
    held = _holders([str(lock)])
    if held is None:
        return False, None
    pids = sorted({pid for pid, _, _ in held if pid != os.getpid()})
    if not pids:
        return True, None
    named = [pid for pid, command, _ in held if _is_dsh(pid, command)]
    # Another holder means liveness is unclear: neither focus nor resume.
    return (True, named[0]) if named else (False, None)


def goal_context(rows: list[dict]) -> Goal:
    messages: list[tuple[str, object, str]] = []
    title = ""
    for row in rows[1:]:
        stamp = _time(row.get("time")) or ""
        if row.get("type") == "session/title":
            title = redact(_data(row).get("title"), 120) or title
        elif _request(row, 20_000):
            messages.append(("user", _data(row).get("content"), stamp))
        elif row.get("type") == "assistant/message":
            # Only prose: reasoning and tool-call blocks never reach a provider.
            prose = [x for x in _content(row) if isinstance(x, dict) and x.get("type") == "text"]
            if prose:
                messages.append(("assistant", prose, stamp))
    return Goal(title, "", "", tuple(messages))


def goal(session: Session) -> Goal:
    rows = records(session.path)
    return goal_context(rows) if rows else Goal("", "", "", ())


HARNESS = Harness(
    kind="dsh",
    name="DeepSeek Harness",
    env="DSH_HOME",
    home_roots=home_roots,
    home_label=lambda name: None,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=titles,
    live_sessions=live_sessions,
    owner=lambda native: native,
    session_pid=session_pid,
    resume=lambda root, session_id: None,
    goal=goal,
)
