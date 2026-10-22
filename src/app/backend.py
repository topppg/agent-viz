"""Entry point for the bundled macOS dashboard server."""

from __future__ import annotations

import http.client
import json
import re

from spiritboard.cli import main

_STANDARD_URL = "http://127.0.0.1:8766"


def running_standard_dashboard() -> str | None:
    connection = http.client.HTTPConnection("127.0.0.1", 8766, timeout=0.5)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(4097)
        if len(body) > 4096:
            return None
        health = json.loads(body)
        if (
            isinstance(health, dict)
            and health.get("ok") is True
            and health.get("service") == "spiritboard"
            and isinstance(health.get("instance_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", health["instance_id"])
        ):
            return _STANDARD_URL
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return None


if __name__ == "__main__":
    running = running_standard_dashboard()
    if running:
        print(f"Spiritboard is already running: {running}", flush=True)
    else:
        raise SystemExit(main(["serve", "--no-open", "--port", "0"]))
