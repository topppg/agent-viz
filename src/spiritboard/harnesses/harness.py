"""What every agent harness adapter provides; see "Adding a harness" in docs/SOURCES.md."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..processes import Process
from ..transcripts import Session

if TYPE_CHECKING:
    from ..sources import SourceProfile

# Native session IDs handed to ``session_pid`` and ``resume`` always match this.
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,79}")


@dataclass(frozen=True)
class Goal:
    """Summary context. ``title`` and ``recap`` are already redacted; messages are raw
    ``(role, content, timestamp)`` and are filtered and redacted by summaries."""

    title: str
    recap: str
    recap_at: str
    messages: tuple[tuple[str, object, str], ...]


@dataclass(frozen=True)
class Harness:
    kind: str
    name: str
    # Environment variable that selects a storage root, e.g. CODEX_HOME.
    env: str
    # (label, root) candidates always checked under a home directory.
    home_roots: Callable[[Path], list[tuple[str, Path]]]
    # Profile label for a home directory entry name, or None when it is not a root.
    home_label: Callable[[str], str | None]
    is_root: Callable[[Path], bool]
    # (newest sessions, coverage); ``archived=False`` limits it to live storage.
    # Raises OSError when the root holds no readable session storage.
    inventory: Callable[[Path, int, float | None, bool], tuple[list[Session], dict]]
    observe: Callable[[SourceProfile, Session], dict | None]
    # Deep search for the latest request when the bounded read did not reach it.
    latest_request: Callable[[Session], str]
    # Native session ID -> session title, when the harness keeps a separate index.
    titles: Callable[[Path], dict[str, str]] | None
    live_sessions: Callable[[Path], frozenset[str] | None]
    # Native ID of the session whose process owns a (sub)agent's native ID.
    owner: Callable[[str], str]
    # (known, pid): known=False means liveness cannot be told.
    session_pid: Callable[[Path, str], tuple[bool, int | None]]
    resume: Callable[[Path, str], str | None]
    goal: Callable[[Session], Goal]
    # True when a recorded recap can be shown as the summary with no provider call.
    free_recaps: bool = False
    # Adjusts one scan's observations in place from metadata kept outside the transcripts.
    enrich: Callable[[SourceProfile, list[dict]], None] | None = None
    # (label, argv) opening a live session in its own app, from the native session ID, the
    # run's ``app`` marker and the owning process chain; None leaves it to the generic adapter.
    open_app: Callable[[str, str, list[Process]], tuple[str, tuple[str, ...]] | None] | None = None


def shell_resume(env: str, root: Path, command: str, cwd: str) -> str:
    parts = [f"cd {shlex.quote(cwd)} &&"] if cwd else []
    parts.append(f"{env}={shlex.quote(str(root))}")
    parts.append(command)
    return " ".join(parts)
