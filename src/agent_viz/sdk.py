"""Small, best-effort local event client. Importing this module makes no requests."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from urllib import error, parse, request


def _now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _local_url(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Collector URL must be a local HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Collector URL cannot contain credentials, query, or fragment")
    host = parsed.hostname.lower()
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        raise ValueError("Only loopback collectors are supported")
    # Reading .port also validates malformed port values.
    _ = parsed.port
    if parsed.path not in {"", "/"}:
        raise ValueError("Collector URL must not include a path")
    return url.rstrip("/")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Bounded background delivery; network failures never escape producer methods.

    Tokens are sent only to a loopback URL, redirects and environment proxies are
    disabled, and each event keeps its identity through at most three attempts.
    Queue overflow drops the new event instead of blocking the agent. Inspect
    ``diagnostics`` or ``flush()`` to detect incomplete delivery.
    """

    def __init__(self, url, token, account="local", session_id=None, queue_size=256, timeout=0.5):
        self.url = _local_url(url)
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("A nonempty collector token is required")
        if not isinstance(queue_size, int) or queue_size < 1 or timeout <= 0:
            raise ValueError("queue_size and timeout must be positive")
        self.account = account
        self.session_id = session_id or str(uuid.uuid4())
        self.producer_id = str(uuid.uuid4())
        self._token = token
        self._capacity = queue_size
        self._timeout = timeout
        self._sequence = 0
        self._queue = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._stats = dict(delivered=0, dropped=0, failed=0, retried=0)
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._thread = threading.Thread(
            target=self._deliver, name="agent-viz-delivery", daemon=True
        )
        self._thread.start()

    @property
    def diagnostics(self):
        with self._condition:
            return dict(self._stats, queued=len(self._queue), closed=self._closed)

    def _emit(self, run_id, event_type, payload, parent_run_id=None):
        with self._condition:
            if self._closed:
                return False
            self._sequence += 1
            event = dict(
                schema_version=1,
                event_id=str(uuid.uuid4()),
                session_id=self.session_id,
                run_id=run_id,
                producer_id=self.producer_id,
                sequence=self._sequence,
                occurred_at=_now(),
                type=event_type,
                payload=payload,
            )
            if parent_run_id:
                event["parent_run_id"] = parent_run_id
            try:
                encoded = json.dumps(event, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                self._stats["dropped"] += 1
                return False
            if len(encoded) > 32768:
                self._stats["dropped"] += 1
                return False
            if len(self._queue) >= self._capacity:
                self._stats["dropped"] += 1
                return False
            self._queue.append([encoded, 0])
            self._condition.notify_all()
            return True

    def _deliver(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed:
                    return
                item = self._queue[0]
                item[1] += 1
                if item[1] > 1:
                    self._stats["retried"] += 1
                req = request.Request(
                    self.url + "/api/events",
                    data=item[0],
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
            successful = False
            retryable = True
            try:
                with self._opener.open(req, timeout=self._timeout) as response:
                    successful = 200 <= response.status < 300
            except error.HTTPError as exc:
                retryable = exc.code >= 500 or exc.code in {408, 429}
                exc.close()
            except (OSError, error.URLError, ValueError):
                pass
            with self._condition:
                if successful:
                    self._stats["delivered"] += 1
                else:
                    self._stats["failed"] += 1
                if self._closed:
                    self._condition.notify_all()
                    return
                if successful or item[1] >= 3 or not retryable:
                    self._queue.popleft()
                    if not successful:
                        self._stats["dropped"] += 1
                else:
                    self._condition.wait(timeout=0.05 * item[1])
                self._condition.notify_all()

    def run(self, name, task="", parent=None, project=None, source="sdk", demo=False):
        if parent is not None and parent.client is not self:
            raise ValueError("Parent run must belong to the same client")
        return Run(self, name, task, parent, project, source, demo)

    def flush(self, timeout=2):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._queue and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return not self._queue and self._stats["dropped"] == 0

    def close(self, timeout=1):
        deadline = time.monotonic() + max(0, timeout)
        complete = self.flush(max(0, deadline - time.monotonic()))
        with self._condition:
            self._closed = True
            self._stats["dropped"] += len(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        self._thread.join(max(0, deadline - time.monotonic()))
        return complete

    async def async_flush(self, timeout=2):
        return await asyncio.to_thread(self.flush, timeout)

    async def async_close(self, timeout=1):
        return await asyncio.to_thread(self.close, timeout)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class Run:
    """One identity in an agent tree; equal display names never merge identities."""

    def __init__(self, client, name, task, parent, project, source, demo):
        self.client = client
        self.run_id = str(uuid.uuid4())
        self.parent_run_id = parent.run_id if parent else None
        self._project, self._source, self._demo = project, source, demo
        self._finished = False
        self._lock = threading.RLock()
        payload = dict(name=name, task=task, source=source, account=client.account, demo=demo)
        if project is not None:
            payload["project"] = project
        self._send("run.started", payload)

    def _send(self, event_type, payload):
        with self._lock:
            if self._finished:
                return False
            return self.client._emit(self.run_id, event_type, payload, self.parent_run_id)

    def child(self, name, task=""):
        return self.client.run(
            name, task, parent=self, project=self._project, source=self._source, demo=self._demo
        )

    def activity(self, text, provenance="application"):
        return self._send("run.activity", dict(text=text, provenance=provenance))

    def state(self, state, reason=""):
        return self._send("run.state_changed", dict(state=state, reason=reason))

    def heartbeat(self):
        return self._send("run.heartbeat", {})

    def start_operation(self, kind, label):
        operation_id = str(uuid.uuid4())
        self._send("operation.started", dict(operation_id=operation_id, kind=kind, label=label))
        return operation_id

    def finish_operation(self, operation_id, outcome="success", detail=""):
        return self._send(
            "operation.finished", dict(operation_id=operation_id, outcome=outcome, detail=detail)
        )

    def operation(self, kind, label):
        return Operation(self, kind, label)

    def finish(self, state="completed", summary=""):
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("Final state must be completed, failed, or cancelled")
        with self._lock:
            result = self._send("run.finished", dict(state=state, summary=summary))
            self._finished = True
            return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.finish(
            "failed" if exc_type else "completed", "Run raised an exception" if exc_type else ""
        )


class Operation:
    def __init__(self, run, kind, label):
        self.run, self.kind, self.label = run, kind, label
        self.operation_id = None

    def __enter__(self):
        self.operation_id = self.run.start_operation(self.kind, self.label)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.run.finish_operation(
            self.operation_id,
            "error" if exc_type else "success",
            "Operation raised an exception" if exc_type else "",
        )
