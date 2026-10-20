import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_viz.sdk import Client


class CaptureServer:
    def __init__(self, statuses=None, redirect=None):
        self.events = []
        self.headers = []
        self.statuses = list(statuses or [])
        self.redirect = redirect
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.events.append(json.loads(body))
                owner.headers.append(dict(self.headers))
                status = owner.statuses.pop(0) if owner.statuses else 202
                self.send_response(status)
                if owner.redirect:
                    self.send_header("Location", owner.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


class SDKTests(unittest.TestCase):
    def test_tree_same_names_and_context_lifecycle(self):
        with CaptureServer() as server:
            with Client(server.url, "test-token", account="account-a") as client:
                with client.run("same", "Invented task") as root:
                    child = root.child("same", "Another invented task")
                    self.assertNotEqual(root.run_id, child.run_id)
                    child.activity("Checking an example")
                    child.state("waiting_user", "Example approval")
                    child.heartbeat()
                    with child.operation("tool", "Example read"):
                        pass
                    child.finish(summary="Done")
                    self.assertFalse(child.activity("Too late"))
                self.assertTrue(client.flush())
                self.assertEqual(client.diagnostics["failed"], 0)
            events = server.events
            self.assertEqual(
                [event["sequence"] for event in events], list(range(1, len(events) + 1))
            )
            self.assertEqual(len({event["event_id"] for event in events}), len(events))
            self.assertEqual(len({event["producer_id"] for event in events}), 1)
            self.assertTrue(all(event["schema_version"] == 1 for event in events))
            self.assertEqual(events[1]["parent_run_id"], root.run_id)
            self.assertEqual(events[1]["payload"]["account"], "account-a")
            self.assertEqual(events[-1]["payload"]["state"], "completed")
            self.assertEqual(server.headers[0]["Authorization"], "Bearer test-token")

    def test_retry_preserves_event_identity(self):
        with CaptureServer([503, 202]) as server:
            client = Client(server.url, "test-token")
            client.run("One")
            self.assertTrue(client.flush())
            self.assertEqual(server.events[0], server.events[1])
            self.assertEqual(client.diagnostics["retried"], 1)
            self.assertEqual(client.diagnostics["failed"], 1)
            self.assertEqual(client.diagnostics["delivered"], 1)
            client.close()

    def test_offline_queue_is_bounded_and_does_not_raise(self):
        # A held lock makes capacity assertions independent of sender scheduling.
        client = Client("http://127.0.0.1:1", "test-token", queue_size=2, timeout=0.05)
        with client._condition:
            run = client.run("Offline")
            self.assertTrue(run.activity("One"))
            self.assertFalse(run.activity("Overflow"))
            self.assertEqual(client.diagnostics["queued"], 2)
            self.assertEqual(client.diagnostics["dropped"], 1)
        self.assertFalse(client.flush(2))
        self.assertEqual(client.diagnostics["queued"], 0)
        self.assertEqual(client.diagnostics["failed"], 6)
        self.assertEqual(client.diagnostics["retried"], 4)
        self.assertFalse(client.close())

    def test_close_prevents_new_network_delivery(self):
        with CaptureServer() as server:
            client = Client(server.url, "test-token")
            run = client.run("Closed")
            self.assertTrue(client.flush())
            self.assertTrue(client.close())
            before = len(server.events)
            self.assertFalse(run.activity("After close"))
            client.run("After close")
            time.sleep(0.05)
            self.assertEqual(len(server.events), before)

    def test_redirect_never_forwards_token(self):
        with CaptureServer() as destination:
            with CaptureServer([302], redirect=destination.url + "/api/events") as server:
                client = Client(server.url, "test-token")
                client.run("Redirect test")
                self.assertFalse(client.flush())
                client.close()
                self.assertEqual(len(server.events), 1)
                self.assertEqual(destination.events, [])

    def test_exception_context_records_failure_without_swallowing(self):
        with CaptureServer() as server:
            with Client(server.url, "test-token") as client:
                with self.assertRaisesRegex(RuntimeError, "original"):
                    with client.run("Example") as run:
                        with run.operation("tool", "Example read"):
                            raise RuntimeError("original")
                self.assertTrue(client.flush())
            self.assertEqual(server.events[-2]["payload"]["outcome"], "error")
            self.assertEqual(server.events[-1]["payload"]["state"], "failed")

    def test_async_flush_and_close(self):
        with CaptureServer() as server:
            client = Client(server.url, "test-token")
            client.run("Async")

            async def check():
                self.assertTrue(await client.async_flush())
                self.assertTrue(await client.async_close())

            asyncio.run(check())

    def test_invalid_collector_urls_are_rejected(self):
        for url in [
            "https://example.com",
            "http://127.0.0.1@evil.test",
            "file:///tmp/test",
            "http://localhost/path",
            "http://localhost?token=x",
            "http://localhost#x",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Client(url, "test-token")

    def test_oversized_event_drops_without_network(self):
        client = Client("http://127.0.0.1:1", "test-token", timeout=0.05)
        with client._condition:
            run = client.run("Example")
            self.assertFalse(run.activity("x" * 40000))
            self.assertEqual(client.diagnostics["dropped"], 1)
        client.close(timeout=0)
