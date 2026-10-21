"""Loopback-only collector and read-only browser dashboard, using stdlib HTTP."""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import focus
from .model import MAX_EVENT_BYTES, parse_json

STATIC = Path(__file__).parent / "static"
FOCUS_ROUTE = re.compile(r"/api/runs/([^/]{1,768})/focus")


class Collector(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        store,
        token: str,
        port: int = 0,
        host: str = "127.0.0.1",
        monitor=None,
        summarizer=None,
    ):
        if host != "127.0.0.1":
            raise ValueError("only IPv4 loopback is supported")
        if len(token) < 24:
            raise ValueError("ingestion token must have at least 24 characters")
        self.store = store
        self.token = token
        self.monitor = monitor
        self.summarizer = summarizer
        self.stopping = threading.Event()
        self._activity: tuple[int, dict] | None = None
        self.focus_lock = threading.Lock()
        super().__init__((host, port), Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def snapshot(self):
        data = self.store.snapshot(limit=5000)
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
                run["ai_summary"] = summaries[run["run_id"]]
        # Every SSE client asks once a second; one aggregate per second is enough.
        now_second = int(time.time())
        if self._activity is None or self._activity[0] != now_second:
            self._activity = (now_second, self.store.activity())
        data["activity"] = self._activity[1]
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
    server_version = "AgentViz/0.1"

    def log_message(self, *_):
        # Never put private run IDs, source paths, or labels into HTTP access logs.
        pass

    def _safe_origin(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{value}" for value in allowed}:
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            return False
        return True

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
        route = FOCUS_ROUTE.fullmatch(urlsplit(self.path).path)
        if route:
            self._focus(unquote(route[1]))
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

    def _focus(self, run_id: str):
        # A custom header forces a CORS preflight, which this server never grants.
        if not self.headers.get("Origin") or self.headers.get("X-Agent-Viz") != "focus":
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
        if not self._safe_origin():
            self._json(403, {"error": "untrusted host or origin"})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        route = FOCUS_ROUTE.fullmatch(path)
        if route:
            run_id = unquote(route[1])
            if len(run_id) > 256:
                self._json(400, {"error": "invalid run"})
                return
            self._json(200, self.server.focus_target(run_id).public())
        elif path == "/api/health":
            self._json(200, {"ok": True, "service": "agent-viz", "version": 1})
        elif path == "/api/snapshot":
            self._json(200, self.server.snapshot())
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
        elif path in {"/", "/app.js", "/style.css"}:
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            content_types = {
                "index.html": "text/html; charset=utf-8",
                "app.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8",
            }
            try:
                data = (STATIC / name).read_bytes()
            except OSError:
                self._json(503, {"error": "dashboard assets unavailable"})
                return
            self.send_response(200)
            self._headers(content_types[name], len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

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
