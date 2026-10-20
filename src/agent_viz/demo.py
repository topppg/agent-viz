"""Invented, model-free scenario for exercising the local dashboard."""

import time

from .sdk import Client


def run_demo(url, token, delay=0.3):
    """Send a repeatable scenario, with fresh identities for each invocation.

    ``delay=0`` is useful in tests. No real files, tools, accounts, or models are
    accessed. One unfinished worker deliberately demonstrates stale detection.
    """
    if delay < 0:
        raise ValueError("delay must not be negative")
    client = Client(url, token, account="synthetic-demo", timeout=0.5)
    ids = {}

    def pause():
        if delay:
            time.sleep(delay)

    try:
        root = client.run(
            "Release rehearsal",
            "Review an invented change safely",
            source="demo",
            project="synthetic",
            demo=True,
        )
        ids["root"] = root.run_id
        root.activity("Splitting an invented review into independent checks")
        pause()
        first = root.child("Worker", "Inspect synthetic test results")
        second = root.child("Worker", "Review invented documentation")
        ids.update(first=first.run_id, second=second.run_id)
        first.activity("Reading synthetic test output")
        second.activity("Checking the example instructions")
        pause()
        nested = first.child("Retry check", "Recover from a simulated tool error")
        ids["nested"] = nested.run_id
        failed = nested.start_operation("tool", "Read invented test fixture")
        pause()
        nested.finish_operation(
            failed, "error", "Simulated temporary read failure; no tool was called"
        )
        nested.activity("Retrying the simulated read with a corrected fixture name")
        with nested.operation("tool", "Read corrected invented fixture"):
            pause()
        nested.finish(summary="Recovered from the simulated error")
        first.state("waiting_user", "Awaiting an invented approval; no real approval required")
        first.activity("Waiting for the demo approval event")
        pause()
        second.finish(summary="Example instructions checked")
        first.state("working", "Synthetic approval received")
        first.heartbeat()
        orphan = root.child(
            "Silent worker", "Deliberately stop sending events to demonstrate staleness"
        )
        ids["stale"] = orphan.run_id
        orphan.activity("This synthetic worker will intentionally stop reporting")
        root.finish(summary="Coordinator finished; child state remains independent")
        pause()
        first.activity("Completing its check after the coordinator finished")
        first.finish(summary="Synthetic checks finished")
        flushed = client.flush(3)
        return dict(
            run_ids=ids,
            runs=len(ids),
            intentionally_unfinished=ids["stale"],
            delivered=flushed,
            diagnostics=client.diagnostics,
        )
    finally:
        client.close(timeout=1)
