"""Capture promotional media using only temporary, invented SDK events."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path

from PIL import Image

from spiritboard.sdk import Client
from spiritboard.server import Collector
from spiritboard.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node", help="Node executable with Playwright available")
    args = parser.parse_args()
    output = ROOT / "docs/media"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spiritboard-media-") as temporary:
        frames = Path(temporary)
        store = Store(frames / "events.sqlite3", stale_after=120)
        server = Collector(store, secrets.token_urlsafe(32), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with Client(server.url, server.token, account="synthetic-demo") as client:
                root = client.run(
                    "Release rehearsal", "Review an invented change", source="SDK", demo=True
                )
                root.activity("Coordinating two independent checks")
                tests = root.child("Test worker", "Check the invented retry fixture")
                tests.activity("Running synthetic fixture checks")
                docs = root.child("Docs worker", "Review the invented quick start")
                docs.state("waiting_user", "Choose the wording for this invented example")
                docs.activity("Example approval needed · no real action")
                other = client.run(
                    "Search rehearsal", "Inspect invented search results", source="SDK", demo=True
                )
                other.activity("Comparing two synthetic result sets")
                if not client.flush(3):
                    raise RuntimeError("Synthetic SDK delivery failed")
                subprocess.run(
                    [args.node, str(ROOT / "scripts/capture_demo.cjs"), server.url, str(frames)],
                    check=True,
                    cwd=ROOT,
                    timeout=120,
                )
        finally:
            server.stopping.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            store.close()
        images = [Image.open(frame).convert("RGB") for frame in sorted(frames.glob("frame-*.png"))]
        if len(images) != 50:
            raise RuntimeError("Expected 50 captured frames")
        palette = images[0].quantize(colors=160)
        indexed = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
        path = output / "demo.gif"
        indexed[0].save(
            path,
            save_all=True,
            append_images=indexed[1:],
            duration=200,
            loop=0,
            optimize=True,
            disposal=1,
        )
        with Image.open(path) as result:
            duration = sum(
                result.seek(index) or result.info["duration"] for index in range(result.n_frames)
            )
            if result.size != (1280, 800) or duration != 10_000 or path.stat().st_size > 5_000_000:
                raise RuntimeError("GIF dimensions, duration, or size outside the media contract")
        social = output / "social-preview.png"
        with Image.open(social) as result:
            if result.size != (1280, 640) or social.stat().st_size > 1_000_000:
                raise RuntimeError("Social preview dimensions or size outside the media contract")
        print(f"{path.relative_to(ROOT)} · 1280×800 · 10 seconds · {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
