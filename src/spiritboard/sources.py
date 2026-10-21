"""Bounded, read-only observations of local CLI transcripts.

These are adapters for observed file formats, not supported vendor APIs. They
never establish whether a process or a signed-in account is currently alive.
Each harness's format lives in ``spiritboard.harnesses``; this module only
discovers profiles and runs the shared, cached scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .harnesses import HARNESSES
from .transcripts import READ_FLAGS, TEXT_HIDDEN

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
        if self.kind not in HARNESSES:
            raise ValueError("Source kind must be one of: " + ", ".join(HARNESSES))
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
    for harness in HARNESSES.values():
        candidates += [(harness.kind, label, root) for label, root in harness.home_roots(base)]
    for harness in HARNESSES.values():
        value = os.environ.get(harness.env)
        if value and home is None:
            candidates.append((harness.kind, f"{harness.name} · configured", Path(value)))
    try:
        for child in sorted(base.iterdir()):
            for harness in HARNESSES.values():
                label = harness.home_label(child.name)
                if label:
                    candidates.append((harness.kind, label, child))
    except OSError:
        pass
    result = []
    seen = set()
    for kind, label, root in candidates:
        try:
            if not HARNESSES[kind].is_root(root):
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
    with os.fdopen(os.open(config_path, READ_FLAGS), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Source config must be a regular file")
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Source config exceeds the size limit")
    value = json.loads(raw.decode("utf-8"))
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


def scan_inventory(profile: SourceProfile, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """Return bounded file counts and coverage warnings, never paths/content."""
    root = Path(profile.root).expanduser().resolve()
    limit = max(1, min(int(limit), MAX_SCAN_LIMIT))
    _, diagnostics = HARNESSES[profile.kind].inventory(root, limit, None, True)
    return {"id": profile.id, "label": profile.label, "kind": profile.kind, **diagnostics}


def scan_profile(
    profile: SourceProfile,
    limit: int = DEFAULT_SCAN_LIMIT,
    *,
    diagnostics: dict | None = None,
    max_age_days: float | None = None,
) -> list[dict]:
    """Observe at most ``limit`` recent sessions; no credentials, hooks, or APIs.

    Raises OSError for a missing/unreadable root so the monitor can surface a
    sanitized source error. Individual disappearing/corrupt files are skipped.
    """
    harness = HARNESSES[profile.kind]
    limit = max(1, min(int(limit), MAX_SCAN_LIMIT))
    root = Path(profile.root).expanduser()
    selected, coverage = harness.inventory(root.resolve(), limit, max_age_days, True)
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
    titles = harness.titles(root) if harness.titles and profile.include_text else {}
    for session in selected:
        try:
            key = (prefix, session.key)
            active_keys.add(key)
            signature = (session.size, session.mtime_ns)
            cached = _CACHE.get(key)
            if cached and cached[0] == signature:
                value = cached[1]
            else:
                value = harness.observe(profile, session)
                if value and profile.include_text and value.get("task") == TEXT_HIDDEN:
                    # Long transcripts can hold the last request before the read tail; a
                    # request found earlier stays valid until a newer one reaches the tail.
                    earlier = cached[1].get("task") if cached and cached[1] else ""
                    found = earlier if earlier and earlier != TEXT_HIDDEN else ""
                    value["task"] = found or harness.latest_request(session) or TEXT_HIDDEN
                _CACHE[key] = (signature, value)
            if value:
                result.append(dict(value))
                if titles and value.get("native_id") in titles:
                    result[-1]["name"] = titles[value["native_id"]]
            else:
                coverage["unsupported_files"] += 1
        except (OSError, ValueError, TypeError, RecursionError):
            coverage["read_errors"] += 1
            continue
    if harness.enrich:
        harness.enrich(profile, result)
    # Bound cache lifetime/memory when transcripts rotate; no raw content cached.
    for key in list(_CACHE):
        if key[0].startswith(profile.id + ":") and key not in active_keys:
            _CACHE.pop(key, None)
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
