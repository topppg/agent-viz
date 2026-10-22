"""Claude Code: ``projects/`` transcripts and ``sessions/<pid>.json`` process records.

Sessions the Claude desktop app runs are ordinary Claude Code transcripts marked by their
``entrypoint``; the app's ``local_*.json`` session records add titles and archive state.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import processes
from ..processes import Process
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
    reply_text,
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
DESKTOP_APP = "Claude desktop"
DESKTOP_BUNDLE = "com.anthropic.claudefordesktop"
# The app itself, or the Claude Code build it bundles under Application Support.
DESKTOP_PROCESS = re.compile(r"/Claude\.app/Contents/|/Application Support/Claude/claude-code/")
# The app's own validator for claude://code/continue?session=…
LOCAL_ID = re.compile(r"local_[A-Za-z0-9-]{1,64}")
MAX_DESKTOP_FILES = 5_000


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
    entry = next((x["entrypoint"] for x in rows if isinstance(x.get("entrypoint"), str)), "")
    if entry.startswith("claude-desktop"):
        out["app"] = DESKTOP_APP
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
        if kind == READ_GAP:
            # The header's request and reply predate the unread middle; the scan searches back.
            # A subagent keeps its first prompt, which is the task its parent gave it.
            last_reply = ""
            if not agent:
                out["task"] = TEXT_HIDDEN
            continue
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
        elif kind == "assistant" and isinstance(message, dict):
            stop = message.get("stop_reason")
            content = message.get("content", [])
            tool = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_use" for x in content
            )
            if profile.include_text:
                last_reply = reply_text(content) or last_reply
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
        if custom_title:
            out["title_source"] = "user"
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
    rows = records(session.path)
    value = parse(profile, rows, session.mtime)
    if (
        value
        and profile.include_text
        and any(row.get("type") == READ_GAP for row in rows)
        and value["activity"]
        in {
            "Tool result recorded",
            "User turn recorded",
            "Turn ended · session may resume",
            "Tool call recorded",
            "Model activity recorded",
        }
    ):
        value["activity"] = latest_reply(session) or value["activity"]
    if value and value.get("parent_run_id") and profile.include_text:
        description = subagent_description(session.path)
        value["name"] = description or value["name"]
        if not description:
            value["title_source"] = "placeholder"
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


def row_reply(row: dict[str, Any]) -> str:
    message = row.get("message")
    if row.get("type") != "assistant" or not isinstance(message, dict):
        return ""
    return reply_text(message.get("content"))


def latest_reply(session: Session) -> str:
    return last_request(session.path, b'"type":"assistant"', row_reply)


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


@dataclass(frozen=True)
class DesktopSession:
    local_id: str
    title: str
    archived: bool


_desktop_lock = threading.Lock()
_desktop_files: dict[str, tuple[tuple[int, int], tuple[str, DesktopSession] | None]] = {}


def desktop_dir() -> Path | None:
    if sys.platform != "darwin":
        return None
    return Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"


def _subdirs(folder: Path) -> list[Path]:
    try:
        with os.scandir(folder) as entries:
            return [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _desktop_record(path: Path) -> tuple[str, DesktopSession] | None:
    data = small_json(path)
    if not data:
        return None
    local, cli = data.get("sessionId"), data.get("cliSessionId")
    if not isinstance(local, str) or local != path.stem or not LOCAL_ID.fullmatch(local):
        return None
    if not isinstance(cli, str) or not SESSION_ID.fullmatch(cli):
        return None
    return cli, DesktopSession(local, safe(data.get("title"), 120), data.get("isArchived") is True)


def desktop_sessions() -> dict[str, DesktopSession]:
    """Claude Code session ID -> the desktop app's record, from ``<account>/<org>/local_*.json``.

    Only names of that form are opened (bounded, no links); other files there are never read.
    """
    base = desktop_dir()
    if base is None:
        return {}
    found: dict[str, DesktopSession] = {}
    seen: set[str] = set()
    with _desktop_lock:
        for folder in [org for account in _subdirs(base) for org in _subdirs(account)]:
            try:
                with os.scandir(folder) as entries:
                    files = [
                        e
                        for e in entries
                        if LOCAL_ID.fullmatch(e.name.removesuffix(".json"))
                        and e.name.endswith(".json")
                        and e.is_file(follow_symlinks=False)
                    ]
            except OSError:
                continue
            for entry in files:
                if len(seen) >= MAX_DESKTOP_FILES:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                seen.add(entry.path)
                signature = (info.st_mtime_ns, info.st_size)
                cached = _desktop_files.get(entry.path)
                if cached is None or cached[0] != signature:
                    cached = (signature, _desktop_record(Path(entry.path)))
                    _desktop_files[entry.path] = cached
                if cached[1]:
                    found[cached[1][0]] = cached[1][1]
        for path in set(_desktop_files) - seen:
            del _desktop_files[path]
    return found


def enrich(profile: SourceProfile, rows: list[dict]) -> None:
    """Mark desktop sessions, hide archived ones and prefer a /rename over the app's title."""
    index = desktop_sessions()
    for row in rows:
        native = row.get("native_id", "")
        desktop = index.get(native.partition(":agent:")[0])
        if desktop is None:
            continue
        row["app"] = DESKTOP_APP
        if desktop.archived:
            row["archived"] = True
        if (
            profile.include_text
            and desktop.title
            and ":agent:" not in native
            and row.get("title_source") != "user"
        ):
            row["name"] = desktop.title


def open_app(session_id: str, app: str, chain: list[Process]) -> tuple[str, tuple[str, ...]] | None:
    """(label, argv) showing a live desktop session in the Claude app; None elsewhere.

    ``claude://resume`` is not used: the app imports the CLI session as a new one.
    """
    if sys.platform != "darwin":
        return None
    if app != DESKTOP_APP and not any(DESKTOP_PROCESS.search(p.command) for p in chain):
        return None
    desktop = desktop_sessions().get(session_id)
    if desktop and not desktop.archived and LOCAL_ID.fullmatch(desktop.local_id):
        return "Claude", ("/usr/bin/open", f"claude://code/continue?session={desktop.local_id}")
    return "Claude", ("/usr/bin/open", "-b", DESKTOP_BUNDLE)


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
    enrich=enrich,
    open_app=open_app,
    live_sessions=live_sessions,
    owner=lambda native: native.partition(":agent:")[0],
    session_pid=session_pid,
    resume=resume,
    goal=goal,
    free_recaps=True,
)
