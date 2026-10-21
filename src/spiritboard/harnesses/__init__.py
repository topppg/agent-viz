"""Registered agent harnesses, keyed by source kind; discovery follows this order."""

from . import claude, codex, opencode, pi
from .harness import Goal, Harness

HARNESSES: dict[str, Harness] = {
    h.kind: h for h in (codex.HARNESS, claude.HARNESS, opencode.HARNESS, pi.HARNESS)
}

__all__ = ["HARNESSES", "Goal", "Harness"]
