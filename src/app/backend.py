"""Entry point for the bundled macOS dashboard server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spiritboard import cli
from spiritboard.launcher import LaunchError, default_data_dir, running_url


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spiritboard-backend")
    parser.add_argument("--data-dir", type=Path, help="show only the dashboard that owns this")
    args = parser.parse_args(argv)
    try:
        url = running_url(args.data_dir or default_data_dir())
    except LaunchError as error:
        if args.data_dir is not None:
            print(f"Spiritboard could not connect: {error}", flush=True)
            return 1
        return cli.main(["serve", "--no-open", "--port", "0"])
    print(f"Spiritboard is already running: {url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
