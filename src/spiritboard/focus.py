"""Find the app that owns a live native session and bring its window forward.

Activation only: nothing here types into, signals or otherwise controls an agent.
Resume commands are returned for the user to run; they are never executed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import processes
from .harnesses import HARNESSES
from .harnesses.harness import SESSION_ID
from .processes import Process
from .sources import SourceProfile

MAX_PARENTS = 16
TTY = re.compile(r"ttys?\d{1,4}|pts/\d{1,4}")
_PATH = r"/[A-Za-z0-9_./@+-]{1,255}"
_ID = r"[A-Za-z0-9][A-Za-z0-9-]{0,63}"
# Only these environment values are ever extracted; everything else is discarded.
ENV_KEYS = {
    "CMUX_WORKSPACE_ID": re.compile(_ID),
    "CMUX_PANEL_ID": re.compile(_ID),
    "CMUX_SOCKET_PATH": re.compile(_PATH),
    "TMUX": re.compile(_PATH + r",\d{1,10},\d{1,10}"),
    "TMUX_PANE": re.compile(r"%\d{1,6}"),
    "TERM_PROGRAM": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"),
    "__CFBundleIdentifier": re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}(\.[A-Za-z0-9-]{1,63}){1,6}"),
    "ITERM_SESSION_ID": re.compile(r"w\d{1,4}t\d{1,4}p\d{1,4}:([0-9A-Fa-f-]{36})"),
    "WINDOWID": re.compile(r"\d{1,12}"),
}
APP_NAMES = {
    "com.microsoft.VSCode": "VS Code",
    "com.microsoft.VSCodeInsiders": "VS Code",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "com.googlecode.iterm2": "iTerm",
    "com.apple.Terminal": "Terminal",
    "com.mitchellh.ghostty": "Ghostty",
    "com.github.wez.wezterm": "WezTerm",
    "net.kovidgoyal.kitty": "kitty",
    "org.alacritty": "Alacritty",
    "dev.warp.Warp-Stable": "Warp",
    "com.openai.chat": "ChatGPT",
    "com.anthropic.claudefordesktop": "Claude",
}
TERM_NAMES = {
    "vscode": "VS Code",
    "iTerm.app": "iTerm",
    "Apple_Terminal": "Terminal",
    "WezTerm": "WezTerm",
    "ghostty": "Ghostty",
}
ITERM_SCRIPT = """on run argv
  set wanted to item 1 of argv
  set wantedTty to item 2 of argv
  tell application id "com.googlecode.iterm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (wanted is not "" and (id of s) is wanted) or ¬
            (wantedTty is not "" and (tty of s) is wantedTty) then
            select w
            tell t to select
            tell s to select
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  error "not found"
end run"""
TERMINAL_SCRIPT = """on run argv
  set wantedTty to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is wantedTty then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  error "not found"
end run"""


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class Target:
    kind: str
    label: str = ""
    command: str = ""
    steps: tuple[Step, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    def public(self) -> dict[str, str | bool]:
        """Browser view: never argv, paths of helpers, or environment values."""
        data: dict[str, str | bool] = {
            "available": self.kind != "unsupported",
            "kind": self.kind,
            "label": self.label,
        }
        if self.kind == "resume":
            data["command"] = self.command
        return data


UNSUPPORTED = Target("unsupported")


def _environment(pid: int) -> dict[str, str]:
    pairs: list[tuple[str, str]] = []
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/environ", "rb") as file:
                raw = file.read(1 << 20).decode(errors="replace")
        except OSError:
            return {}
        pairs = [
            (key, value)
            for key, _, value in (item.partition("=") for item in raw.split("\0"))
            if key in ENV_KEYS
        ]
    else:
        args = processes.run(["ps", "-ww", "-o", "command=", "-p", str(pid)])
        full = processes.run(["ps", "eww", "-o", "command=", "-p", str(pid)])
        if not args or not full or args[0] != 0 or full[0] != 0:
            return {}
        command, output = args[1].strip(), full[1].strip()
        # ps eww prints the arguments, then the environment; never parse the arguments.
        if not command or not output.startswith(command):
            return {}
        pairs = [
            (key, value)
            for key, value in re.findall(
                r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(\S*)", output[len(command) :]
            )
            if key in ENV_KEYS
        ]
    last = dict(pairs)
    return {key: value for key, value in last.items() if ENV_KEYS[key].fullmatch(value)}


def _lineage(pid: int) -> list[Process]:
    chain: list[Process] = []
    current, seen = pid, set()
    while current > 1 and current not in seen and len(chain) < MAX_PARENTS:
        seen.add(current)
        try:
            process = processes.process(current)
        except OSError:
            break
        if process is None:
            break
        chain.append(process)
        current = process.ppid
    return chain


def _app_bundle(chain: list[Process]) -> Path | None:
    for process in chain:
        match = re.match(r"(/.+?\.app)/Contents/", process.command)
        if match and Path(match[1]).is_dir():
            return Path(match[1])
    return None


def _app_label(app: Path | None) -> str:
    name = app.stem if app else ""
    if name in {"Code", "Visual Studio Code"}:
        return "VS Code"
    return name if re.fullmatch(r"[\w .+-]{1,40}", name) else ""


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def live_sessions(profile: SourceProfile) -> frozenset[str] | None:
    """Native session ids a live local process owns; None when that cannot be told."""
    return HARNESSES[profile.kind].live_sessions(Path(profile.root).expanduser())


def _activation(env: dict[str, str], app: Path | None) -> tuple[Step, str] | None:
    bundle = env.get("__CFBundleIdentifier")
    term = TERM_NAMES.get(env.get("TERM_PROGRAM", ""), "")
    if bundle:
        name = APP_NAMES.get(bundle) or _app_label(app) or term or "app"
        return Step(("/usr/bin/open", "-b", bundle), required=False), name
    if app:
        return Step(("/usr/bin/open", "-a", str(app)), required=False), _app_label(app) or "app"
    return None


def _cmux_cli(app: Path | None) -> str | None:
    bundled = [app / "Contents/Resources/bin/cmux"] if app else []
    bundled.append(Path("/Applications/cmux.app/Contents/Resources/bin/cmux"))
    for path in bundled:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("cmux")


def _tmux_steps(env: dict[str, str], required: bool) -> tuple[Step, ...]:
    tmux = shutil.which("tmux")
    socket = env.get("TMUX", "").split(",")[0]
    pane = env.get("TMUX_PANE", "")
    if not tmux or not pane or not _is_socket(socket):
        return ()
    base = (tmux, "-S", socket)
    return (
        Step((*base, "switch-client", "-t", pane), required=False),
        Step((*base, "select-window", "-t", pane), required=required),
        Step((*base, "select-pane", "-t", pane), required=required),
    )


def _window_step(env: dict[str, str]) -> Step | None:
    wmctrl = shutil.which("wmctrl")
    if not wmctrl or "WINDOWID" not in env:
        return None
    return Step((wmctrl, "-i", "-a", hex(int(env["WINDOWID"]))))


def adapter(pid: int, chain: list[Process] | None = None) -> Target:
    """Choose the most specific way to show the terminal or app hosting ``pid``."""
    chain = _lineage(pid) if chain is None else chain
    if not chain:
        return UNSUPPORTED
    env = _environment(pid)
    mac = sys.platform == "darwin"
    app = _app_bundle(chain) if mac else None
    tty = chain[0].tty if TTY.fullmatch(chain[0].tty) else ""
    activate = _activation(env, app) if mac else None
    tail = (activate[0],) if activate else ()
    workspace, panel = env.get("CMUX_WORKSPACE_ID"), env.get("CMUX_PANEL_ID")
    cli = _cmux_cli(app) if workspace and panel else None
    if workspace and panel and cli:
        steps = (
            Step((cli, "select-workspace", "--workspace", workspace)),
            Step((cli, "focus-panel", "--panel", panel, "--workspace", workspace)),
            *_tmux_steps(env, required=False),
            *tail,
        )
        socket = env.get("CMUX_SOCKET_PATH", "")
        extra = (("CMUX_SOCKET_PATH", socket),) if _is_socket(socket) else ()
        return Target("focus", "cmux", steps=steps, env=extra)
    window = _window_step(env) if not mac else None
    tmux = _tmux_steps(env, required=True)
    if tmux:
        if window:
            tail = (Step(window.argv, required=False),)
        return Target("focus", "tmux", steps=(*tmux, *tail))
    if mac and ("ITERM_SESSION_ID" in env or env.get("TERM_PROGRAM") == "iTerm.app"):
        match = ENV_KEYS["ITERM_SESSION_ID"].fullmatch(env.get("ITERM_SESSION_ID", ""))
        guid = match[1] if match else ""
        if guid or tty:
            argv = ("/usr/bin/osascript", "-e", ITERM_SCRIPT, guid, f"/dev/{tty}" if tty else "")
            return Target("focus", "iTerm", steps=(Step(argv),))
    if mac and env.get("TERM_PROGRAM") == "Apple_Terminal" and tty:
        argv = ("/usr/bin/osascript", "-e", TERMINAL_SCRIPT, f"/dev/{tty}")
        return Target("focus", "Terminal", steps=(Step(argv),))
    if activate:
        return Target("focus", activate[1], steps=(Step(activate[0].argv),))
    if window:
        return Target("focus", "window", steps=(window,))
    return UNSUPPORTED


def resolve(run: dict[str, object] | None, profiles: list[SourceProfile]) -> Target:
    """Focus target for a native observation run; resume only when nothing is live."""
    if not run or run.get("mode") != "observation":
        return UNSUPPORTED
    source, native = run.get("source"), run.get("native_id")
    profile = next((p for p in profiles if run.get("session_id") == f"{p.id}:observed"), None)
    if profile is None or profile.kind != source or not isinstance(native, str):
        return UNSUPPORTED
    harness = HARNESSES[profile.kind]
    session = harness.owner(native)
    if not SESSION_ID.fullmatch(session):
        return UNSUPPORTED
    root = Path(profile.root).expanduser().resolve()
    try:
        known, pid = harness.session_pid(root, session)
        if pid:
            chain = _lineage(pid)
            app = run.get("app")
            marker = app if isinstance(app, str) else ""
            opened = harness.open_app(session, marker, chain) if harness.open_app else None
            if opened:
                return Target("focus", opened[0], steps=(Step(opened[1]),))
            return adapter(pid, chain)
        opened = harness.open_app(session, "", []) if harness.remote and harness.open_app else None
        if opened:
            return Target("focus", opened[0], steps=(Step(opened[1]),))
        # Resuming a session that is still running elsewhere would fork it.
        if not known or session != native or run.get("parent_run_id"):
            return UNSUPPORTED
        command = harness.resume(root, session)
        return Target("resume", "resume", command=command) if command else UNSUPPORTED
    except (OSError, ValueError, RuntimeError):
        return UNSUPPORTED


def focus(target: Target) -> bool:
    if target.kind != "focus" or not target.steps:
        return False
    env = {**os.environ, **dict(target.env)} if target.env else None
    for step in target.steps:
        result = processes.run(list(step.argv), env)
        if step.required and (result is None or result[0] != 0):
            return False
    return True
