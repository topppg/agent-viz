"""Find the app that owns a live native session and bring its window forward.

Activation only: nothing here types into, signals or otherwise controls an agent.
Resume commands are returned for the user to run; they are never executed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .sources import SourceProfile, _records

TIMEOUT = 3.0
MAX_PARENTS = 16
MAX_WALK = 50_000
MAX_SESSION_FILE = 64 * 1024
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,79}")
PID_FILE = re.compile(r"\d{1,10}\.json")
PS_LINE = re.compile(
    r"\s*(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.+)"
)
TTY = re.compile(r"ttys?\d{1,4}|pts/\d{1,4}")
START_LINE = re.compile(r"\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4})\s*")
PROCESS_START = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?:[1-9]|[12]\d|3[01]) (?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d \d{4}"
)
LOCK_FILE = re.compile(r"([0-9A-Za-z][0-9A-Za-z-]{7,79})\.lock")
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


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    tty: str
    start: str
    command: str


def _run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str] | None:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.returncode, done.stdout


def _process(pid: int) -> Process | None:
    """None when no such process; OSError when ps itself could not answer."""
    # UTC and the C locale match the procStart format Claude Code records.
    argv = ["ps", "-o", "ppid=", "-o", "tty=", "-o", "lstart=", "-o", "comm=", "-p", str(pid)]
    result = _run(argv, {**os.environ, "TZ": "UTC", "LC_ALL": "C"})
    if result is None:
        raise OSError("process lookup failed")
    lines = [line for line in result[1].splitlines() if line.strip()]
    if result[0] == 1 and not lines:
        return None
    match = PS_LINE.fullmatch(lines[0]) if lines else None
    if result[0] != 0 or not match:
        raise OSError("process lookup failed")
    start = " ".join(match[3].split())
    return Process(pid, int(match[1]), match[2], start, match[4].strip())


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
        args = _run(["ps", "-ww", "-o", "command=", "-p", str(pid)])
        full = _run(["ps", "eww", "-o", "command=", "-p", str(pid)])
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
            process = _process(current)
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


def _small_json(path: Path) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            raw = file.read(MAX_SESSION_FILE + 1)
    except OSError:
        return None
    if len(raw) > MAX_SESSION_FILE:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def claude_pid(root: Path, session_id: str) -> tuple[bool, int | None]:
    """(known, pid) from ``sessions/<pid>.json``; other files there are never opened."""
    try:
        with os.scandir(root / "sessions") as entries:
            names = [entry.name for entry in entries if PID_FILE.fullmatch(entry.name)]
    except OSError:
        return False, None
    known = True
    for name in sorted(names):
        path = root / "sessions" / name
        if path.is_symlink():
            known = False
            continue
        data = _small_json(path)
        pid = int(name.removesuffix(".json"))
        session = data.get("sessionId") if data else None
        if (
            not data
            or data.get("pid") != pid
            or not isinstance(session, str)
            or not SESSION_ID.fullmatch(session)
        ):
            known = False
            continue
        if session != session_id:
            continue
        process = _process(pid)
        if process is None:
            continue
        recorded = _process_start(data.get("procStart"))
        observed = _process_start(process.start)
        if recorded is None or observed is None:
            known = False
            continue
        if recorded != observed:
            continue
        return True, pid
    return known, None


def _process_start(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if PROCESS_START.fullmatch(normalized) else None


def live_sessions(profile: SourceProfile) -> frozenset[str] | None:
    """Native session ids a live local process owns; None when that cannot be told."""
    root = Path(profile.root).expanduser()
    return _claude_live(root) if profile.kind == "claude" else _codex_live(root)


def _claude_live(root: Path) -> frozenset[str] | None:
    try:
        with os.scandir(root / "sessions") as entries:
            names = [e.name for e in entries if PID_FILE.fullmatch(e.name) and not e.is_symlink()]
    except OSError:
        return None
    recorded: dict[int, tuple[str, str | None]] = {}
    for name in names:
        pid, data = int(name.removesuffix(".json")), _small_json(root / "sessions" / name)
        session = data.get("sessionId") if data else None
        if not data or data.get("pid") != pid or not isinstance(session, str):
            return None
        if not SESSION_ID.fullmatch(session):
            return None
        recorded[pid] = (session, _process_start(data.get("procStart")))
    if not recorded:
        return frozenset()
    argv = ["ps", "-o", "pid=", "-o", "lstart=", "-p", ",".join(map(str, sorted(recorded)))]
    result = _run(argv, {**os.environ, "TZ": "UTC", "LC_ALL": "C"})
    if result is None or result[0] not in {0, 1}:
        return None
    live, seen = set(), set()
    for line in result[1].splitlines():
        if not line.strip():
            continue
        match = START_LINE.fullmatch(line)
        if not match or int(match[1]) not in recorded or int(match[1]) in seen:
            return None
        seen.add(int(match[1]))
        session, start = recorded[int(match[1])]
        observed = _process_start(match[2])
        if start is None or observed is None:
            return None
        # A recycled pid has a different start time, so the session is not live.
        if observed == start:
            live.add(session)
    if result[0] == 0 and not seen:
        return None
    return frozenset(live)


def _codex_live(root: Path) -> frozenset[str] | None:
    """Codex holds ``thread-writer-locks/<thread>.lock`` open while a session is live."""
    try:
        with os.scandir(root / "thread-writer-locks") as entries:
            locks = [e.path for e in entries if LOCK_FILE.fullmatch(e.name) and not e.is_symlink()]
    except OSError:
        return None
    lsof = shutil.which("lsof")
    if not locks:
        return frozenset()
    if not lsof:
        return None
    result = _run([lsof, "-F", "pcn", "--", *locks])
    if result is None or result[0] not in {0, 1}:
        return None
    live, command = set(), ""
    for line in result[1].splitlines():
        if line.startswith("p"):
            command = ""
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and "codex" in command.lower():
            match = LOCK_FILE.fullmatch(Path(line[1:]).name)
            if match:
                live.add(match[1])
    return frozenset(live)


def _rollout(root: Path, native: str) -> Path | None:
    pattern = re.compile(rf"[-_]{re.escape(native)}(?:_[0-9A-Za-z-]+)?\.jsonl$")
    for tree in ("sessions", "archived_sessions"):
        pending, visited = [(root / tree, 0)], 0
        while pending and visited < MAX_WALK:
            folder, depth = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        visited += 1
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False) and depth < 5:
                            pending.append((Path(entry.path), depth + 1))
                        elif pattern.search(entry.name):
                            return Path(entry.path)
            except OSError:
                continue
    return None


def codex_pid(root: Path, path: Path) -> tuple[bool, int | None]:
    """(known, pid) of the process holding a rollout transcript open."""
    if path.is_relative_to(root / "archived_sessions"):
        return True, None
    lsof = shutil.which("lsof")
    if not lsof or not path.is_file():
        return False, None
    result = _run([lsof, "-t", "--", str(path)])
    if result is None or result[0] not in {0, 1}:
        return False, None
    candidates = [int(x) for x in result[1].split() if x.isdigit() and int(x) != os.getpid()]
    if result[0] == 1 and not candidates:
        return True, None
    processes = [p for p in map(_process, candidates) if p]
    named = [p for p in processes if "codex" in p.command.lower()]
    # Another holder (an editor, tail) means liveness is unclear: neither focus nor resume.
    return (True, named[0].pid) if named else (False, None)


def _cwd(path: Path | None, codex: bool) -> str:
    if path is None:
        return ""
    try:
        records = _records(path)
    except (OSError, ValueError):
        return ""
    for record in records:
        if codex and record.get("type") != "session_meta":
            continue
        value = record.get("payload") if codex else record
        cwd = value.get("cwd") if isinstance(value, dict) else None
        if (
            isinstance(cwd, str)
            and cwd.startswith("/")
            and len(cwd) <= 1024
            and not any(ord(c) < 32 or ord(c) == 127 for c in cwd)
        ):
            return cwd
    return ""


def _claude_transcript(root: Path, session_id: str) -> Path | None:
    try:
        with os.scandir(root / "projects") as entries:
            folders = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for folder in folders:
        path = folder / f"{session_id}.jsonl"
        if path.is_file() and not path.is_symlink():
            return path
    return None


def resume_command(kind: str, root: Path, native: str, cwd: str) -> str:
    parts = [f"cd {shlex.quote(cwd)} &&"] if cwd else []
    variable = "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME"
    parts.append(f"{variable}={shlex.quote(str(root))}")
    parts.append(f"claude --resume {native}" if kind == "claude" else f"codex resume {native}")
    return " ".join(parts)


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


def adapter(pid: int) -> Target:
    """Choose the most specific way to show the terminal or app hosting ``pid``."""
    chain = _lineage(pid)
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
    session, _, agent = native.partition(":agent:")
    if not SESSION_ID.fullmatch(session):
        return UNSUPPORTED
    root = Path(profile.root).expanduser().resolve()
    try:
        if source == "claude":
            known, pid = claude_pid(root, session)
            transcript = None if pid else _claude_transcript(root, session)
        else:
            transcript = _rollout(root, session)
            if transcript is None:
                return UNSUPPORTED
            known, pid = codex_pid(root, transcript)
        if pid:
            return adapter(pid)
        # Resuming a session that is still running elsewhere would fork it.
        if not known or agent or run.get("parent_run_id"):
            return UNSUPPORTED
        cwd = _cwd(transcript, codex=source == "codex")
        return Target("resume", "resume", command=resume_command(source, root, session, cwd))
    except (OSError, ValueError, RuntimeError):
        return UNSUPPORTED


def focus(target: Target) -> bool:
    if target.kind != "focus" or not target.steps:
        return False
    env = {**os.environ, **dict(target.env)} if target.env else None
    for step in target.steps:
        result = _run(list(step.argv), env)
        if step.required and (result is None or result[0] != 0):
            return False
    return True
