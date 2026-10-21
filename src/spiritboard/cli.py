"""Local commands. Never modifies native account or transcript storage."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import webbrowser
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .harnesses import HARNESSES
from .launcher import Instance, LaunchError, default_data_dir, running_url
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


def open_dashboard(url: str, *, enabled: bool) -> None:
    if not enabled:
        return
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print("Browser unavailable; open the local dashboard URL above.", flush=True)


def serve_dashboard(args: argparse.Namespace) -> int:
    profiles = load_profiles(args.config) if args.config else None
    profile_settings = (
        [{**asdict(p), "root": str(Path(p.root).expanduser().resolve())} for p in profiles]
        if profiles is not None
        else None
    )
    discovery = profiles is None and not args.no_discovery and not args.demo
    settings = {
        "demo": args.demo,
        "no_discovery": args.no_discovery,
        "watch": args.watch,
        "config": str(args.config.resolve()) if args.config else None,
        "config_fingerprint": (
            hashlib.sha256(json.dumps(profile_settings, sort_keys=True).encode()).hexdigest()
            if profile_settings is not None
            else None
        ),
        "summaries": args.summaries,
        "key_file": str(args.openrouter_key_file.resolve()) if args.openrouter_key_file else None,
        "stale_after": args.stale_after,
        "demo_delay": args.demo_delay,
        "limit_per_profile": args.limit_per_profile,
        "max_age_days": args.max_age_days,
        # Discovery selectors only matter when discovery runs.
        **{h.env.lower(): os.environ.get(h.env) if discovery else None for h in HARNESSES.values()},
    }
    with ExitStack() as resources:
        instance = resources.enter_context(Instance(args.data_dir, settings))
        if not instance.owner:
            # Port 0 asks for any free port, so it never pins the running board's port.
            url = instance.existing_url(port=args.port or None)
            print(f"Spiritboard is already running: {url}", flush=True)
            open_dashboard(url, enabled=not args.no_open)
            return 0

        api_key = summary_key(args.openrouter_key_file) if args.summaries else None
        store = Store(args.data_dir / "events.sqlite3", stale_after=args.stale_after)
        resources.callback(store.close)
        # Bind before scanning any native sources or starting optional API calls.
        token = token_for(args.data_dir)
        try:
            server = Collector(store, token, port=8766 if args.port is None else args.port)
        except OSError as error:
            if args.port is not None or error.errno != errno.EADDRINUSE:
                raise
            server = Collector(store, token, port=0)
        resources.callback(server.server_close)
        resources.callback(server.stopping.set)
        # Serve and publish before the first scan, so a second launch can verify this board
        # within its startup wait even when the scan is slow.
        threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="spiritboard-http",
            daemon=True,
        ).start()
        resources.callback(server.shutdown)
        instance.publish(server.url, server.instance_id)
        monitor = None
        if args.config or args.watch or (not args.no_discovery and not args.demo):
            from .monitor import Monitor

            monitor = Monitor(
                profiles,
                store,
                limit=args.limit_per_profile,
                max_age_days=args.max_age_days or None,
            )
            server.monitor = monitor
            resources.callback(monitor.stop)
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
            server.summarizer = summarizer
            resources.callback(summarizer.stop)
        if monitor:
            monitor.start()
        if summarizer:
            summarizer.start()
        print(f"Spiritboard local dashboard: {server.url}", flush=True)
        print("Read-only native observation; Ctrl-C stops only this dashboard.", flush=True)
        if monitor:
            print(
                f"Observing {len(monitor.profiles)} storage profiles; "
                "private paths and content omitted from logs.",
                flush=True,
            )
        if summarizer:
            print(
                "DS4F summaries enabled: bounded context leaves this machine via OpenRouter; "
                "Claude recaps are used locally.",
                flush=True,
            )
        if args.demo:
            from .demo import run_demo

            def emit_demo() -> None:
                try:
                    result = run_demo(server.url, server.token, args.demo_delay)
                    print(
                        f"Synthetic demo: {result['runs']} runs; delivered={result['delivered']}",
                        flush=True,
                    )
                except Exception:
                    print("Synthetic demo failed; check local delivery diagnostics.", flush=True)

            threading.Thread(target=emit_demo, name="spiritboard-demo", daemon=True).start()
        threading.Thread(
            target=open_dashboard,
            args=(server.url,),
            kwargs={"enabled": not args.no_open},
            name="spiritboard-browser",
            daemon=True,
        ).start()
        try:
            while not server.stopping.wait(1):
                pass
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spiritboard",
        description="Local read-only agent dashboard. Run without a subcommand to start the board.",
        epilog="Use spiritboard serve --help for dashboard options.",
    )
    parser.add_argument("--version", action="version", version=f"Spiritboard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start the loopback-only dashboard")
    serve.add_argument("--port", type=int, help="local port; default 8766, or a free port if busy")
    serve.add_argument("--data-dir", type=Path, help="override the OS user-data directory")
    serve.add_argument(
        "--no-open", action="store_true", help="print the URL without opening a browser"
    )
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
    demo.add_argument("--url", help="collector URL; default: the dashboard running on --data-dir")
    demo.add_argument("--data-dir", type=Path)
    demo.add_argument("--delay", type=float, default=0.5)
    prune = sub.add_parser(
        "prune", help="delete old observer history only; never native transcripts"
    )
    prune.add_argument("--data-dir", type=Path)
    prune.add_argument("--older-than-days", type=float, required=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--version"}):
        argv.insert(0, "serve")
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
                print("Private source config created; task text is shown by default.")
            return 0
        if args.data_dir is None:
            args.data_dir = default_data_dir(demo=args.command == "serve" and args.demo)
        if args.command == "demo":
            from .demo import run_demo

            url = args.url or running_url(args.data_dir)
            result = run_demo(url, token_for(args.data_dir), args.delay)
            print(
                json.dumps(
                    {"runs": result["runs"], "delivered": result["delivered"], "synthetic": True}
                )
            )
            return 0 if result["delivered"] else 1
        if args.command == "prune":
            if not math.isfinite(args.older_than_days) or args.older_than_days <= 0:
                raise ValueError("retention period must be positive")
            history = args.data_dir / "events.sqlite3"
            if not history.is_file() and not (args.data_dir / "instance.lock").is_file():
                raise LaunchError("No dashboard history exists in that --data-dir.")
            with Instance(args.data_dir, {"operation": "prune"}) as instance:
                if not instance.owner:
                    raise LaunchError("Stop the dashboard before pruning its local history.")
                store = Store(args.data_dir / "events.sqlite3")
                try:
                    print(json.dumps(store.prune(args.older_than_days)))
                finally:
                    store.close()
            return 0
        if (
            (args.port is not None and not 0 <= args.port <= 65535)
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
        return serve_dashboard(args)
    except LaunchError as error:
        print(str(error))
        return 1
    except (ValueError, OSError, TypeError):
        # Error strings from source files can contain private paths/content.
        print(
            "Unable to start or configure Spiritboard. "
            "Check local paths, port, and argument values."
        )
        return 1
