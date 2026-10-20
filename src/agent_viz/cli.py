"""Local commands. Never modifies native account or transcript storage."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import threading
from dataclasses import asdict
from pathlib import Path

from .server import Collector
from .sources import discover_profiles, load_profiles
from .store import Store


def write_private(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w") as file:
        file.write(value)


def token_for(directory: Path):
    path = directory / "token"
    if not path.exists():
        try:
            write_private(path, secrets.token_urlsafe(32) + "\n")
        except FileExistsError:
            pass
    if path.is_symlink():
        raise ValueError("token must not be a symbolic link")
    token = path.read_text().strip()
    if len(token) < 24 or len(token) > 256 or any(c.isspace() for c in token):
        raise ValueError("invalid local ingestion token")
    return token


def summary_key(path: Path | None):
    """Read only the selected key; never execute dotenv content or print values."""
    value = os.environ.get("OPENROUTER_API_KEY", "")
    if path:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "r") as file:
            content = file.read(65537)
        if len(content) > 65536:
            raise ValueError("key source too large")
        value = ""
        for line in content.splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*?)\s*", line)
            if match:
                value = match[1].strip().strip("\"'")
                break
        if not value and content.strip().startswith("sk-or-") and "\n" not in content.strip():
            value = content.strip()
    if not value.startswith("sk-or-") or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("OpenRouter API key required for opt-in summaries")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local read-only agent dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start the loopback-only dashboard")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--data-dir", type=Path, default=Path(".agent-viz/local"))
    serve.add_argument(
        "--watch", action="store_true", help="observe discovered native profile files read-only"
    )
    serve.add_argument(
        "--no-discovery",
        action="store_true",
        help="SDK-only collector without native transcript discovery",
    )
    serve.add_argument("--config", type=Path, help="explicit local source profile configuration")
    serve.add_argument(
        "--demo", action="store_true", help="emit a clearly labeled model-free synthetic scenario"
    )
    serve.add_argument("--demo-delay", type=float, default=0.5)
    serve.add_argument("--stale-after", type=float, default=15)
    serve.add_argument("--limit-per-profile", type=int, default=5000)
    serve.add_argument(
        "--max-age-days",
        type=float,
        default=7,
        help="skip native transcripts not modified within N days; 0 reads all",
    )
    serve.add_argument(
        "--summaries",
        action="store_true",
        help="opt in to sending bounded redacted activity context to OpenRouter DS4F",
    )
    serve.add_argument(
        "--openrouter-key-file",
        type=Path,
        help="local key or dotenv file; key is never sent to the browser",
    )
    discover = sub.add_parser(
        "discover", help="list storage profile labels without opening credential files"
    )
    discover.add_argument(
        "--write-config",
        type=Path,
        help="create a private local profile config; refuses to overwrite",
    )
    discover.add_argument(
        "--label",
        action="append",
        help="select an exact discovered profile label; repeat for several",
    )
    demo = sub.add_parser("demo", help="send a synthetic scenario to an existing local collector")
    demo.add_argument("--url", default="http://127.0.0.1:8766")
    demo.add_argument("--data-dir", type=Path, default=Path(".agent-viz/local"))
    demo.add_argument("--delay", type=float, default=0.5)
    prune = sub.add_parser(
        "prune", help="delete old observer history only; never native transcripts"
    )
    prune.add_argument("--data-dir", type=Path, default=Path(".agent-viz/local"))
    prune.add_argument("--older-than-days", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            profiles = discover_profiles()
            if args.label:
                profiles = [p for p in profiles if p.label in args.label]
                if {p.label for p in profiles} != set(args.label):
                    raise ValueError("a selected label was not discovered")
            print(
                json.dumps(
                    {
                        "profiles": [
                            {"id": p.id, "kind": p.kind, "label": p.label} for p in profiles
                        ],
                        "note": "Storage labels are not verified signed-in account identities.",
                    },
                    indent=2,
                )
            )
            if args.write_config:
                write_private(
                    args.write_config,
                    json.dumps({"profiles": [asdict(p) for p in profiles]}, indent=2) + "\n",
                )
                print("Private source config created. Task text is disabled by default.")
            return 0
        if args.command == "demo":
            from .demo import run_demo

            result = run_demo(args.url, token_for(args.data_dir), args.delay)
            print(
                json.dumps(
                    {"runs": result["runs"], "delivered": result["delivered"], "synthetic": True}
                )
            )
            return 0 if result["delivered"] else 1
        if args.command == "prune":
            if args.older_than_days <= 0:
                raise ValueError("retention period must be positive")
            store = Store(args.data_dir / "events.sqlite3")
            try:
                print(json.dumps(store.prune(args.older_than_days)))
            finally:
                store.close()
            return 0
        if (
            not 0 <= args.port <= 65535
            or not math.isfinite(args.stale_after)
            or args.stale_after <= 0
            or not math.isfinite(args.demo_delay)
            or args.demo_delay < 0
            or not 1 <= args.limit_per_profile <= 5000
            or not math.isfinite(args.max_age_days)
            or args.max_age_days < 0
        ):
            raise ValueError("invalid port, freshness threshold, or profile limit")
        if args.demo and (args.watch or args.config or args.summaries):
            raise ValueError(
                "synthetic demo must stay isolated from native sources and paid summaries"
            )
        if args.no_discovery and (args.config or args.watch):
            raise ValueError("conflicting discovery settings")
        api_key = summary_key(args.openrouter_key_file) if args.summaries else None
        args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        store = Store(args.data_dir / "events.sqlite3", stale_after=args.stale_after)
        monitor = None
        if args.config or args.watch or (not args.no_discovery and not args.demo):
            from .monitor import Monitor

            profiles = load_profiles(args.config) if args.config else None
            monitor = Monitor(
                profiles,
                store,
                limit=args.limit_per_profile,
                max_age_days=args.max_age_days or None,
            )
            monitor.scan_once()
        summarizer = None
        if args.summaries:
            from .summaries import Summarizer

            summarizer = Summarizer(
                lambda: list(monitor.profiles) if monitor else [],
                store,
                api_key,
                state_path=args.data_dir / "summaries.sqlite3",
            )
        server = Collector(
            store, token_for(args.data_dir), port=args.port, monitor=monitor, summarizer=summarizer
        )
        print(f"Agent Viz local dashboard: {server.url}", flush=True)
        print("Read-only native observation; no login verification or remote control.", flush=True)
        if monitor:
            print(
                f"Observing {len(monitor.profiles)} storage profiles; "
                "private paths and content omitted from logs.",
                flush=True,
            )
            monitor.start()
        if summarizer:
            print(
                "DS4F summaries enabled: bounded context leaves this machine via OpenRouter; "
                "Claude recaps are used locally.",
                flush=True,
            )
            summarizer.start()
        if args.demo:
            from .demo import run_demo

            def emit_demo():
                try:
                    result = run_demo(server.url, server.token, args.demo_delay)
                    print(
                        f"Synthetic demo: {result['runs']} runs; delivered={result['delivered']}",
                        flush=True,
                    )
                except Exception:
                    print("Synthetic demo failed; check local delivery diagnostics.", flush=True)

            threading.Thread(target=emit_demo, name="agent-viz-demo", daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            if monitor:
                monitor.stop()
            if summarizer:
                summarizer.stop()
            server.stopping.set()
            server.server_close()
            store.close()
        return 0
    except (ValueError, OSError, TypeError):
        # Error strings from source files can contain private paths/content.
        print(
            "Unable to start or configure Agent Viz. Check local paths, port, and argument values."
        )
        return 1
