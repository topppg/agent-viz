"""Claude Code: ``projects/`` transcripts and ``sessions/<pid>.json`` process records."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import processes
from ..transcripts import (
    READ_FLAGS,
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
    small_json,
    text,
    utc_time,
    valid_cwd,
    walk_jsonl,
)
from .harness import SESSION_ID, Goal, Harness, shell_resume

if TYPE_CHECKING:
    from ..sources import SourceProfile

TREES = ("projects",)
PID_FILE = re.compile(r"\d{1,10}\.json")
START_LINE = re.compile(r"\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4})\s*")
RECAP_SUFFIX = re.compile(r"\s*\(disable recaps in /config\)\s*$", re.I)


def home_label(name: str) -> str | None:
    if name == ".claude":
        return "Claude · default"
    if name.startswith(".claude-"):
        return "Claude · " + name.removeprefix(".claude").lstrip("-")
    return None


def is_root(root: Path) -> bool:
    return any((root / tree).is_dir() for tree in TREES)


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    return walk_jsonl(existing_trees(root, TREES), limit, max_age_days)


def parse(profile: SourceProfile, rows: list[dict], mtime: float) -> dict | None:
    session = next(
        (native_id(x.get("sessionId")) for x in rows if native_id(x.get("sessionId"))), ""
    )
    if not session:
        return None
    side = next(
        (x for x in rows if x.get("isSidechain") is True and native_id(x.get("agentId"))), None
    )
    # parentUuid is a MESSAGE link, never an agent-parent relationship.
    agent = native_id(side.get("agentId")) if side else ""
    native = f"{session}:agent:{agent}" if agent else session
    started = next(
        (utc_time(x.get("timestamp")) for x in rows if utc_time(x.get("timestamp"))), None
    )
    cwd = next((x["cwd"] for x in rows if isinstance(x.get("cwd"), str)), "")
    out = observation(profile, "Claude", native, safe(Path(cwd).name, 80), started)
    if agent:
        out["parent_run_id"] = ns(profile, session)
        out["name"] = f"Claude subagent {agent[:8]}"
    last_state_time = None
    prompt_seen = False
    last_reply = ""
    custom_title = ai_title = ""
    for row in rows:
        stamp = utc_time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind, message = row.get("type"), row.get("message", {})
        state, activity, evidence = None, None, None
        if kind == "custom-title":
            custom_title = safe(row.get("customTitle"), 120) or custom_title
        elif kind == "ai-title":
            ai_title = safe(row.get("aiTitle"), 120) or ai_title
        if kind == "user" and isinstance(message, dict):
            content = message.get("content", [])
            result = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_result" for x in content
            )
            state, evidence = "working", "user/tool_result" if result else "user"
            activity = (last_reply or "Tool result recorded") if result else "User turn recorded"
            # Skill bodies, compaction summaries and command echoes are not requests.
            if (
                profile.include_text
                and (not result or (agent and "</fork-boilerplate>" in text(content, 20_000)))
                and not (row.get("isMeta") or row.get("isCompactSummary"))
            ):
                # A subagent's goal is the prompt its parent gave it, so the first one wins.
                request = request_text(content, 400 if agent else 160)
                if request and not (agent and prompt_seen):
                    out["task"], prompt_seen = request, True
                    if not result:
                        last_reply = ""
        elif kind == "assistant" and isinstance(message, dict):
            stop = message.get("stop_reason")
            content = message.get("content", [])
            tool = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_use" for x in content
            )
            if profile.include_text:
                last_reply = text(content) or last_reply
            if stop in {"end_turn", "stop_sequence", "max_tokens"}:
                state, evidence = "idle", "assistant stop_reason"
                activity = last_reply or "Turn ended · session may resume"
            elif tool or stop == "tool_use":
                state, evidence = "waiting_tool", "assistant tool_use"
                activity = last_reply or "Tool call recorded"
            else:
                state, evidence = "working", "assistant"
                activity = last_reply or "Model activity recorded"
        elif kind == "system" and row.get("subtype") == "turn_duration":
            state, evidence = "idle", "turn_duration"
            activity = last_reply or "Turn ended · session may resume"
        if state and (stamp is None or last_state_time is None or stamp >= last_state_time):
            out.update(
                state=state,
                activity=activity,
                evidence=f"Claude transcript: {evidence}; inferred session state",
            )
            last_state_time = stamp or last_state_time
    # A /rename beats Claude's generated title; subagents keep their parent-given name.
    if profile.include_text and not agent and (custom_title or ai_title):
        out["name"] = custom_title or ai_title
    out["last_seen"] = out["last_seen"] or mtime_iso(mtime)
    return out


def subagent_description(path: Path) -> str:
    """The short description the parent gave a subagent, from agent-<id>.meta.json."""
    meta = path.with_name(path.stem + ".meta.json")
    try:
        fd = os.open(meta, READ_FLAGS)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return ""
            data = stream.read(65_537)
        if len(data) > 65_536:
            return ""
        value = json.loads(data)
    except (OSError, ValueError):
        return ""
    return safe(value.get("description"), 120) if isinstance(value, dict) else ""


def observe(profile: SourceProfile, session: Session) -> dict | None:
    value = parse(profile, records(session.path), session.mtime)
    if value and value.get("parent_run_id") and profile.include_text:
        value["name"] = subagent_description(session.path) or value["name"]
    return value


def row_request(row: dict[str, Any]) -> str:
    message = row.get("message")
    if row.get("type") != "user" or not isinstance(message, dict):
        return ""
    if row.get("isMeta") or row.get("isCompactSummary") or row.get("isSidechain"):
        return ""
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(x, dict) and x.get("type") == "tool_result" for x in content
    ):
        return ""
    return request_text(content)


def latest_request(session: Session) -> str:
    return last_request(session.path, b'"type":"user"', row_request)


def session_pid(root: Path, session_id: str) -> tuple[bool, int | None]:
    """(known, pid) from ``sessions/<pid>.json``; other files there are never opened."""
    try:
        with os.scandir(root / "sessions") as entries:
            names = [entry.name for entry in entries if PID_FILE.fullmatch(entry.name)]
    except OSError:
        return False, None
    known = True
    for name in sorted(names):
        path = root / "sessions" / name
        if path.is_symlink():
            known = False
            continue
        data = small_json(path)
        pid = int(name.removesuffix(".json"))
        session = data.get("sessionId") if data else None
        if (
            not data
            or data.get("pid") != pid
            or not isinstance(session, str)
            or not SESSION_ID.fullmatch(session)
        ):
            known = False
            continue
        if session != session_id:
            continue
        process = processes.process(pid)
        if process is None:
            continue
        recorded = processes.process_start(data.get("procStart"))
        observed = processes.process_start(process.start)
        if recorded is None or observed is None:
            known = False
            continue
        if recorded != observed:
            continue
        return True, pid
    return known, None


def live_sessions(root: Path) -> frozenset[str] | None:
    try:
        with os.scandir(root / "sessions") as entries:
            names = []
            for entry in entries:
                if PID_FILE.fullmatch(entry.name):
                    if entry.is_symlink():
                        return None
                    names.append(entry.name)
    except OSError:
        return None
    recorded: dict[int, tuple[str, str | None]] = {}
    for name in names:
        pid, data = int(name.removesuffix(".json")), small_json(root / "sessions" / name)
        session = data.get("sessionId") if data else None
        if not data or data.get("pid") != pid or not isinstance(session, str):
            return None
        if not SESSION_ID.fullmatch(session):
            return None
        recorded[pid] = (session, processes.process_start(data.get("procStart")))
    if not recorded:
        return frozenset()
    argv = ["ps", "-o", "pid=", "-o", "lstart=", "-p", ",".join(map(str, sorted(recorded)))]
    result = processes.run(argv, processes.utc_env())
    if result is None or result[0] not in {0, 1}:
        return None
    live, seen = set(), set()
    for line in result[1].splitlines():
        if not line.strip():
            continue
        match = START_LINE.fullmatch(line)
        if not match or int(match[1]) not in recorded or int(match[1]) in seen:
            return None
        seen.add(int(match[1]))
        session, start = recorded[int(match[1])]
        observed = processes.process_start(match[2])
        if start is None or observed is None:
            return None
        # A recycled pid has a different start time, so the session is not live.
        if observed == start:
            live.add(session)
    if result[0] == 0 and not seen:
        return None
    return frozenset(live)


def transcript(root: Path, session_id: str) -> Path | None:
    try:
        with os.scandir(root / "projects") as entries:
            folders = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for folder in folders:
        path = folder / f"{session_id}.jsonl"
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _cwd(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        rows = records(path)
    except (OSError, ValueError):
        return ""
    return next((cwd for row in rows if (cwd := valid_cwd(row.get("cwd")))), "")


def resume_command(root: Path, session_id: str, cwd: str) -> str:
    return shell_resume("CLAUDE_CONFIG_DIR", root, f"claude --resume {session_id}", cwd)


def resume(root: Path, session_id: str) -> str:
    return resume_command(root, session_id, _cwd(transcript(root, session_id)))


def goal_context(rows: list[dict]) -> Goal:
    messages: list[tuple[str, object, str]] = []
    custom = ai_title = recap = recap_at = ""
    for row in rows:
        stamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else ""
        if row.get("type") == "custom-title":
            custom = redact(row.get("customTitle"), 120) or custom
        elif row.get("type") == "ai-title":
            ai_title = redact(row.get("aiTitle"), 120) or ai_title
        elif row.get("type") == "system" and row.get("subtype") == "away_summary":
            recap = redact(RECAP_SUFFIX.sub("", str(row.get("content") or "")), 600)
            recap_at = stamp
        # Skill bodies, compaction summaries and slash/bash-mode echoes are not requests.
        elif row.get("isMeta") or row.get("isCompactSummary"):
            continue
        elif row.get("type") in {"user", "assistant"} and isinstance(row.get("message"), dict):
            role, content = row["type"], row["message"].get("content")
            if (
                role == "user"
                and isinstance(content, list)
                and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content
                )
            ):
                continue
            messages.append((role, content, stamp))
    return Goal(custom or ai_title, recap, recap_at, tuple(messages))


def goal(session: Session) -> Goal:
    return goal_context(records(session.path))


HARNESS = Harness(
    kind="claude",
    name="Claude",
    env="CLAUDE_CONFIG_DIR",
    home_roots=lambda home: [],
    home_label=home_label,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=None,
    live_sessions=live_sessions,
    owner=lambda native: native.partition(":agent:")[0],
    session_pid=session_pid,
    resume=resume,
    goal=goal,
    free_recaps=True,
)
