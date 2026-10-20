"""Polling bridge from read-only native observations to the local store."""

from __future__ import annotations

import threading

from .sources import (
    DEFAULT_SCAN_LIMIT,
    MAX_SCAN_LIMIT,
    SourceProfile,
    UsageDays,
    discover_profiles,
    prune_usage_cache,
    scan_profile,
    scan_usage,
)


class Monitor:
    def __init__(
        self,
        profiles: list[SourceProfile] | None = None,
        store=None,
        interval: float = 2,
        limit: int = DEFAULT_SCAN_LIMIT,
        max_age_days: float | None = None,
        usage_interval: float = 60,
    ):
        if store is None:
            raise ValueError("Monitor requires a store")
        self.automatic = profiles is None
        self.profiles = list(profiles or [])
        self.store = store
        self.interval = max(0.2, float(interval))
        self.limit = max(1, min(int(limit), MAX_SCAN_LIMIT))
        self.max_age_days = max_age_days
        self.usage_interval = max(1.0, float(usage_interval))
        self._usage: dict = {"profiles": {}, "limited": False}
        self.errors: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.source_stats: list[dict] = []
        self._public = {
            "profiles": self.public_profiles(),
            "source_errors": {},
            "source_stats": [],
            "automatic_discovery": self.automatic,
        }
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def scan_once(self) -> dict:
        counts, errors, stats = {}, {}, []
        with self._lock:
            profiles = list(self.profiles)
            if self.automatic:
                try:
                    found = discover_profiles()
                    known = {p.id: p for p in profiles}
                    # Keep disappeared profiles visible with an unavailable error.
                    # IDs reflect canonical storage roots, never session content.
                    known.update({p.id: p for p in found})
                    profiles = list(known.values())
                except Exception:
                    errors["discovery"] = "Profile discovery could not be completed"
            for profile in profiles:
                coverage = {"id": profile.id, "label": profile.label, "kind": profile.kind}
                try:
                    observations = scan_profile(
                        profile, self.limit, diagnostics=coverage, max_age_days=self.max_age_days
                    )
                    accepted = 0
                    for observation in observations:
                        try:
                            self.store.observe(observation)
                            accepted += 1
                        except Exception:
                            # Never expose exception text: it can contain paths/content.
                            errors[profile.id] = "An observation could not be stored"
                    counts[profile.id] = accepted
                except Exception:
                    counts[profile.id] = 0
                    errors[profile.id] = "Source unavailable or unreadable"
                    coverage.update(
                        unavailable=True,
                        observations=0,
                        truncated=True,
                        discovery_limited=True,
                        scan_limit=self.limit,
                        files_seen=0,
                        selected_files=0,
                    )
                stats.append(coverage)
            # Publish a complete scan atomically for HTTP readers; no root paths.
            self.profiles = profiles
            self.counts, self.errors, self.source_stats = counts, errors, stats
            self._public = {
                "profiles": self.public_profiles(),
                "source_errors": dict(errors),
                "source_stats": [dict(s) for s in stats],
                "automatic_discovery": self.automatic,
            }
        return {"counts": counts, "errors": errors, "source_stats": stats}

    def scan_usage_once(self) -> dict:
        """Daily usage per profile; a slow full-year pass kept off the 2s scan loop."""
        profiles: dict[str, UsageDays] = {}
        limited = False
        for profile in list(self.profiles):
            coverage: dict = {}
            try:
                profiles[profile.id] = scan_usage(profile, diagnostics=coverage)
            except Exception:
                # The regular scan already reports this source as unavailable.
                limited = True
                continue
            limited = limited or bool(
                coverage.get("truncated")
                or coverage.get("read_errors")
                or coverage.get("clipped_files")
                or coverage.get("oversized_records")
            )
        prune_usage_cache({p.id for p in self.profiles})
        self._usage = {"profiles": profiles, "limited": limited}
        return self._usage

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, name="agent-viz-sources", daemon=True),
            threading.Thread(target=self._run_usage, name="agent-viz-usage", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.interval)

    def _run_usage(self) -> None:
        while not self._stop.is_set():
            self.scan_usage_once()
            self._stop.wait(self.usage_interval)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(2, self.interval + 1))

    def public_profiles(self) -> list[dict]:
        return [
            {"id": p.id, "kind": p.kind, "label": p.label, "include_text": p.include_text}
            for p in self.profiles
        ]

    def public_state(self) -> dict:
        """One complete published scan; no partial discovery lists or root paths."""
        state = self._public
        return {
            "profiles": [dict(p) for p in state["profiles"]],
            "source_errors": dict(state["source_errors"]),
            "source_stats": [dict(s) for s in state["source_stats"]],
            "automatic_discovery": state["automatic_discovery"],
        }

    def public_usage(self) -> dict:
        """Counts and namespaced session IDs only; never paths or transcript text."""
        return self._usage
