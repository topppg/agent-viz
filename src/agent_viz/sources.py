"""Bounded, read-only observations of local CLI transcripts.

These are adapters for observed file formats, not supported vendor APIs. They
never establish whether a process or a signed-in account is currently alive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 512 * 1024
REQUEST_SEARCH_BYTES = 32 * 1024 * 1024
TEXT_HIDDEN = "Text hidden · enable include_text for this profile"
MAX_ENTRIES = 30_000
MAX_DEPTH = 7
DEFAULT_SCAN_LIMIT = 5_000
MAX_SCAN_LIMIT = 10_000
_CACHE: dict[tuple[str, str], tuple[tuple[int, int], dict | None]] = {}


@dataclass(frozen=True)
class SourceProfile:
    id: str
    kind: str
    label: str
    root: str
    include_text: bool = True

    def __post_init__(self) -> None:
        if self.kind not in {"codex", "claude"}:
            raise ValueError("Source kind must be codex or claude")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.id):
            raise ValueError("Invalid source profile ID")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 100:
            raise ValueError("Profile label must be 1–100 characters")
        if not isinstance(self.include_text, bool):
            raise ValueError("include_text must be a boolean")
        if not isinstance(self.root, str) or not self.root:
            raise ValueError("Profile root is required")


def _profile(kind: str, label: str, root: Path) -> SourceProfile:
    resolved = str(root.expanduser().resolve())
    suffix = hashlib.sha256(resolved.encode()).hexdigest()[:10]
    return SourceProfile(f"{kind}-{suffix}", kind, label, resolved)


def discover_profiles(home: str | Path | None = None) -> list[SourceProfile]:
    """Find common profile roots, without opening credential/configuration files.

    Labels identify storage profiles, not verified account identities. Discovery
    only returns roots containing the expected transcript directory. Symlinked
    root aliases are resolved and deduplicated; scans never follow child links.
    """
    base = Path(home).expanduser() if home is not None else Path.home()
    candidates: list[tuple[str, str, Path]] = []
    switch = base / ".codex-switch" / "profiles"
    try:
        for child in sorted(switch.iterdir()):
            if child.is_dir():
                candidates.append(("codex", f"Codex · {child.name}", child))
    except OSError:
        pass
    candidates.append(("codex", "Codex · default", base / ".codex"))
    for kind, env in (("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR")):
        value = os.environ.get(env)
        if value and home is None:
            candidates.append((kind, f"{kind.title()} · configured", Path(value)))
    try:
        for child in sorted(base.iterdir()):
            if child.name.startswith(".codex-"):
                candidates.append(("codex", "Codex · " + child.name.removeprefix(".codex-"), child))
            if child.name == ".claude" or child.name.startswith(".claude-"):
                candidates.append(
                    (
                        "claude",
                        "Claude · " + child.name.removeprefix(".claude").lstrip("-")
                        if child.name != ".claude"
                        else "Claude · default",
                        child,
                    )
                )
    except OSError:
        pass
    result = []
    seen = set()
    for kind, label, root in candidates:
        try:
            markers = ("sessions", "archived_sessions") if kind == "codex" else ("projects",)
            if not any((root / marker).is_dir() for marker in markers):
                continue
            profile = _profile(kind, label, root)
            key = (kind, profile.root)
            if key not in seen:
                seen.add(key)
                result.append(profile)
        except (OSError, RuntimeError):
            continue
    return result


def load_profiles(config_path: str | Path) -> list[SourceProfile]:
    """Read an explicit local config, either a profile list or {profiles: [...]}.

    Unknown fields fail rather than accidentally interpreting secret-bearing
    tool configuration as dashboard configuration.
    """
    value = json.loads(Path(config_path).read_text(encoding="utf-8"))
    entries = value.get("profiles") if isinstance(value, dict) else value
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Expected at most 100 source profiles")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each source profile must be an object")
        if set(entry) - {"id", "kind", "label", "root", "include_text"}:
            raise ValueError("Unknown source profile field")
        result.append(SourceProfile(**entry))
    if len({p.id for p in result}) != len(result):
        raise ValueError("Duplicate source profile ID")
    if len({(p.kind, str(Path(p.root).expanduser().resolve())) for p in result}) != len(result):
        raise ValueError("Duplicate source storage root")
    return result


def _file_inventory(
    root: Path | list[Path], limit: int, max_age_days: float | None = None
) -> tuple[list[Path], dict]:
    limit = max(1, min(int(limit), MAX_SCAN_LIMIT))
    cutoff_ns = time.time_ns() - int(max_age_days * 86400e9) if max_age_days else None
    roots = root if isinstance(root, list) else [root]
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
        "transcript_trees": [p.name for p in roots],
        "tree_count": len(roots),
        "max_age_days": max_age_days,
        "aged_out": 0,
    }
    pending = []
    for path in roots:
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
                            mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                            if cutoff_ns is not None and mtime < cutoff_ns:
                                diagnostics["aged_out"] += 1
                                continue
                            files.append((mtime, Path(entry.path)))
                        except OSError:
                            pass
        except OSError:
            if folder in roots and len(roots) == 1:
                raise
            diagnostics["unreadable_directories"] += 1
            continue
    if pending:
        diagnostics["entry_budget_hit"] = True
    selected = [path for _, path in sorted(files, key=lambda pair: pair[0], reverse=True)[:limit]]
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


def _recent_files(root: Path, limit: int) -> list[Path]:
    """Compatibility helper for metadata-only local audits."""
    return _file_inventory(root, limit)[0]


def _source_trees(profile: SourceProfile) -> list[Path]:
    root = Path(profile.root).expanduser().resolve()
    names = ("sessions", "archived_sessions") if profile.kind == "codex" else ("projects",)
    trees = [root / name for name in names if (root / name).is_dir()]
    if not trees:
        raise OSError("Transcript directory is unavailable")
    return trees


def scan_inventory(profile: SourceProfile, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """Return bounded file counts and coverage warnings, never paths/content."""
    _, diagnostics = _file_inventory(_source_trees(profile), limit)
    return {"id": profile.id, "label": profile.label, "kind": profile.kind, **diagnostics}


def _records(path: Path) -> list[dict[str, Any]]:
    """Read only a bounded header/tail; ignore partial lines and large records."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
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


def _safe(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:limit]


def _id(value: Any) -> str:
    # Native IDs, unlike labels, must not carry free-form content or paths.
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else ""
    )


def _ns(profile: SourceProfile, native_id: str) -> str:
    value = f"{profile.id}:{native_id}"
    return (
        value
        if len(value) <= 180
        else f"{profile.id}:sha256-{hashlib.sha256(native_id.encode()).hexdigest()[:32]}"
    )


def _time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        return None


def _text(message: Any, limit: int = 160) -> str:
    if isinstance(message, str):
        return _safe(message, limit)
    if isinstance(message, list):
        return _safe(
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


def _request_text(message: object, limit: int = 160) -> str:
    """The user's latest request; wrapped tool, shell or instruction echoes are skipped."""
    text = _text(message, 20_000)
    # Forked Claude subagents get shared boilerplate first; their directive follows it.
    _, fork, directive = text.partition("</fork-boilerplate>")
    if fork:
        text = re.sub(r"^\s*Your directive:\s*", "", directive)
    return "" if text.lstrip().startswith(("<", "# AGENTS.md")) else _safe(text, limit)


def _base(profile: SourceProfile, native: str, project: str, started: str | None) -> dict:
    # An observer collection is NOT a vendor conversation/session. Its identity
    # stays fixed when ancestor transcripts enter or leave a bounded scan window.
    return {
        "run_id": _ns(profile, native),
        "session_id": f"{profile.id}:observed",
        "parent_run_id": None,
        "native_id": native,
        "name": f"{profile.kind.title()} {native[:8]}",
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


def _codex(profile: SourceProfile, records: list[dict], mtime: float) -> dict | None:
    meta = next(
        (
            x.get("payload", {})
            for x in records
            if x.get("type") == "session_meta" and isinstance(x.get("payload"), dict)
        ),
        None,
    )
    if not meta:
        return None
    native = _id(meta.get("id")) or _id(meta.get("session_id"))
    if not native:
        return None
    started = _time(meta.get("timestamp")) or _time(records[0].get("timestamp"))
    cwd = meta.get("cwd", "")
    project = _safe(Path(cwd).name, 80) if isinstance(cwd, str) else ""
    source = meta.get("source", {})
    spawn = (
        source.get("subagent", {}).get("thread_spawn", {})
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict)
        else {}
    )
    parent = _id(meta.get("parent_thread_id")) or (
        _id(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""
    )
    out = _base(profile, native, project, started)
    if parent and parent != native:
        out["parent_run_id"] = _ns(profile, parent)
    last_state_time = None
    last_reply = ""
    for row in records:
        stamp = _time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind = row.get("type")
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
                reply = _text(p.get("last_agent_message")) if profile.include_text else ""
                reply = reply or last_reply
                state, evidence = "idle", str(event)
                activity = reply or "Turn ended · session may resume"
            if profile.include_text and event == "user_message":
                text = _request_text(p.get("message"))
                if text:
                    out["task"], last_reply = text, ""
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
                text = _request_text(p.get("content")) if profile.include_text else ""
                if text:
                    # A new request starts a new turn; the previous reply no longer describes it.
                    out["task"], last_reply = text, ""
                    state, activity, evidence = "working", "Turn started", "user message"
            elif event in {"reasoning", "agent_message"} or (
                event == "message" and p.get("role") == "assistant"
            ):
                state, evidence = "working", str(event)
                if profile.include_text and event in {"message", "agent_message"}:
                    # Only the agent's own prose; wrapped system notices are not replies.
                    reply = _text(p.get("content"))
                    last_reply = reply if reply and not reply.startswith("<") else last_reply
                activity = last_reply or "Model activity recorded"
        if state and (stamp is None or last_state_time is None or stamp >= last_state_time):
            out.update(
                state=state,
                activity=activity,
                evidence=f"Codex transcript: {evidence}; inferred session state",
            )
            last_state_time = stamp or last_state_time
    out["last_seen"] = out["last_seen"] or datetime.fromtimestamp(mtime, UTC).isoformat()
    return out


def _claude(profile: SourceProfile, records: list[dict], mtime: float) -> dict | None:
    session = next((_id(x.get("sessionId")) for x in records if _id(x.get("sessionId"))), "")
    if not session:
        return None
    side = next(
        (x for x in records if x.get("isSidechain") is True and _id(x.get("agentId"))), None
    )
    # parentUuid is a MESSAGE link, never an agent-parent relationship.
    agent = _id(side.get("agentId")) if side else ""
    native = f"{session}:agent:{agent}" if agent else session
    started = next((_time(x.get("timestamp")) for x in records if _time(x.get("timestamp"))), None)
    cwd = next((x["cwd"] for x in records if isinstance(x.get("cwd"), str)), "")
    out = _base(profile, native, _safe(Path(cwd).name, 80), started)
    if agent:
        out["parent_run_id"] = _ns(profile, session)
        out["name"] = f"Claude subagent {agent[:8]}"
    last_state_time = None
    prompt_seen = False
    last_reply = ""
    for row in records:
        stamp = _time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind, message = row.get("type"), row.get("message", {})
        state, activity, evidence = None, None, None
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
                and (not result or (agent and "</fork-boilerplate>" in _text(content, 20_000)))
                and not (row.get("isMeta") or row.get("isCompactSummary"))
            ):
                # A subagent's goal is the prompt its parent gave it, so the first one wins.
                text = _request_text(content, 400 if agent else 160)
                if text and not (agent and prompt_seen):
                    out["task"], prompt_seen = text, True
                    if not result:
                        last_reply = ""
        elif kind == "assistant" and isinstance(message, dict):
            stop = message.get("stop_reason")
            content = message.get("content", [])
            tool = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_use" for x in content
            )
            if profile.include_text:
                last_reply = _text(content) or last_reply
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
    out["last_seen"] = out["last_seen"] or datetime.fromtimestamp(mtime, UTC).isoformat()
    return out


def _row_request(row: dict[str, Any], kind: str) -> str:
    if kind == "codex":
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return ""
        if row.get("type") == "response_item" and payload.get("type") == "message":
            return _request_text(payload.get("content")) if payload.get("role") == "user" else ""
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            return _request_text(payload.get("message"))
        return ""
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
    return _request_text(content)


def _last_request(path: Path, kind: str) -> str:
    """The latest user request before the bounded tail, scanning backwards in chunks."""
    marker = b'"role":"user"' if kind == "codex" else b'"type":"user"'
    chunk = 1024 * 1024
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            end = os.fstat(stream.fileno()).st_size
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
                    text = _row_request(row, kind) if isinstance(row, dict) else ""
                    if text:
                        return text
                end = start
    except OSError:
        return ""
    return ""


def _subagent_description(path: Path) -> str:
    """The short description the parent gave a Claude subagent, from agent-<id>.meta.json."""
    meta = path.with_name(path.stem + ".meta.json")
    try:
        fd = os.open(meta, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(65_537)
        if len(data) > 65_536:
            return ""
        value = json.loads(data)
    except (OSError, ValueError):
        return ""
    return _safe(value.get("description"), 120) if isinstance(value, dict) else ""


def scan_profile(
    profile: SourceProfile,
    limit: int = DEFAULT_SCAN_LIMIT,
    *,
    diagnostics: dict | None = None,
    max_age_days: float | None = None,
) -> list[dict]:
    """Observe at most ``limit`` recent files; no credentials, hooks, or APIs.

    Raises OSError for a missing/unreadable root so the monitor can surface a
    sanitized source error. Individual disappearing/corrupt files are skipped.
    """
    selected, coverage = _file_inventory(_source_trees(profile), limit, max_age_days)
    coverage.update(
        id=profile.id,
        label=profile.label,
        kind=profile.kind,
        read_errors=0,
        unsupported_files=0,
        observations=0,
    )
    result = []
    active_keys = set()
    prefix = f"{profile.id}:{profile.include_text}:{profile.label}"
    for path in selected:
        try:
            info = path.stat(follow_symlinks=False)
            key = (prefix, str(path))
            active_keys.add(key)
            signature = (info.st_size, info.st_mtime_ns)
            cached = _CACHE.get(key)
            if cached and cached[0] == signature:
                value = cached[1]
            else:
                rows = _records(path)
                value = (_codex if profile.kind == "codex" else _claude)(
                    profile, rows, info.st_mtime
                )
                if value and value.get("parent_run_id") and profile.include_text:
                    value["name"] = _subagent_description(path) or value["name"]
                if value and profile.include_text and value.get("task") == TEXT_HIDDEN:
                    # Long transcripts can hold the last request before the read tail; a
                    # request found earlier stays valid until a newer one reaches the tail.
                    earlier = cached[1].get("task") if cached and cached[1] else ""
                    found = earlier if earlier and earlier != TEXT_HIDDEN else ""
                    value["task"] = found or _last_request(path, profile.kind) or TEXT_HIDDEN
                _CACHE[key] = (signature, value)
            if value:
                result.append(dict(value))
            else:
                coverage["unsupported_files"] += 1
        except (OSError, ValueError, TypeError, RecursionError):
            coverage["read_errors"] += 1
            continue
    # Bound cache lifetime/memory when transcripts rotate; no raw content cached.
    for key in list(_CACHE):
        if key[0].startswith(profile.id + ":") and key not in active_keys:
            _CACHE.pop(key, None)
    if profile.kind == "codex":
        by_id = {row["run_id"]: row for row in result}
        for row in result:
            current, seen = row, {row["run_id"]}
            while current.get("parent_run_id") in by_id:
                parent = by_id[current["parent_run_id"]]
                if parent["run_id"] in seen:
                    row["parent_run_id"] = None
                    break
                seen.add(parent["run_id"])
                current = parent
    # Some installations have duplicate transcripts: one observation per ID.
    unique = {}
    for row in result:
        prior = unique.get(row["run_id"])
        if prior is None or row["last_seen"] > prior["last_seen"]:
            unique[row["run_id"]] = row
    values = list(unique.values())
    coverage["observations"] = len(values)
    coverage["duplicate_observations"] = len(result) - len(values)
    if diagnostics is not None:
        diagnostics.update(coverage)
    return values
