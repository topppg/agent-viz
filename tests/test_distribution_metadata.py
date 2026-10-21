"""Release descriptions must work on package indexes as well as GitHub."""

import ast
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_readme_links_and_images_are_absolute_or_local_anchors() -> None:
    readme = (ROOT / "README.md").read_text()
    targets = re.findall(r"!?\[[^\]\n]+\]\(([^)\n]+)\)", readme)
    assert targets
    assert all(target.startswith("#") or urlsplit(target).scheme == "https" for target in targets)
    images = re.findall(r"!\[[^\]\n]+\]\(([^)\n]+)\)", readme)
    assert len(images) >= 2
    assert all(urlsplit(target).netloc == "raw.githubusercontent.com" for target in images)


def test_readme_does_not_freeze_a_temporary_unpublished_status() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "A PyPI release is not published yet" not in readme
    assert "If no release is listed yet" in readme
    assert "uvx spiritboard" in readme


def test_package_and_command_version_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    source = ast.parse((ROOT / "src/spiritboard/__init__.py").read_text())
    versions = [
        ast.literal_eval(node.value)
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    assert versions == [project["project"]["version"]]
