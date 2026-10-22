"""The Mac app ships its own select menu because it may show an older dashboard."""

import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "src" / "app"
MENU = APP / "select-menu.js"
NODE = shutil.which("node")


def test_select_menu_is_bundled_and_injected() -> None:
    assert "cp src/app/select-menu.js" in (APP / "build.sh").read_text()
    assert (
        'forResource: "select-menu", withExtension: "js"' in (APP / "Spiritboard.swift").read_text()
    )


def test_select_menu_styles_avoid_style_elements_blocked_by_csp() -> None:
    source = MENU.read_text()
    assert 'createElement("style")' not in source
    assert "adoptedStyleSheets" in source


@pytest.mark.skipif(NODE is None, reason="Node is optional; syntax check skipped")
def test_select_menu_is_valid_javascript() -> None:
    subprocess.run([str(NODE), "--check", str(MENU)], check=True)
