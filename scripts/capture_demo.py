"""Capture a busy, synthetic dashboard as a matching video and looping GIF."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import imageio_ffmpeg
from demo_scene import seed_scene
from PIL import Image

from spiritboard.server import Collector
from spiritboard.store import Store

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1440, 1000
FRAMES, FPS = 100, 10


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node", help="Node executable with Playwright available")
    args = parser.parse_args()
    output = ROOT / "docs/media"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spiritboard-media-") as temporary:
        frames = Path(temporary)
        store = Store(frames / "events.sqlite3", stale_after=120)
        scene = seed_scene(store, time.time())
        server = Collector(store, secrets.token_urlsafe(32), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stopped = threading.Event()

        def animate() -> None:
            step = 0
            while not stopped.wait(0.5):
                scene.tick(step, time.time())
                step += 1

        updater = threading.Thread(target=animate, daemon=True)
        updater.start()
        try:
            subprocess.run(
                [args.node, str(ROOT / "scripts/capture_demo.cjs"), server.url, str(frames)],
                check=True,
                cwd=ROOT,
                timeout=120,
            )
        finally:
            stopped.set()
            updater.join(timeout=3)
            server.stopping.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            store.close()
        captured = sorted(frames.glob("frame-*.png"))
        if len(captured) != FRAMES:
            raise RuntimeError(f"Expected {FRAMES} captured frames")
        movie = output / "demo.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                str(FPS),
                "-i",
                str(frames / "frame-%03d.png"),
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(movie),
            ],
            check=True,
            timeout=120,
        )
        video_frames = imageio_ffmpeg.read_frames(str(movie), pix_fmt="rgb24")
        try:
            metadata = next(video_frames)
            count = sum(1 for _ in video_frames)
        finally:
            video_frames.close()
        if metadata["size"] != (WIDTH, HEIGHT) or count != FRAMES:
            raise RuntimeError("Video dimensions or frame count outside the media contract")
        if abs(metadata["duration"] - 10) > 0.05 or metadata["fps"] != FPS:
            raise RuntimeError("Video must play for ten seconds")
        if metadata["codec"] != "h264" or not metadata["pix_fmt"].startswith("yuv420p"):
            raise RuntimeError("Video must use the shareable H.264/yuv420p format")
        images = []
        for path in captured[::2]:
            with Image.open(path) as frame:
                images.append(frame.convert("RGB"))
        palette = images[0].quantize(colors=128)
        indexed = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
        gif = output / "demo.gif"
        indexed[0].save(
            gif,
            save_all=True,
            append_images=indexed[1:],
            duration=200,
            loop=0,
            optimize=True,
            disposal=1,
        )
        with Image.open(gif) as result:
            duration = sum(
                result.seek(index) or result.info["duration"] for index in range(result.n_frames)
            )
            if result.size != (WIDTH, HEIGHT) or duration != 10_000:
                raise RuntimeError("GIF dimensions or duration outside the media contract")
            if gif.stat().st_size > 5_000_000:
                raise RuntimeError("GIF exceeds the 5 MB media limit")
        for path in (gif, movie):
            print(f"{path.relative_to(ROOT)} · {WIDTH}×{HEIGHT} · 10 s · {path.stat().st_size:,} B")


if __name__ == "__main__":
    main()
