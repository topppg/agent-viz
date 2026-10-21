"""Codex CLI: rollout transcripts under ``sessions/`` and ``archived_sessions/``."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import processes
from ..transcripts import (
    READ_FLAGS,
    READ_GAP,
    TEXT_HIDDEN,
    Session,
    existing_trees,
    last_request,
    mtime_iso,
    native_id,
    ns,
    observation,
    records,
    redact,
    request_text,
    safe,
    text,
    utc_time,
    valid_cwd,
    walk_jsonl,
)
from .harness import Goal, Harness, shell_resume

if TYPE_CHECKING:
    from ..sources import SourceProfile

TREES = ("sessions", "archived_sessions")
MAX_WALK = 50_000
LOCK_FILE = re.compile(r"([0-9A-Za-z][0-9A-Za-z-]{7,79})\.lock")
_TITLES: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def home_roots(home: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    switch = home / ".codex-switch" / "profiles"
    try:
        for child in sorted(switch.iterdir()):
            if child.is_dir():
                candidates.append((f"Codex · {child.name}", child))
    except OSError:
        pass
    candidates.append(("Codex · default", home / ".codex"))
    return candidates


def home_label(name: str) -> str | None:
    return "Codex · " + name.removeprefix(".codex-") if name.startswith(".codex-") else None


def is_root(root: Path) -> bool:
    return any((root / tree).is_dir() for tree in TREES)


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    return walk_jsonl(existing_trees(root, TREES if archived else TREES[:1]), limit, max_age_days)


def parse(profile: SourceProfile, rows: list[dict], mtime: float) -> dict | None:
    meta = next(
        (
            x.get("payload", {})
            for x in rows
            if x.get("type") == "session_meta" and isinstance(x.get("payload"), dict)
        ),
        None,
    )
    if not meta:
        return None
    native = native_id(meta.get("id")) or native_id(meta.get("session_id"))
    if not native:
        return None
    started = utc_time(meta.get("timestamp")) or utc_time(rows[0].get("timestamp"))
    cwd = meta.get("cwd", "")
    project = safe(Path(cwd).name, 80) if isinstance(cwd, str) else ""
    source = meta.get("source", {})
    spawn = (
        source.get("subagent", {}).get("thread_spawn", {})
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict)
        else {}
    )
    parent = native_id(meta.get("parent_thread_id")) or (
        native_id(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""
    )
    out = observation(profile, "Codex", native, project, started)
    if parent and parent != native:
        out["parent_run_id"] = ns(profile, parent)
    last_state_time = None
    last_reply = ""
    for row in rows:
        stamp = utc_time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind = row.get("type")
        if kind == READ_GAP:
            # The header's request and reply predate the unread middle; the scan searches back.
            out["task"], last_reply = TEXT_HIDDEN, ""
            continue
        p = row.get("payload", {})
        if not isinstance(p, dict):
            continue
        state, activity, evidence = None, None, None
        if kind == "event_msg":
            event = p.get("type")
            if event in {"task_started", "turn_started", "user_message"}:
                state, activity, evidence = "working", "Turn started", str(event)
            elif event in {"task_complete", "turn_complete", "turn_aborted"}:
                # An idle session reads best as its last reply, when text is allowed.
                reply = text(p.get("last_agent_message")) if profile.include_text else ""
                reply = reply or last_reply
                state, evidence = "idle", str(event)
                activity = reply or "Turn ended · session may resume"
            if profile.include_text and event == "user_message":
                request = request_text(p.get("message"))
                if request:
                    out["task"], last_reply = request, ""
        elif kind == "response_item":
            event = p.get("type")
            if event in {"function_call", "custom_tool_call", "local_shell_call"}:
                state, evidence = "waiting_tool", str(event)
                activity = last_reply or "Tool call recorded"
            elif event in {
                "function_call_output",
                "custom_tool_call_output",
                "local_shell_call_output",
            }:
                state, evidence = "working", str(event)
                activity = last_reply or "Tool result recorded"
            elif event == "message" and p.get("role") == "user":
                request = request_text(p.get("content")) if profile.include_text else ""
                if request:
                    # A new request starts a new turn; the previous reply no longer describes it.
                    out["task"], last_reply = request, ""
                    state, activity, evidence = "working", "Turn started", "user message"
            elif event in {"reasoning", "agent_message"} or (
                event == "message" and p.get("role") == "assistant"
            ):
                state, evidence = "working", str(event)
                if profile.include_text and event == "message":
                    # Only the agent's own prose: agent_message items are routed between agents
                    # and wrapped system notices are not replies.
                    reply = text(p.get("content"))
                    last_reply = reply if reply and not reply.startswith("<") else last_reply
                activity = last_reply or "Model activity recorded"
        if state and (stamp is None or last_state_time is None or stamp >= last_state_time):
            out.update(
                state=state,
                activity=activity,
                evidence=f"Codex transcript: {evidence}; inferred session state",
            )
            last_state_time = stamp or last_state_time
    out["last_seen"] = out["last_seen"] or mtime_iso(mtime)
    return out


def observe(profile: SourceProfile, session: Session) -> dict | None:
    return parse(profile, records(session.path), session.mtime)


def row_request(row: dict[str, Any]) -> str:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return ""
    if row.get("type") == "response_item" and payload.get("type") == "message":
        return request_text(payload.get("content")) if payload.get("role") == "user" else ""
    if row.get("type") == "event_msg" and payload.get("type") == "user_message":
        return request_text(payload.get("message"))
    return ""


def latest_request(session: Session) -> str:
    return last_request(session.path, b'"role":"user"', row_request)


def titles(root: Path) -> dict[str, str]:
    """Codex thread names from ``session_index.jsonl``; bounded, cached by size and mtime."""
    path = root / "session_index.jsonl"
    try:
        with os.fdopen(os.open(path, READ_FLAGS), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return {}
            signature = (info.st_size, info.st_mtime_ns)
            cached = _TITLES.get(str(path))
            if cached and cached[0] == signature:
                return cached[1]
            stream.seek(max(0, info.st_size - 2_000_000))
            data = stream.read(2_000_000)
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            name = safe(row.get("thread_name"), 120)
            if name:
                found[row["id"]] = name
    _TITLES[str(path)] = (signature, found)
    return found


def live_sessions(root: Path) -> frozenset[str] | None:
    """Codex holds ``thread-writer-locks/<thread>.lock`` open while a session is live."""
    try:
        with os.scandir(root / "thread-writer-locks") as entries:
            locks = [e.path for e in entries if LOCK_FILE.fullmatch(e.name) and not e.is_symlink()]
    except OSError:
        return None
    lsof = shutil.which("lsof")
    if not locks:
        return frozenset()
    if not lsof:
        return None
    result = processes.run([lsof, "-F", "pcn", "--", *locks])
    if result is None or result[0] not in {0, 1}:
        return None
    live, command = set(), ""
    for line in result[1].splitlines():
        if line.startswith("p"):
            command = ""
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and "codex" in command.lower():
            match = LOCK_FILE.fullmatch(Path(line[1:]).name)
            if match:
                live.add(match[1])
    return frozenset(live)


def find_rollout(root: Path, native: str) -> Path | None:
    pattern = re.compile(rf"[-_]{re.escape(native)}(?:_[0-9A-Za-z-]+)?\.jsonl$")
    for tree in TREES:
        pending, visited = [(root / tree, 0)], 0
        while pending and visited < MAX_WALK:
            folder, depth = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        visited += 1
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False) and depth < 5:
                            pending.append((Path(entry.path), depth + 1))
                        elif pattern.search(entry.name):
                            return Path(entry.path)
            except OSError:
                continue
    return None


def rollout_pid(root: Path, path: Path) -> tuple[bool, int | None]:
    """(known, pid) of the process holding a rollout transcript open."""
    if path.is_relative_to(root / "archived_sessions"):
        return True, None
    lsof = shutil.which("lsof")
    if not lsof or not path.is_file():
        return False, None
    result = processes.run([lsof, "-t", "--", str(path)])
    if result is None or result[0] not in {0, 1}:
        return False, None
    candidates = [int(x) for x in result[1].split() if x.isdigit() and int(x) != os.getpid()]
    if result[0] == 1 and not candidates:
        return True, None
    found = [p for p in map(processes.process, candidates) if p]
    named = [p for p in found if "codex" in p.command.lower()]
    # Another holder (an editor, tail) means liveness is unclear: neither focus nor resume.
    return (True, named[0].pid) if named else (False, None)


def session_pid(root: Path, native: str) -> tuple[bool, int | None]:
    path = find_rollout(root, native)
    return rollout_pid(root, path) if path else (False, None)


def _cwd(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        rows = records(path)
    except (OSError, ValueError):
        return ""
    for row in rows:
        payload = row.get("payload") if row.get("type") == "session_meta" else None
        cwd = valid_cwd(payload.get("cwd") if isinstance(payload, dict) else None)
        if cwd:
            return cwd
    return ""


def resume_command(root: Path, native: str, cwd: str) -> str:
    return shell_resume("CODEX_HOME", root, f"codex resume {native}", cwd)


def resume(root: Path, native: str) -> str:
    return resume_command(root, native, _cwd(find_rollout(root, native)))


def goal_context(rows: list[dict]) -> Goal:
    messages: list[tuple[str, object, str]] = []
    recap = recap_at = ""
    for row in rows:
        stamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else ""
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "compacted" and isinstance(payload.get("message"), str):
            recap, recap_at = redact(payload["message"], 600), stamp
        elif row.get("type") == "event_msg" and payload.get("type") in {
            "user_message",
            "agent_message",
        }:
            role = "user" if payload["type"] == "user_message" else "assistant"
            messages.append((role, payload.get("message"), stamp))
        elif row.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if payload.get("channel") != "analysis" and role in {"user", "assistant"}:
                messages.append((role, payload.get("content"), stamp))
    return Goal("", recap, recap_at, tuple(messages))


def goal(session: Session) -> Goal:
    return goal_context(records(session.path))


HARNESS = Harness(
    kind="codex",
    name="Codex",
    env="CODEX_HOME",
    home_roots=home_roots,
    home_label=home_label,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=titles,
    live_sessions=live_sessions,
    owner=lambda native: native,
    session_pid=session_pid,
    resume=resume,
    goal=goal,
)
