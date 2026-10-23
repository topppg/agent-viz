"""Loopback-only collector and read-only browser dashboard, using stdlib HTTP."""

from __future__ import annotations

import hmac
import json
import math
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import focus
from .harnesses import HARNESSES
from .model import MAX_EVENT_BYTES, parse_json, timestamp

STATIC = Path(__file__).parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/summary-settings.js": ("summary-settings.js", "text/javascript; charset=utf-8"),
    "/summary-settings.css": ("summary-settings.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/fonts/space-grotesk.woff2": ("fonts/space-grotesk.woff2", "font/woff2"),
    "/fonts/space-mono-400.woff2": ("fonts/space-mono-400.woff2", "font/woff2"),
    "/fonts/space-mono-700.woff2": ("fonts/space-mono-700.woff2", "font/woff2"),
}
FOCUS_ROUTE = re.compile(r"/api/runs/([^/]{1,768})/focus")
RENAME_ROUTE = re.compile(r"/api/runs/([^/]{1,768})/name")
DEFAULT_WINDOW = 600
MAX_WINDOW = 30 * 86400
# The smallest of these that keeps a chart window within 90 bars.
BUCKETS = (10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 21600, 28800)


class Collector(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a listening port; bind exclusively.
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self,
        store,
        token: str,
        port: int = 0,
        host: str = "127.0.0.1",
        monitor=None,
        summarizer=None,
        summary_providers=None,
        retention_days: float = 30,
    ):
        if host != "127.0.0.1":
            raise ValueError("only IPv4 loopback is supported")
        if len(token) < 24:
            raise ValueError("ingestion token must have at least 24 characters")
        self.store = store
        self.token = token
        self.monitor = monitor
        self.summarizer = summarizer
        self.summary_providers = summary_providers
        self.retention_days = retention_days
        self.stopping = threading.Event()
        self._activity: dict[int, tuple[float, dict]] = {}
        self.focus_lock = threading.Lock()
        self.instance_id = uuid.uuid4().hex
        super().__init__((host, port), Handler)

    def server_bind(self) -> None:
        # HTTPServer's reverse-DNS lookup can stall; this server only binds numeric loopback.
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def activity(self, window: int = DEFAULT_WINDOW) -> dict:
        if not 60 <= window <= MAX_WINDOW:
            raise ValueError("window out of range")
        # Every SSE client asks once a second; one aggregate per second is enough.
        ttl = 1 if window == DEFAULT_WINDOW else 10
        current = time.time()
        cached = self._activity.get(window)
        if cached is None or current - cached[0] >= ttl:
            if len(self._activity) > 8:
                self._activity = {}
            bucket = next(b for b in BUCKETS if window // b <= 90)
            cached = (current, self.store.activity(window, bucket))
            self._activity[window] = cached
        return cached[1]

    def moment(self, value: str) -> float:
        """A past instant within retention, as epoch seconds or ISO 8601."""
        try:
            at = float(value)
        except ValueError:
            at = timestamp(value)
        current = time.time()
        oldest = current - self.retention_days * 86400 if self.retention_days else 0
        if not math.isfinite(at) or not oldest <= at <= current + 1:
            raise ValueError("moment outside history")
        return min(at, current)

    def snapshot(self, at: float | None = None):
        if at is None:
            data = self.store.snapshot(limit=5000)
        else:
            data = self.store.snapshot_at(at, limit=5000)
        observed = (
            self.monitor.public_state()
            if self.monitor
            else {
                "profiles": [],
                "source_errors": {},
                "source_stats": [],
                "automatic_discovery": False,
            }
        )
        profiles = observed["profiles"]
        data["profiles"] = [
            {"id": p["id"], "label": p["label"], "kind": p["kind"]} for p in profiles
        ]
        errors = observed["source_errors"]
        data["source_errors"] = [
            {"label": next((p["label"] for p in profiles if p["id"] == key), key), "message": value}
            for key, value in errors.items()
        ]
        data["source_stats"] = observed["source_stats"]
        data["automatic_discovery"] = observed["automatic_discovery"]
        data["ai"] = (
            self.summarizer.public_state()
            if self.summarizer
            else {"enabled": False, "status": "disabled", "summaries": []}
        )
        summaries = {item["run_id"]: item for item in data["ai"].get("summaries", [])}
        for run in data["runs"]:
            if run["run_id"] in summaries:
                summary = summaries[run["run_id"]]
                # Today's summary text would misdescribe the past; keep only the title.
                run["ai_summary"] = summary if at is None else {"title": summary.get("title", "")}
        kinds = {p["id"]: p["kind"] for p in profiles}
        live = observed.get("live", {})
        for run in data["runs"]:
            profile_id = run["session_id"].removesuffix(":observed")
            if run.get("mode") != "observation" or profile_id not in kinds:
                continue
            if at is not None:
                run["live"] = None
                continue
            owners = live.get(profile_id)
            native = run["run_id"].removeprefix(profile_id + ":")
            # A subagent can run inside its parent's process (Claude).
            owner = HARNESSES[kinds[profile_id]].owner(native)
            run["live"] = None if owners is None else owner in owners
            # Unconfirmed whether the desktop app writes sessions/<pid>.json: never claim
            # a desktop session closed from a missing record, only from a positive check.
            if run["live"] is False and run.get("app"):
                run["live"] = None
        data["activity"] = self.activity() if at is None else self.store.activity(end=at)
        data["stale_after_seconds"] = self.store.stale_after
        data["mode"] = "local-observer"
        return data

    def focus_target(self, run_id: str) -> focus.Target:
        profiles = list(self.monitor.profiles) if self.monitor else []
        return focus.resolve(self.store.run(run_id), profiles)

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Spiritboard/0.1"

    def log_message(self, *_):
        # Never put private run IDs, source paths, or labels into HTTP access logs.
        pass

    def _safe_origin(self):
        if not self._safe_host():
            return False
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{value}" for value in allowed}:
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            return False
        return True

    def _safe_host(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        return host in allowed

    def _headers(self, content_type, length=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        if length is not None:
            self.send_header("Content-Length", str(length))

    def _json(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._safe_origin():
            self.close_connection = True
            self._json(403, {"error": "untrusted host or origin"})
            return
        if urlsplit(self.path).path.startswith("/api/summaries/"):
            self._summary_action()
            return
        route = FOCUS_ROUTE.fullmatch(urlsplit(self.path).path)
        if route:
            self._focus(unquote(route[1]))
            return
        route = RENAME_ROUTE.fullmatch(urlsplit(self.path).path)
        if route:
            self._rename(unquote(route[1]))
            return
        if self.path != "/api/events":
            self.close_connection = True
            self._json(404, {"error": "not found"})
            return
        authorization = self.headers.get("Authorization", "")
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")
        ):
            self.close_connection = True
            self._json(401, {"error": "ingestion token required"})
            return
        if (
            self.headers.get("Transfer-Encoding")
            or self.headers.get_content_type() != "application/json"
        ):
            self.close_connection = True
            self._json(400, {"error": "bounded JSON request required"})
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= MAX_EVENT_BYTES:
                raise ValueError("invalid size")
            self.connection.settimeout(3)
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("incomplete body")
            result = self.server.store.ingest(parse_json(data))
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            self.close_connection = True
            self._json(400, {"error": "invalid or conflicting event"})
            return
        self._json(200, result)

    def _summary_action(self) -> None:
        if not self.headers.get("Origin") or self.headers.get("X-Spiritboard") != "summaries":
            self.close_connection = True
            self._json(403, {"error": "same-origin dashboard request required"})
            return
        manager = self.server.summary_providers
        if manager is None:
            self.close_connection = True
            self._json(503, {"error": "summary settings are unavailable"})
            return
        try:
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get_content_type() != "application/json"
                or urlsplit(self.path).query
            ):
                raise ValueError
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= 4096:
                raise ValueError
            self.connection.settimeout(3)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            body = parse_json(raw)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, TypeError, OSError, RecursionError):
            self.close_connection = True
            self._json(400, {"error": "bounded JSON request required"})
            return
        path = urlsplit(self.path).path
        try:
            if path in {"/api/summaries/openrouter/connect", "/api/summaries/codex/connect"}:
                if body:
                    raise ValueError
                provider = path.split("/")[3]
                host = "localhost" if provider == "openrouter" else "127.0.0.1"
                callback = (
                    f"http://{host}:{self.server.server_port}/api/summaries/{provider}/callback"
                )
                result = manager.begin(provider, callback)
            elif path in {
                "/api/summaries/openrouter/key",
                "/api/summaries/anthropic/key",
                "/api/summaries/openai/key",
            } and set(body) == {"key"}:
                manager.set_key(body["key"], provider=path.split("/")[3])
                result = manager.public_state()
            elif path == "/api/summaries/provider" and set(body) == {"provider"}:
                manager.set_provider(body["provider"])
                result = manager.public_state()
            elif path == "/api/summaries/disconnect" and set(body) == {"provider"}:
                manager.disconnect(body["provider"])
                result = manager.public_state()
            elif path == "/api/summaries/enabled" and set(body) == {"enabled"}:
                manager.set_enabled(body["enabled"])
                result = manager.public_state()
            else:
                self._json(400, {"error": "invalid summary action"})
                return
        except Exception:
            # Never reflect provider authentication exception data to the browser.
            self._json(409, {"error": "summary action failed; check connection settings"})
            return
        self._json(200, result)

    def _summary_callback(self, parsed) -> None:
        if not self._safe_host():
            self._json(403, {"error": "untrusted host"})
            return
        manager = self.server.summary_providers
        if manager is None:
            self._json(503, {"error": "summary settings are unavailable"})
            return
        try:
            if len(parsed.query) > 8192:
                raise ValueError
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            if set(query) - {"state", "code", "error", "error_description", "client_id", "scope"}:
                raise ValueError
            if any(len(values) != 1 for values in query.values()):
                raise ValueError
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            if not state or (not code and "error" not in query):
                raise ValueError
            provider = parsed.path.split("/")[3]
            manager.complete(
                provider,
                state,
                code,
                client_id=query.get("client_id", [None])[0],
                error=query.get("error", [None])[0],
            )
        except Exception:
            self._json(
                400, {"error": "sign-in was not completed; start again from summary settings"}
            )
            return
        self.send_response(303)
        self._headers("text/plain; charset=utf-8", 0)
        self.send_header("Location", self.server.url)
        self.end_headers()

    def _rename(self, run_id: str):
        # A custom header forces a CORS preflight, which this server never grants.
        if not self.headers.get("Origin") or self.headers.get("X-Spiritboard") != "rename":
            self.close_connection = True
            self._json(403, {"error": "same-origin dashboard request required"})
            return
        try:
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get_content_type() != "application/json"
                or len(run_id) > 256
            ):
                raise ValueError
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= 1024:
                raise ValueError
            self.connection.settimeout(3)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            body = parse_json(raw)
            if not isinstance(body, dict) or set(body) != {"name"}:
                raise ValueError
            saved = self.server.store.rename(run_id, body["name"])
        except KeyError:
            self._json(404, {"error": "unknown run"})
            return
        except (ValueError, TypeError, OSError, RecursionError):
            self.close_connection = True
            self._json(400, {"error": "invalid name"})
            return
        self._json(200, {"ok": True, "local_name": saved})

    def _focus(self, run_id: str):
        # A custom header forces a CORS preflight, which this server never grants.
        if not self.headers.get("Origin") or self.headers.get("X-Spiritboard") != "focus":
            self.close_connection = True
            self._json(403, {"error": "same-origin dashboard request required"})
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self.close_connection = True
            self._json(400, {"error": "no request body expected"})
            return
        if len(run_id) > 256:
            self._json(400, {"error": "invalid run"})
            return
        with self.server.focus_lock:
            target = self.server.focus_target(run_id)
            if target.kind != "focus":
                self._json(409, {"error": "no running app to open"})
                return
            opened = focus.focus(target)
        if opened:
            self._json(200, {"ok": True})
        else:
            self._json(502, {"error": "could not open the app"})

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in {
            "/api/summaries/openrouter/callback",
            "/api/summaries/codex/callback",
        }:
            # Only these state-validated loopback callbacks accept cross-site navigation.
            self._summary_callback(parsed)
            return
        if not self._safe_origin():
            self._json(403, {"error": "untrusted host or origin"})
            return
        path = parsed.path
        route = FOCUS_ROUTE.fullmatch(path)
        if route:
            run_id = unquote(route[1])
            if len(run_id) > 256:
                self._json(400, {"error": "invalid run"})
                return
            self._json(200, self.server.focus_target(run_id).public())
        elif path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "spiritboard",
                    "version": 1,
                    "instance_id": self.server.instance_id,
                },
            )
        elif path == "/api/snapshot":
            try:
                at = self._query(parsed.query, "at")
                data = self.server.snapshot(None if at is None else self.server.moment(at))
            except ValueError:
                self._json(400, {"error": "at must be a past moment within retention"})
                return
            self._json(200, data)
        elif path == "/api/activity":
            try:
                window = self._query(parsed.query, "window")
                data = self.server.activity(DEFAULT_WINDOW if window is None else int(window))
            except ValueError:
                self._json(400, {"error": f"window must be 60 to {MAX_WINDOW} seconds"})
                return
            self._json(200, data)
        elif path == "/api/summaries/settings":
            manager = self.server.summary_providers
            if manager is None:
                self._json(503, {"error": "summary settings are unavailable"})
            else:
                self._json(200, manager.public_state())
        elif path == "/api/events":
            self._stream(parsed.query)
        elif path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = unquote(path[len("/api/runs/") : -len("/events")])
            query = parse_qs(parsed.query)
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
                if after >= 2**53:
                    raise ValueError("cursor too large")
                limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
                if not run_id or len(run_id) > 256:
                    raise ValueError("invalid run")
                data = self.server.store.events(run_id, after, limit)
            except ValueError:
                self._json(400, {"error": "invalid event query"})
                return
            self._json(200, data)
        elif path in ASSETS:
            name, content_type = ASSETS[path]
            try:
                data = (STATIC / name).read_bytes()
            except OSError:
                self._json(503, {"error": "dashboard assets unavailable"})
                return
            self.send_response(200)
            self._headers(content_type, len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

    def _query(self, query_string: str, key: str) -> str | None:
        query = parse_qs(query_string, keep_blank_values=True, max_num_fields=2)
        if set(query) - {key} or any(len(v) != 1 or len(v[0]) > 64 for v in query.values()):
            raise ValueError("invalid query")
        return query[key][0] if key in query else None

    def _stream(self, query_string):
        query = parse_qs(query_string)
        try:
            after = int(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0])
            if not 0 <= after < 2**53:
                raise ValueError("invalid cursor range")
        except ValueError:
            self._json(400, {"error": "invalid cursor"})
            return
        self.send_response(200)
        self._headers("text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(5)
        try:
            initial = True
            while not self.server.stopping.is_set():
                data = self.server.snapshot()
                # Always deliver a complete snapshot. No partial-delta loss on
                # reconnect; a cursor from another/cleared store resets explicitly.
                kind = "reset" if initial and after > data["cursor"] else "snapshot"
                body = json.dumps(data, allow_nan=False)
                packet = f"id: {data['cursor']}\nevent: {kind}\ndata: {body}\n\n"
                self.wfile.write(packet.encode())
                self.wfile.flush()
                initial = False
                if self.server.stopping.wait(1):
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
