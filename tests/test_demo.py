import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request

from spiritboard.demo import run_demo
from spiritboard.model import validate_event
from spiritboard.server import Collector
from spiritboard.store import Store


class DemoTests(unittest.TestCase):
    def test_synthetic_scenario_has_tree_recovery_and_unfinished_child(self):
        events = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                events.append(validate_event(event))
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_demo("http://127.0.0.1:" + str(server.server_port), "demo-token", delay=0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
        self.assertTrue(result["delivered"])
        self.assertEqual(result["runs"], 5)
        starts = [event for event in events if event["type"] == "run.started"]
        self.assertTrue(all(event["payload"]["demo"] for event in starts))
        self.assertTrue(all(event["payload"]["source"] == "demo" for event in starts))
        self.assertTrue(all(event["payload"]["account"] == "synthetic-demo" for event in starts))
        ids = result["run_ids"]
        nested = next(event for event in starts if event["run_id"] == ids["nested"])
        self.assertEqual(nested["parent_run_id"], ids["first"])
        finishes = [event for event in events if event["type"] == "run.finished"]
        self.assertNotIn(ids["stale"], [event["run_id"] for event in finishes])
        self.assertLess(
            next(
                i
                for i, event in enumerate(events)
                if event["type"] == "run.finished" and event["run_id"] == ids["root"]
            ),
            next(
                i
                for i, event in enumerate(events)
                if event["type"] == "run.finished" and event["run_id"] == ids["first"]
            ),
        )
        outcomes = [
            event["payload"]["outcome"] for event in events if event["type"] == "operation.finished"
        ]
        self.assertEqual(outcomes[:2], ["error", "success"])
        self.assertTrue(any(event["payload"].get("state") == "waiting_user" for event in events))

    def test_offline_demo_returns_delivery_failure_without_raising(self):
        result = run_demo("http://127.0.0.1:1", "demo-token", delay=0)
        self.assertFalse(result["delivered"])

    def test_negative_delay_rejected(self):
        with self.assertRaises(ValueError):
            run_demo("http://127.0.0.1:1", "demo-token", delay=-1)

    def test_real_collector_accepts_complete_sdk_demo(self):
        store = Store()
        collector = Collector(store, "synthetic-test-token-long-enough")
        thread = threading.Thread(target=collector.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_demo(collector.url, collector.token, delay=0)
            self.assertTrue(result["delivered"], result["diagnostics"])
            self.assertEqual(result["diagnostics"]["failed"], 0)
            with request.urlopen(collector.url + "/api/snapshot", timeout=2) as response:
                snapshot = json.load(response)
            self.assertEqual(snapshot["total_runs"], 5)
            rows = {row["run_id"]: row for row in snapshot["runs"]}
            for key in ("root", "first", "second", "nested"):
                self.assertEqual(rows[result["run_ids"][key]]["state"], "completed")
            self.assertEqual(rows[result["run_ids"]["stale"]]["state"], "working")
            self.assertEqual(rows[result["run_ids"]["nested"]]["operations"][0]["outcome"], "error")
        finally:
            collector.stop()
            thread.join(1)
            store.close()
