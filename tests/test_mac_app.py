"""The Mac app ships its own select menu because it may show an older dashboard."""

import shutil
import subprocess
from pathlib import Path

import pytest

MENU = Path(__file__).resolve().parents[1] / "src" / "app" / "select-menu.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is optional; syntax check skipped")
def test_select_menu_is_valid_javascript() -> None:
    subprocess.run([str(NODE), "--check", str(MENU)], check=True)
