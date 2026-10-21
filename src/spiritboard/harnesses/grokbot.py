"""Grok Bot: the desktop app's local cache of its cloud agents, base32-named JSON blobs."""

from __future__ import annotations

import base64
import binascii
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..transcripts import (
    TEXT_HIDDEN,
    Session,
    mtime_iso,
    native_id,
    observation,
    redact,
    reply_text,
    request_text,
    safe,
    small_json,
)
from .harness import Goal, Harness

if TYPE_CHECKING:
    from ..sources import SourceProfile

CACHE = "sand-client-persistence"
BLOB = ".blob"
PREFIX = "sand.client.slice.account."
ROSTER = re.compile(r"sand\.client\.slice\.account\.([^.]{1,200})\.roster\.last-roster")
REPLICA = ".transcript.replicas."
# Versions seen in Grok Bot 0.71; any other is skipped rather than misread.
ROSTER_SCHEMAS = frozenset({4})
REPLICA_SCHEMAS = frozenset({1})
MAX_BLOB = 8 * 1024 * 1024
MAX_BLOBS = 5_000
MAX_ROWS = 5_000
MAX_ENTRIES = 2_000
# A request newer than this without a reply means the cloud agent is still on it.
WORKING_MS = 30 * 60_000
# An agent question left unanswered longer than this was abandoned, not pending.
QUESTION_MS = 24 * 3_600_000
USER_KINDS = {"message", "user-attachment"}
_VERSIONS: dict[str, tuple[tuple[int, int], bool]] = {}


def home_roots(home: Path) -> list[tuple[str, Path]]:
    if sys.platform != "darwin":
        return []
    return [("Grok Bot", home / "Library" / "Application Support" / "Grok Bot")]


def is_root(root: Path) -> bool:
    folder = root / CACHE
    return folder.is_dir() and not folder.is_symlink()


def blob_name(key: str) -> str:
    return base64.b32encode(key.encode()).decode().rstrip("=").lower() + BLOB


def blob_key(name: str) -> str:
    stem = name.removesuffix(BLOB)
    if not name.endswith(BLOB) or not re.fullmatch(r"[a-z2-7]{1,4000}", stem):
        return ""
    try:
        return base64.b32decode(stem.upper() + "=" * (-len(stem) % 8)).decode()
    except (binascii.Error, UnicodeError, ValueError):
        return ""


def _blobs(folder: Path, diagnostics: dict) -> dict[str, Path]:
    if folder.is_symlink():
        raise OSError("Cache directory is a link")
    found: dict[str, Path] = {}
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_BLOBS:
                diagnostics["entry_budget_hit"] = True
                break
            if entry.is_symlink():
                diagnostics["symlinks_skipped"] += 1
            elif entry.is_file(follow_symlinks=False):
                key = blob_key(entry.name)
                if key.startswith(PREFIX):
                    found[key] = Path(entry.path)
    return found


def _value(path: Path, schemas: frozenset[int]) -> tuple[dict | None, bool]:
    """(value, supported): a mid-write or oversized blob is (None, True)."""
    data = small_json(path, MAX_BLOB)
    if data is None:
        return None, True
    value = data.get("value")
    if data.get("schemaVersion") not in schemas or not isinstance(value, dict):
        return None, False
    return value, True


def _supported(path: Path, info: os.stat_result) -> bool:
    """A replica's schema check, re-parsed only when the blob changes."""
    signature = (info.st_size, info.st_mtime_ns)
    cached = _VERSIONS.get(str(path))
    if cached and cached[0] == signature:
        return cached[1]
    supported = _value(path, REPLICA_SCHEMAS)[1]
    _VERSIONS[str(path)] = (signature, supported)
    return supported


def _rows(value: dict | None) -> list[dict]:
    rows = value.get("rows") if value else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows[:MAX_ROWS] if isinstance(r, dict) and native_id(r.get("id"))]


def _ms(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _iso(ms: int) -> str | None:
    if not 0 < ms < 1e14:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _activity(row: dict) -> int:
    return _ms(row.get("lastActivityAt")) or _ms(row.get("updatedAt"))


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    """One session per roster agent; hidden agents are archived and skipped unless asked for."""
    folder = root / CACHE
    diagnostics: dict = {
        "files_seen": 0,
        "selected_files": 0,
        "truncated": False,
        "scan_limit": limit,
        "discovery_limited": False,
        "entry_budget_hit": False,
        "symlinks_skipped": 0,
        "transcript_trees": [CACHE],
        "tree_count": 1,
        "max_age_days": max_age_days,
        "aged_out": 0,
        "archived_skipped": 0,
        "unsupported_schema": False,
        "unsupported_blobs": 0,
        "unreadable_blobs": 0,
    }
    blobs = _blobs(folder, diagnostics)
    cutoff = int((time.time() - max_age_days * 86400) * 1000) if max_age_days else 0
    found: list[tuple[int, Session]] = []
    for key in sorted(blobs):
        match = ROSTER.fullmatch(key)
        if not match:
            continue
        roster, supported = _value(blobs[key], ROSTER_SCHEMAS)
        if not supported:
            diagnostics["unsupported_schema"] = True
            diagnostics["unsupported_blobs"] += 1
            continue
        if roster is None:
            diagnostics["unreadable_blobs"] += 1
            continue
        try:
            roster_mtime = os.stat(blobs[key], follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        for row in _rows(roster):
            native = row["id"]
            if row.get("isHiddenFromSidebar") is True and not archived:
                diagnostics["archived_skipped"] += 1
                continue
            stamp = _activity(row)
            if cutoff and stamp < cutoff:
                diagnostics["aged_out"] += 1
                continue
            replica = f"{PREFIX}{match[1]}{REPLICA}{native}"
            path = blobs.get(replica, folder / blob_name(replica))
            size, mtime_ns = 0, roster_mtime
            if replica in blobs:
                try:
                    info = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                if not _supported(path, info):
                    diagnostics["unsupported_schema"] = True
                    diagnostics["unsupported_blobs"] += 1
                    continue
                # The roster holds the name and hidden flag, so its writes count too.
                size, mtime_ns = info.st_size, max(info.st_mtime_ns, roster_mtime)
            found.append((stamp, Session(native, path, size, mtime_ns, mtime_ns / 1e9)))
    found.sort(key=lambda item: (item[0], item[1].key), reverse=True)
    selected = [session for _, session in found[:limit]]
    limited = bool(diagnostics["entry_budget_hit"] or diagnostics["symlinks_skipped"])
    diagnostics.update(
        files_seen=len(found),
        selected_files=len(selected),
        discovery_limited=limited,
        truncated=limited or len(found) > len(selected),
    )
    return selected, diagnostics


def _account(session: Session) -> str:
    key = blob_key(session.path.name)
    account, separator, native = key.removeprefix(PREFIX).rpartition(REPLICA)
    return account if separator and key.startswith(PREFIX) and native == session.key else ""


def _row(session: Session) -> dict | None:
    account = _account(session)
    if not account:
        return None
    key = f"{PREFIX}{account}.roster.last-roster"
    roster, _ = _value(session.path.parent / blob_name(key), ROSTER_SCHEMAS)
    return next((r for r in _rows(roster) if r["id"] == session.key), None)


def _entries(session: Session) -> list[dict] | None:
    """The cached transcript, oldest first; [] when none is cached, None when unusable."""
    if not session.size:
        return []
    value, supported = _value(session.path, REPLICA_SCHEMAS)
    entries = value.get("entries") if value else None
    if not supported or not isinstance(entries, list):
        return None
    return [e for e in entries[-MAX_ENTRIES:] if isinstance(e, dict)]


def _user(entry: dict) -> bool:
    return entry.get("kind") in USER_KINDS and entry.get("role", "user") == "user"


def _request(entry: dict, limit: int = 160) -> str:
    if entry.get("kind") != "message" or entry.get("role") != "user":
        return ""
    return request_text(entry.get("content"), limit)


def _message(entry: dict) -> dict:
    message = entry.get("message") if entry.get("kind") == "send-message" else None
    return message if isinstance(message, dict) else {}


def _reply(entry: dict) -> str:
    message = _message(entry)
    return reply_text(message.get("content")) if message.get("type") == "text" else ""


def _question(entry: dict) -> str:
    """A label when an agent message still waits on the user's answer, else ""."""
    message = _message(entry)
    kind = message.get("type")
    if kind == "widget" and not ("respondedValue" in entry or entry.get("widgetSkipped")):
        return "Question asked"
    if kind == "user-form" and "formResolution" not in entry:
        return "Form requested"
    if entry.get("boxRequestId") and "boxResolution" not in entry:
        return "Approval requested"
    return ""


def observe(profile: SourceProfile, session: Session) -> dict | None:
    row = _row(session)
    entries = _entries(session)
    if row is None or entries is None:
        return None
    native = row["id"]
    out = observation(profile, "Grok Bot", native, "", _iso(_ms(row.get("createdAt"))))
    if row.get("isHiddenFromSidebar") is True:
        out["archived"] = True
    if profile.include_text:
        out["name"] = safe(row.get("name"), 120) or out["name"]
    stamps = [_activity(row), *(_ms(e.get("timestampMs")) for e in entries)]
    out["last_seen"] = _iso(max(stamps)) or out["started_at"] or mtime_iso(session.mtime)
    turn = [e for e in entries if e.get("kind") != "event"]
    if not turn:
        out.update(
            activity="No transcript cached locally",
            evidence="Grok Bot cache: agent listed without a cached transcript",
        )
        return out
    request = reply = ""
    answered = False
    for entry in reversed(turn if profile.include_text else []):
        if _user(entry):
            answered = True
            request = _request(entry)
            if request:
                break
        elif not answered and not reply:
            # The newest reply after the latest user turn; older turns no longer describe it.
            reply = _reply(entry)
    if profile.include_text:
        out["task"] = request or TEXT_HIDDEN
    last = turn[-1]
    streaming = any(e.get("isStreaming") is True for e in turn[-5:])
    age = time.time() * 1000 - _ms(last.get("timestampMs"))
    if streaming:
        state, activity, evidence = "working", "Message streaming", "message streaming"
    elif _user(last) and age < WORKING_MS:
        state, activity, evidence = "working", "Request sent · agent working", "user turn"
    elif _user(last):
        state, activity, evidence = "idle", "No reply recorded", "user turn without a reply"
    elif age < QUESTION_MS and (question := _question(last)):
        prompt = _message(last).get("widget")
        asked = reply_text(prompt.get("prompt")) if isinstance(prompt, dict) else ""
        state, evidence = "waiting_user", "agent message awaiting an answer"
        activity = (asked if profile.include_text else "") or question
    else:
        state, activity, evidence = "idle", reply or "Reply recorded", "agent message"
    out.update(
        state=state,
        activity=activity,
        evidence=f"Grok Bot cache: {evidence}; inferred, the agent runs in the cloud",
    )
    return out


def latest_request(session: Session) -> str:
    entries = _entries(session) or []
    return next((found for e in reversed(entries) if (found := _request(e))), "")


def goal(session: Session) -> Goal:
    row = _row(session) or {}
    messages: list[tuple[str, object, str]] = []
    for entry in _entries(session) or []:
        stamp = _iso(_ms(entry.get("timestampMs"))) or ""
        if _request(entry, 20_000):
            messages.append(("user", entry.get("content"), stamp))
        elif _message(entry).get("type") == "text":
            messages.append(("assistant", _message(entry).get("content"), stamp))
    return Goal(redact(row.get("name"), 120), "", "", tuple(messages))


HARNESS = Harness(
    kind="grokbot",
    name="Grok Bot",
    env="GROK_BOT_HOME",
    home_roots=home_roots,
    home_label=lambda name: None,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=None,
    # Agents run in remote boxes: no local process tells whether one is alive.
    live_sessions=lambda root: None,
    owner=lambda native: native,
    session_pid=lambda root, native: (False, None),
    resume=lambda root, native: None,
    goal=goal,
)
