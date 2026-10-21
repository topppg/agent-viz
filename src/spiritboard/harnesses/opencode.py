"""opencode: sessions, messages and parts in its SQLite database, opened read-only."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import processes
from ..transcripts import (
    TEXT_HIDDEN,
    Session,
    native_id,
    ns,
    observation,
    redact,
    reply_text,
    request_text,
    safe,
    valid_cwd,
)
from .harness import Goal, Harness, shell_resume

if TYPE_CHECKING:
    from ..sources import SourceProfile

DATABASE = "opencode.db"
# Credential tables; the authorizer refuses any statement that touches them.
FORBIDDEN = frozenset({"account", "account_state", "control_account"})
COLUMNS = {
    "session": {
        "id",
        "parent_id",
        "directory",
        "title",
        "time_created",
        "time_updated",
        "time_archived",
    },
    "message": {"id", "session_id", "time_created", "time_updated", "data"},
    "part": {"id", "message_id", "session_id", "time_updated", "data"},
}
CHUNK = 500
HOT_MS = 15 * 60_000
RECENT_MESSAGES = 20
REQUEST_MESSAGES = 200
MAX_PARTS = 400
MAX_TEXT = 4000
MAX_DIRECTORIES = 100
ACTIVE_TOOL = {"pending", "running"}


def home_roots(home: Path) -> list[tuple[str, Path]]:
    data = home / ".local" / "share" / "opencode"
    configured = ""
    # Selectors describe the real home only; an explicit ``home`` (tests, demos) ignores them.
    if home == Path.home():
        xdg = os.environ.get("XDG_DATA_HOME", "")
        data = Path(xdg) / "opencode" if os.path.isabs(xdg) else data
        configured = os.environ.get("OPENCODE_DB", "")
    candidates = [("opencode", data / DATABASE)]
    try:
        with os.scandir(data) as entries:
            names = sorted(e.name for e in entries if e.name.startswith("opencode-"))
    except OSError:
        names = []
    for name in names[:20]:
        if name.endswith(".db"):
            candidates.append((f"opencode · {name[9:-3]}", data / name))
    if configured and not os.path.isabs(configured):
        candidates.append(("opencode · configured", data / configured))
    return candidates


def database(root: Path) -> Path:
    return root / DATABASE if root.is_dir() else root


def is_root(root: Path) -> bool:
    path = database(root)
    return path.name.endswith(".db") and path.is_file()


def _guard(action: int, arg1: str | None, arg2: str | None, *_: str | None) -> int:
    if action == sqlite3.SQLITE_READ and arg1 in FORBIDDEN:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and arg2 in FORBIDDEN:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@contextmanager
def connect(root: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection; SQLite failures surface as OSError like other storage errors."""
    path = Path(os.path.abspath(database(root)))
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OSError("opencode database is not a regular file")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as error:
        raise OSError("opencode database is unreadable") from error
    try:
        conn.set_authorizer(_guard)
        conn.execute("PRAGMA query_only=ON")
        yield conn
    except sqlite3.Error as error:
        raise OSError("opencode database query failed") from error
    finally:
        conn.close()


def supported(conn: sqlite3.Connection) -> bool:
    """True when the v1 ``session``/``message``/``part`` tables exist (not only ``session_v2``)."""
    for table, needed in COLUMNS.items():
        found = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not needed <= found:
            return False
    return True


def _iso(ms: object) -> str | None:
    if not isinstance(ms, int | float) or isinstance(ms, bool) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _chunks(values: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(values), CHUNK):
        yield values[start : start + CHUNK]


def _latest(conn: sqlite3.Connection, table: str, ids: list[str]) -> dict[str, tuple[int, int]]:
    """Per session: (row count, newest ``time_updated``) in ``table``."""
    found: dict[str, tuple[int, int]] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        query = (
            f"SELECT session_id, COUNT(*), MAX(time_updated) FROM {table} "
            f"WHERE session_id IN ({marks}) GROUP BY session_id"
        )
        for sid, count, newest in conn.execute(query, chunk):
            found[sid] = (int(count or 0), int(newest or 0))
    return found


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    """Newest non-archived sessions; archived ones are never observed, whatever ``archived``."""
    db = database(root)
    diagnostics: dict = {
        "files_seen": 0,
        "selected_files": 0,
        "truncated": False,
        "scan_limit": limit,
        "discovery_limited": False,
        "transcript_trees": [db.name],
        "tree_count": 1,
        "max_age_days": max_age_days,
        "aged_out": 0,
        "archived_skipped": 0,
        "unsupported_schema": False,
    }
    cutoff = int((time.time() - max_age_days * 86400) * 1000) if max_age_days else 0
    with connect(root) as conn:
        if not supported(conn):
            diagnostics["unsupported_schema"] = True
            return [], diagnostics
        live, aged, archived_count = conn.execute(
            "SELECT SUM(time_archived IS NULL AND time_updated >= ?), "
            "SUM(time_archived IS NULL AND time_updated < ?), "
            "SUM(time_archived IS NOT NULL) FROM session",
            (cutoff, cutoff),
        ).fetchone()
        rows = conn.execute(
            "SELECT id, time_updated FROM session WHERE time_archived IS NULL "
            "AND time_updated >= ? ORDER BY time_updated DESC, id DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        ids = [sid for sid, _ in rows if isinstance(sid, str)]
        messages = _latest(conn, "message", ids)
        now = time.time() * 1000
        newest = {sid: max(int(t or 0), messages.get(sid, (0, 0))[1]) for sid, t in rows}
        # Part updates (streamed text, tool status) only matter for recently active sessions.
        hot = [sid for sid in ids if now - newest[sid] < HOT_MS]
        for sid, (_, stamp) in _latest(conn, "part", hot).items():
            newest[sid] = max(newest[sid], stamp)
    sessions = [
        Session(sid, db, messages.get(sid, (0, 0))[0], newest[sid] * 1_000_000, newest[sid] / 1000)
        for sid in ids
    ]
    diagnostics.update(
        files_seen=int(live or 0),
        selected_files=len(sessions),
        truncated=int(live or 0) > len(sessions),
        aged_out=int(aged or 0) if cutoff else 0,
        archived_skipped=int(archived_count or 0),
    )
    return sessions, diagnostics


def _session(conn: sqlite3.Connection, sid: str) -> tuple | None:
    return conn.execute(
        "SELECT id, parent_id, directory, substr(title, 1, 300), time_created, time_updated, "
        "time_archived FROM session WHERE id = ?",
        (sid,),
    ).fetchone()


def _messages(conn: sqlite3.Connection, sid: str, limit: int, role: str = "") -> list[tuple]:
    """Newest first: (id, role, created, completed, agent)."""
    where = "AND json_extract(data, '$.role') = ?" if role else ""
    args = (sid, role, limit) if role else (sid, limit)
    return conn.execute(
        "SELECT id, json_extract(data, '$.role'), json_extract(data, '$.time.created'), "
        "json_extract(data, '$.time.completed'), json_extract(data, '$.agent') "
        f"FROM message WHERE session_id = ? {where} "
        "ORDER BY time_created DESC, id DESC LIMIT ?",
        args,
    ).fetchall()


def _parts(conn: sqlite3.Connection, message_ids: list[str]) -> dict[str, list[tuple]]:
    """Per message, in order: (type, tool, status, text). Reasoning and tool I/O are never read."""
    found: dict[str, list[tuple]] = {}
    for chunk in _chunks(message_ids):
        marks = ",".join("?" * len(chunk))
        query = (
            "SELECT message_id, json_extract(data, '$.type') AS kind, "
            "json_extract(data, '$.tool'), json_extract(data, '$.state.status'), "
            "CASE WHEN json_extract(data, '$.type') = 'text' "
            "AND coalesce(json_extract(data, '$.synthetic'), 0) = 0 "
            f"THEN substr(json_extract(data, '$.text'), 1, {MAX_TEXT}) END "
            f"FROM part WHERE message_id IN ({marks}) ORDER BY time_created DESC, id DESC LIMIT ?"
        )
        for mid, kind, tool, status, body in conn.execute(query, [*chunk, MAX_PARTS]):
            found.setdefault(mid, []).append((kind, tool, status, body))
    return {mid: rows[::-1] for mid, rows in found.items()}


def _body(parts: list[tuple]) -> str:
    return " ".join(p[3] for p in parts if p[0] == "text" and isinstance(p[3], str))


def _request(conn: sqlite3.Connection, sid: str, limit: int) -> str:
    users = [row[0] for row in _messages(conn, sid, limit, "user")]
    parts = _parts(conn, users)
    for mid in users:
        found = request_text(_body(parts.get(mid, [])))
        if found:
            return found
    return ""


def observe(profile: SourceProfile, session: Session) -> dict | None:
    with connect(session.path) as conn:
        row = _session(conn, session.key)
        if row is None or row[6] is not None or not native_id(row[0]):
            return None
        messages = _messages(conn, row[0], RECENT_MESSAGES)
        parts = _parts(conn, [m[0] for m in messages])
        request = _request(conn, row[0], 5) if profile.include_text else ""
    sid, parent, directory, title, created, updated = row[:6]
    cwd = valid_cwd(directory)
    out = observation(profile, "opencode", sid, safe(Path(cwd).name, 80), _iso(created))
    if native_id(parent) and parent != sid:
        out["parent_run_id"] = ns(profile, parent)
    out["last_seen"] = _iso(max([updated or 0, *(m[2] or 0 for m in messages)])) or _iso(created)
    if profile.include_text:
        out["name"] = safe(title, 120) or out["name"]
        out["task"] = request or TEXT_HIDDEN
    assistant = next((m for m in messages if m[1] == "assistant"), None)
    if assistant is None:
        return out
    current = parts.get(assistant[0], [])
    active = [p[1] for p in current if p[0] == "tool" and p[2] in ACTIVE_TOOL]
    reply = ""
    if profile.include_text:
        # The newest reply after the latest request; older turns no longer describe the session.
        for message in messages:
            if message[1] == "user":
                break
            reply = reply or reply_text(_body(parts.get(message[0], [])))
    if active:
        tool = safe(active[-1], 40) if profile.include_text else ""
        state, evidence = "working", "tool part running"
        activity = f"Running {tool}" if tool else "Tool running"
    elif assistant[3] is None:
        state, evidence = "working", "assistant message incomplete"
        activity = reply or "Model activity recorded"
    else:
        state, evidence = "idle", "assistant message completed"
        activity = reply or "Turn ended · session may resume"
    out.update(
        state=state,
        activity=activity,
        evidence=f"opencode database: {evidence}; inferred session state",
    )
    return out


def latest_request(session: Session) -> str:
    try:
        with connect(session.path) as conn:
            return _request(conn, session.key, REQUEST_MESSAGES)
    except OSError:
        return ""


def _processes() -> dict[str, list[int]] | None:
    """Top-level opencode process IDs by working directory; None when ps/lsof cannot tell."""
    lsof = shutil.which("lsof")
    listing = processes.run(["ps", "-axo", "pid=,ppid=,comm="])
    if not lsof or listing is None or listing[0] != 0:
        return None
    found: dict[int, int] = {}
    for line in listing[1].splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            if Path(fields[2].strip()).name.lower().startswith("opencode"):
                found[int(fields[0])] = int(fields[1])
    # A launcher shim and its native binary share one cwd; only the outermost counts.
    top = sorted(pid for pid, ppid in found.items() if ppid not in found and pid != os.getpid())
    if not top:
        return {}
    result = processes.run([lsof, "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, top))])
    if result is None or result[0] not in {0, 1}:
        return None
    dirs: dict[str, list[int]] = {}
    pid = 0
    for line in result[1].splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid in top:
            dirs.setdefault(os.path.realpath(line[1:]), []).append(pid)
    return dirs


def _live(conn: sqlite3.Connection, dirs: dict[str, list[int]]) -> dict[str, list[int]]:
    """Live session IDs -> candidate pids. Liveness is per directory, not per session:
    the N most recently updated top-level sessions in a directory with N opencode
    processes count as live, together with their child sessions."""
    live: dict[str, list[int]] = {}
    for directory, pids in sorted(dirs.items())[:MAX_DIRECTORIES]:
        newest = conn.execute(
            "SELECT id FROM session WHERE directory = ? AND time_archived IS NULL "
            "AND parent_id IS NULL ORDER BY time_updated DESC LIMIT ?",
            (directory, len(pids)),
        ).fetchall()
        for (sid,) in newest:
            live[sid] = pids
            for (child,) in conn.execute(
                "SELECT id FROM session WHERE parent_id = ? LIMIT 500", (sid,)
            ):
                live[child] = pids
    return live


def live_sessions(root: Path) -> frozenset[str] | None:
    dirs = _processes()
    if dirs is None:
        return None
    if not dirs:
        return frozenset()
    try:
        with connect(root) as conn:
            return frozenset(_live(conn, dirs)) if supported(conn) else None
    except OSError:
        return None


def session_pid(root: Path, native: str) -> tuple[bool, int | None]:
    dirs = _processes()
    if dirs is None:
        return False, None
    try:
        with connect(root) as conn:
            if not supported(conn):
                return False, None
            row = _session(conn, native)
            live = _live(conn, dirs) if row else {}
    except OSError:
        return False, None
    cwd = valid_cwd(row[2]) if row else ""
    if not cwd:
        return False, None
    if not dirs.get(os.path.realpath(cwd)):
        return True, None
    pids = live.get(native, [])
    # Another opencode in this directory may still hold an older session: neither focus nor resume.
    return (True, pids[0]) if len(pids) == 1 else (False, None)


def resume(root: Path, native: str) -> str | None:
    try:
        with connect(root) as conn:
            row = _session(conn, native) if supported(conn) else None
    except OSError:
        return None
    if row is None:
        return None
    db = Path(os.path.abspath(database(root)))
    return shell_resume("OPENCODE_DB", db, f"opencode -s {native}", valid_cwd(row[2]))


def goal(session: Session) -> Goal:
    with connect(session.path) as conn:
        row = _session(conn, session.key)
        messages = _messages(conn, session.key, RECENT_MESSAGES * 2) if row else []
        parts = _parts(conn, [m[0] for m in messages])
    history = tuple(
        (m[1], _body(parts.get(m[0], [])), _iso(m[2]) or "")
        for m in reversed(messages)
        if m[1] in {"user", "assistant"}
    )
    return Goal(redact(row[3], 120) if row else "", "", "", history)


HARNESS = Harness(
    kind="opencode",
    name="opencode",
    env="OPENCODE_DB",
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
