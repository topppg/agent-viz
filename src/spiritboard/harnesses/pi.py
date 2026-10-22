"""Pi coding agent: tree-shaped JSONL sessions under ``sessions/--<cwd>--/``."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import processes
from ..transcripts import (
    READ_FLAGS,
    Session,
    last_request,
    mtime_iso,
    native_id,
    observation,
    records,
    redact,
    reply_text,
    request_text,
    safe,
    utc_time,
    valid_cwd,
    walk_jsonl,
)
from .harness import Goal, Harness

if TYPE_CHECKING:
    from ..sources import SourceProfile

SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
SESSION_FILE = re.compile(r"\d{4}-\d\d-\d\dT[\d-]+Z_([A-Za-z0-9._-]{1,160})\.jsonl")
HEADER_BYTES = 64 * 1024
MAX_ENTRIES = 50_000
MAX_HEADERS = 200


def home_roots(home: Path) -> list[tuple[str, Path]]:
    candidates = [("Pi · default", home / ".pi" / "agent")]
    value = os.environ.get(SESSION_DIR_ENV)
    # Like the harness env selectors, the session-dir override applies only to the real home.
    if value and home == Path.home():
        candidates.append(("Pi · configured sessions", Path(value).expanduser()))
    return candidates


def _flat(root: Path) -> bool:
    """A session directory chosen by PI_CODING_AGENT_SESSION_DIR holds session files directly."""
    try:
        with os.scandir(root) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_ENTRIES:
                    break
                if SESSION_FILE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    return True
    except OSError:
        pass
    return False


def sessions_dir(root: Path) -> Path | None:
    if (root / "sessions").is_dir():
        return root / "sessions"
    return root if _flat(root) else None


def is_root(root: Path) -> bool:
    return sessions_dir(root) is not None


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    folder = sessions_dir(root)
    if folder is None:
        raise OSError("Transcript directory is unavailable")
    return walk_jsonl([folder], limit, max_age_days)


def _entries(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("type") != "session" and isinstance(r.get("id"), str)]


def branch(rows: list[dict]) -> list[dict]:
    """The active branch: from the last entry back through ``parentId``, oldest first.

    Stops where a parent lies outside the bounded read.
    """
    entries = _entries(rows)
    if not entries:
        return []
    by_id = {e["id"]: e for e in entries}
    path, seen, current = [], set(), entries[-1]
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        path.append(current)
        parent = current.get("parentId")
        current = by_id.get(parent) if isinstance(parent, str) else None
    return path[::-1]


def _message(entry: dict) -> dict:
    message = entry.get("message") if entry.get("type") == "message" else None
    return message if isinstance(message, dict) else {}


def _reply(message: dict) -> str:
    # Only text blocks: thinking and tool calls never become a reply.
    return reply_text(message.get("content")) if message.get("role") == "assistant" else ""


def _title(rows: list[dict], clean: Callable[[object, int], str]) -> str:
    title = ""
    for row in rows:
        if row.get("type") == "session_info":
            # An empty name clears the title, as in Pi itself.
            title = clean(row.get("name"), 120)
    return title


def parse(profile: SourceProfile, rows: list[dict], mtime: float) -> dict | None:
    header = rows[0] if rows and rows[0].get("type") == "session" else None
    native = native_id(header.get("id")) if header else ""
    if not header or not native:
        return None
    cwd = valid_cwd(header.get("cwd"))
    started = utc_time(header.get("timestamp"))
    out = observation(profile, "Pi", native, safe(Path(cwd).name, 80) if cwd else "", started)
    for row in rows:
        stamp = utc_time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
    reply = ""
    state = activity = evidence = None
    for entry in branch(rows):
        message = _message(entry)
        role = message.get("role")
        if role == "user":
            request = request_text(message.get("content")) if profile.include_text else ""
            if request:
                out["task"] = request
            reply = ""
            state, activity, evidence = "working", "User turn recorded", "user message"
        elif role == "toolResult":
            state, evidence = "working", "toolResult"
            activity = reply or "Tool result recorded"
        elif role == "assistant":
            if profile.include_text:
                reply = _reply(message) or reply
            stop = message.get("stopReason")
            if stop == "toolUse":
                state, evidence = "waiting_tool", "assistant toolUse"
                activity = reply or "Tool call recorded"
            elif stop in {"pending", "deferred"}:
                state, evidence = "working", f"assistant {stop}"
                activity = reply or "Model activity recorded"
            else:
                # error and aborted end the turn too; Pi stays open for the next prompt.
                state, evidence = "idle", f"assistant {safe(stop, 20) or 'message'}"
                activity = reply or "Turn ended · session may resume"
    if state:
        out.update(
            state=state,
            activity=activity,
            evidence=f"Pi transcript: {evidence}; inferred session state",
        )
    if profile.include_text:
        first = next(
            (
                request
                for row in rows
                if _message(row).get("role") == "user"
                and (request := request_text(_message(row).get("content"), 120))
            ),
            "",
        )
        named = _title(rows, safe)
        out["name"] = named or first or out["name"]
        if named:
            out["title_source"] = "user"
    out["last_seen"] = out["last_seen"] or mtime_iso(mtime)
    return out


def observe(profile: SourceProfile, session: Session) -> dict | None:
    return parse(profile, records(session.path), session.mtime)


def latest_request(session: Session) -> str:
    """Scans backwards along the active branch only; abandoned branches are skipped."""
    expected: list[str | None] = [None]

    def on_branch(row: dict[str, Any]) -> str:
        entry = row.get("id")
        if row.get("type") == "session" or not isinstance(entry, str) or expected[0] == "":
            return ""
        if expected[0] is None:
            expected[0] = entry
        if entry != expected[0]:
            return ""
        parent = row.get("parentId")
        expected[0] = parent if isinstance(parent, str) else ""
        message = _message(row)
        return request_text(message.get("content")) if message.get("role") == "user" else ""

    return last_request(session.path, b'"parentId":', on_branch)


def header(path: Path) -> dict[str, Any] | None:
    try:
        with os.fdopen(os.open(path, READ_FLAGS), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            line = stream.readline(HEADER_BYTES)
        value = json.loads(line)
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) and value.get("type") == "session" else None


def _session_files(folder: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as entries:
            return [
                e
                for e in entries
                if SESSION_FILE.fullmatch(e.name) and e.is_file(follow_symlinks=False)
            ]
    except OSError:
        return []


def encoded(cwd: str) -> str:
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"


def newest(root: Path, cwd: str) -> str:
    """Native ID of the most recently modified session recorded for ``cwd``."""
    folder = sessions_dir(root)
    if folder is None:
        return ""
    files = _session_files(folder / encoded(cwd)) or _session_files(folder)

    def modified(entry: os.DirEntry[str]) -> int:
        try:
            return entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            return 0

    for entry in sorted(files, key=modified, reverse=True)[:MAX_HEADERS]:
        meta = header(Path(entry.path))
        if meta and meta.get("cwd") == cwd and native_id(meta.get("id")):
            return meta["id"]
    return ""


def pi_processes() -> dict[int, str] | None:
    """pid -> cwd of processes titled ``pi``; None when ps or lsof cannot answer."""
    result = processes.run(["ps", "-A", "-o", "pid=", "-o", "args="])
    if result is None or result[0] != 0:
        return None
    pids = []
    for line in result[1].splitlines():
        pid, _, args = line.strip().partition(" ")
        command = args.split(maxsplit=1)[0] if args.strip() else ""
        if pid.isdigit() and int(pid) != os.getpid() and Path(command).name == "pi":
            pids.append(int(pid))
    if not pids:
        return {}
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    result = processes.run([lsof, "-a", "-d", "cwd", "-F", "pn", "-p", ",".join(map(str, pids))])
    if result is None or result[0] not in {0, 1}:
        return None
    found, pid = {}, 0
    for line in result[1].splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid in pids:
            cwd = valid_cwd(line[1:])
            if cwd:
                found[pid] = cwd
    return found


def live_sessions(root: Path) -> frozenset[str] | None:
    """Pi holds no lock: a ``pi`` process makes the newest session in its cwd live."""
    running = pi_processes()
    if running is None:
        return None
    live = {newest(root, cwd) for cwd in set(running.values())}
    return frozenset(live - {""})


def find_session(root: Path, native: str) -> Path | None:
    folder = sessions_dir(root)
    if folder is None:
        return None
    suffix = f"_{native}.jsonl"
    for entry in _session_files(folder):
        if entry.name.endswith(suffix):
            return Path(entry.path)
    try:
        with os.scandir(folder) as entries:
            folders = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for child in folders[:MAX_ENTRIES]:
        for entry in _session_files(child):
            if entry.name.endswith(suffix):
                return Path(entry.path)
    return None


def _cwd(path: Path) -> str:
    meta = header(path)
    return valid_cwd(meta.get("cwd")) if meta else ""


def session_pid(root: Path, native: str) -> tuple[bool, int | None]:
    path = find_session(root, native)
    cwd = _cwd(path) if path else ""
    if not cwd:
        return False, None
    running = pi_processes()
    if running is None:
        return False, None
    pids = [pid for pid, folder in running.items() if folder == cwd]
    if not pids or newest(root, cwd) != native:
        return True, None
    # Several pi processes in one directory: which one owns the session cannot be told.
    return (True, pids[0]) if len(pids) == 1 else (False, None)


def resume_command(path: Path, cwd: str) -> str:
    command = f"pi --session {shlex.quote(str(path))}"
    return f"cd {shlex.quote(cwd)} && {command}" if cwd else command


def resume(root: Path, native: str) -> str | None:
    path = find_session(root, native)
    return resume_command(path, _cwd(path)) if path else None


def goal_context(rows: list[dict]) -> Goal:
    messages: list[tuple[str, object, str]] = []
    recap = recap_at = ""
    for entry in branch(rows):
        stamp = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else ""
        if entry.get("type") == "compaction" and isinstance(entry.get("summary"), str):
            recap, recap_at = redact(entry["summary"], 600), stamp
        message = _message(entry)
        if message.get("role") == "user":
            messages.append(("user", message.get("content"), stamp))
        elif message.get("role") == "assistant":
            content = message.get("content")
            content = content if isinstance(content, list) else []
            blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if blocks:
                messages.append(("assistant", blocks, stamp))
    return Goal(_title(rows, redact), recap, recap_at, tuple(messages))


def goal(session: Session) -> Goal:
    return goal_context(records(session.path))


HARNESS = Harness(
    kind="pi",
    name="Pi",
    env="PI_CODING_AGENT_DIR",
    home_roots=home_roots,
    home_label=lambda name: None,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=None,
    live_sessions=live_sessions,
    owner=lambda native: native,
    session_pid=session_pid,
    resume=resume,
    goal=goal,
)
