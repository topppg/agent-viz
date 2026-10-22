"""Entry point for the bundled macOS dashboard server."""

from __future__ import annotations

from spiritboard.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["serve", "--no-open", "--port", "0"]))
