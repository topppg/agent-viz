"""The local Mac installer is exercised with a synthetic checkout and app."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "src" / "app" / "install.sh"


def executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    checkout = tmp_path / "checkout"
    app_scripts = checkout / "src" / "app"
    app_scripts.mkdir(parents=True)
    shutil.copy2(INSTALLER, app_scripts / "install.sh")
    executable(
        app_scripts / "build.sh",
        """#!/bin/sh
printf 'build\\n' >> "$FAKE_LOG"
if [ "${FAKE_BUILD_FAIL:-}" = 1 ]; then exit 1; fi
mkdir -p build/mac-app/Spiritboard.app/Contents
printf 'new app' > build/mac-app/Spiritboard.app/Contents/version
""",
    )

    home = tmp_path / "home"
    destination = home / "Applications" / "Spiritboard.app"
    (destination / "Contents").mkdir(parents=True)
    (destination / "Contents" / "version").write_text("old app")
    (destination / "stale-file").write_text("remove with old app")

    commands = tmp_path / "commands"
    commands.mkdir()
    executable(
        commands / "git",
        """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *' branch --show-current '*) printf '%s\\n' "${FAKE_GIT_BRANCH:-main}" ;;
  *' status --porcelain '*)
    if [ "${FAKE_GIT_DIRTY:-}" = 1 ]; then printf ' M tracked-file\\n'; fi ;;
esac
""",
    )
    executable(
        commands / "ditto",
        """#!/bin/sh
printf 'ditto %s %s\\n' "$1" "$2" >> "$FAKE_LOG"
if [ "${FAKE_DITTO_FAIL:-}" = 1 ]; then exit 1; fi
cp -R "$1" "$2"
""",
    )
    executable(
        commands / "pgrep",
        """#!/bin/sh
printf 'pgrep %s\\n' "$*" >> "$FAKE_LOG"
exit 1
""",
    )
    executable(
        commands / "open",
        """#!/bin/sh
printf 'open %s\\n' "$*" >> "$FAKE_LOG"
test -f "$1/Contents/version"
""",
    )
    executable(
        commands / "osascript",
        """#!/bin/sh
printf 'osascript %s\\n' "$*" >> "$FAKE_LOG"
""",
    )
    log = tmp_path / "commands.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{commands}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
    }
    return checkout, destination, log, env


def run_install(checkout: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "src/app/install.sh"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_updates_builds_replaces_and_opens_app(
    installation: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    checkout, destination, log, env = installation
    result = run_install(checkout, env)

    assert result.returncode == 0, result.stderr
    assert (destination / "Contents" / "version").read_text() == "new app"
    assert not (destination / "stale-file").exists()
    calls = log.read_text().splitlines()
    assert next(
        i for i, call in enumerate(calls) if "pull --ff-only origin main" in call
    ) < calls.index("build")
    assert calls[-1] == f"open {destination}"


@pytest.mark.parametrize("reason", ["dirty", "branch"])
def test_installer_refuses_checkout_before_pull_or_build(
    installation: tuple[Path, Path, Path, dict[str, str]], reason: str
) -> None:
    checkout, destination, log, env = installation
    env["FAKE_GIT_DIRTY" if reason == "dirty" else "FAKE_GIT_BRANCH"] = (
        "1" if reason == "dirty" else "feature"
    )

    result = run_install(checkout, env)

    assert result.returncode != 0
    assert (destination / "Contents" / "version").read_text() == "old app"
    calls = log.read_text()
    assert "pull --ff-only origin main" not in calls
    assert "build" not in calls
    assert "open" not in calls


def test_failed_build_preserves_installed_app(
    installation: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    checkout, destination, log, env = installation
    env["FAKE_BUILD_FAIL"] = "1"

    result = run_install(checkout, env)

    assert result.returncode != 0
    assert (destination / "Contents" / "version").read_text() == "old app"
    assert (destination / "stale-file").exists()
    assert not any(call.startswith("open ") for call in log.read_text().splitlines())


def test_failed_copy_preserves_installed_app(
    installation: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    checkout, destination, log, env = installation
    env["FAKE_DITTO_FAIL"] = "1"

    result = run_install(checkout, env)

    assert result.returncode != 0
    assert (destination / "Contents" / "version").read_text() == "old app"
    assert (destination / "stale-file").exists()
    assert not list(destination.parent.glob(".spiritboard-install.*"))
    assert not any(call.startswith("open ") for call in log.read_text().splitlines())
