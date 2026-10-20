"""Bounded, read-only observations of local CLI transcripts.

These are adapters for observed file formats, not supported vendor APIs. They
never establish whether a process or a signed-in account is currently alive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 512 * 1024
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
    include_text: bool = False

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
                candidates.append(("claude", "Claude · " + child.name.removeprefix(".claude").lstrip("-") if child.name != ".claude" else "Claude · default", child))
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


def _file_inventory(root: Path | list[Path], limit: int) -> tuple[list[Path], dict]:
    limit = max(1, min(int(limit), MAX_SCAN_LIMIT))
    roots = root if isinstance(root, list) else [root]
    diagnostics = {"files_seen": 0, "selected_files": 0, "truncated": False,
                   "scan_limit": limit, "discovery_limited": False,
                   "entry_budget_hit": False, "depth_budget_hit": False,
                   "unreadable_directories": 0, "symlinks_skipped": 0,
                   "transcript_trees": [p.name for p in roots], "tree_count": len(roots)}
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
                            files.append((entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path)))
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
    limited = bool(diagnostics["entry_budget_hit"] or diagnostics["depth_budget_hit"]
                   or diagnostics["unreadable_directories"] or diagnostics["symlinks_skipped"])
    diagnostics.update(files_seen=len(files), selected_files=len(selected),
                       discovery_limited=limited, truncated=limited or len(files) > len(selected))
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
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else ""


def _ns(profile: SourceProfile, native_id: str) -> str:
    value = f"{profile.id}:{native_id}"
    return value if len(value) <= 180 else f"{profile.id}:sha256-{hashlib.sha256(native_id.encode()).hexdigest()[:32]}"


def _time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _text(message: Any) -> str:
    if isinstance(message, str):
        return _safe(message)
    if isinstance(message, list):
        return _safe(" ".join(x.get("text", "") for x in message if isinstance(x, dict) and x.get("type") in {"text", "input_text", "output_text"} and isinstance(x.get("text"), str)))
    return ""


def _base(profile: SourceProfile, native: str, project: str, started: str | None) -> dict:
    # An observer collection is NOT a vendor conversation/session. Its identity
    # stays fixed when ancestor transcripts enter or leave a bounded scan window.
    return {"run_id": _ns(profile, native), "session_id": f"{profile.id}:observed",
            "parent_run_id": None, "native_id": native,
            "name": f"{profile.kind.title()} {native[:8]}",
            "task": "Text hidden · enable include_text for this profile",
            "activity": "Transcript observed", "source": profile.kind,
            "account": profile.label, "project": project, "state": "unknown",
            "inferred": True, "evidence": "Read-only transcript; no process liveness proof",
            "last_seen": started, "started_at": started, "health": "observed"}


def _codex(profile: SourceProfile, records: list[dict], mtime: float) -> dict | None:
    meta = next((x.get("payload", {}) for x in records if x.get("type") == "session_meta" and isinstance(x.get("payload"), dict)), None)
    if not meta:
        return None
    native = _id(meta.get("id")) or _id(meta.get("session_id"))
    if not native:
        return None
    started = _time(meta.get("timestamp")) or _time(records[0].get("timestamp"))
    cwd = meta.get("cwd", "")
    project = _safe(Path(cwd).name, 80) if isinstance(cwd, str) else ""
    source = meta.get("source", {})
    spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else {}
    parent = _id(meta.get("parent_thread_id")) or (_id(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else "")
    out = _base(profile, native, project, started)
    if parent and parent != native:
        out["parent_run_id"] = _ns(profile, parent)
    last_state_time = None
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
                state, activity, evidence = "idle", "Turn ended · session may resume", str(event)
            if profile.include_text and event == "user_message" and not out.get("_task_set"):
                text = _text(p.get("message"))
                if text:
                    out["task"], out["_task_set"] = text, True
        elif kind == "response_item":
            event = p.get("type")
            if event in {"function_call", "custom_tool_call", "local_shell_call"}:
                state, activity, evidence = "waiting_tool", "Tool call recorded", str(event)
            elif event in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
                state, activity, evidence = "working", "Tool result recorded", str(event)
            elif event in {"message", "reasoning", "agent_message"} and p.get("role") != "user":
                state, activity, evidence = "working", "Model activity recorded", str(event)
                if profile.include_text and event in {"message", "agent_message"}:
                    activity = _text(p.get("content")) or activity
        if state and (stamp is None or last_state_time is None or stamp >= last_state_time):
            out.update(state=state, activity=activity, evidence=f"Codex transcript: {evidence}; inferred session state")
            last_state_time = stamp or last_state_time
    out.pop("_task_set", None)
    out["last_seen"] = out["last_seen"] or datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    return out


def _claude(profile: SourceProfile, records: list[dict], mtime: float) -> dict | None:
    session = next((_id(x.get("sessionId")) for x in records if _id(x.get("sessionId"))), "")
    if not session:
        return None
    side = next((x for x in records if x.get("isSidechain") is True and _id(x.get("agentId"))), None)
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
    task_set = False
    for row in records:
        stamp = _time(row.get("timestamp"))
        if stamp and (out["last_seen"] is None or stamp > out["last_seen"]):
            out["last_seen"] = stamp
        kind, message = row.get("type"), row.get("message", {})
        state, activity, evidence = None, None, None
        if kind == "user" and isinstance(message, dict):
            content = message.get("content", [])
            result = isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
            state, activity, evidence = "working", "Tool result recorded" if result else "User turn recorded", "user/tool_result" if result else "user"
            if profile.include_text and not result and not task_set:
                text = _text(content)
                if text:
                    out["task"], task_set = text, True
        elif kind == "assistant" and isinstance(message, dict):
            stop = message.get("stop_reason")
            content = message.get("content", [])
            tool = isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_use" for x in content)
            if stop in {"end_turn", "stop_sequence", "max_tokens"}:
                state, activity, evidence = "idle", "Turn ended · session may resume", "assistant stop_reason"
            elif tool or stop == "tool_use":
                state, activity, evidence = "waiting_tool", "Tool call recorded", "assistant tool_use"
            else:
                state, activity, evidence = "working", "Model activity recorded", "assistant"
                if profile.include_text:
                    activity = _text(content) or activity
        elif kind == "system" and row.get("subtype") == "turn_duration":
            state, activity, evidence = "idle", "Turn ended · session may resume", "turn_duration"
        if state and (stamp is None or last_state_time is None or stamp >= last_state_time):
            out.update(state=state, activity=activity, evidence=f"Claude transcript: {evidence}; inferred session state")
            last_state_time = stamp or last_state_time
    out["last_seen"] = out["last_seen"] or datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    return out


def scan_profile(profile: SourceProfile, limit: int = DEFAULT_SCAN_LIMIT, *, diagnostics: dict | None = None) -> list[dict]:
    """Observe at most ``limit`` recent files; no credentials, hooks, or APIs.

    Raises OSError for a missing/unreadable root so the monitor can surface a
    sanitized source error. Individual disappearing/corrupt files are skipped.
    """
    selected, coverage = _file_inventory(_source_trees(profile), limit)
    coverage.update(id=profile.id, label=profile.label, kind=profile.kind,
                    read_errors=0, unsupported_files=0, observations=0)
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
                value = (_codex if profile.kind == "codex" else _claude)(profile, rows, info.st_mtime)
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
