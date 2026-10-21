# Spiritboard

**Your agents. One board.**

Open it and immediately know which agents need your attention, what they're
doing, and where to continue.

Spiritboard is a local dashboard for every coding agent you run. Claude Code,
Codex and opencode sessions from your terminals, your desktop apps and each of your accounts
appear side by side, with what each one is working on, whether it is running,
waiting for you or gone, and a button that takes you straight to it.

It only reads what the agents already write to disk. It never starts, steers or
stops an agent, and nothing leaves your machine unless you turn on summaries.

## What you see

- **Every harness at once.** Each storage profile (personal and work accounts,
  the default CLI folders, the desktop apps) is a harness with its own colour,
  live events per minute, and a 10-minute activity chart stacked by harness.
- **Where to put your attention.** Every session has one status:

  | Status | Meaning |
  | --- | --- |
  | ◐ **needs you** | Waiting for your approval. |
  | ↩ **your turn** | Finished its turn in the last 30 minutes and is waiting for your next direction. |
  | ● **running** | Working, including while it waits on a tool or a subagent. |
  | ◌ **open · idle** | Still open in a terminal or app, idle for longer. |
  | ○ **quiet** | No recent activity, and liveness could not be checked. Quiet never means failed. |
  | ■ **closed** | No running process owns the session any more. |
  | ✓ **done** | An SDK run reported that it finished. |

- **What each one is doing.** The session's own title (your `/rename`, Claude's
  generated title, the Codex thread name or the opencode session title), its goal, your latest request and
  the agent's latest reply or commentary. Subagents sit under their parent,
  named by the task the parent gave them.
- **Jump in.** Open a session in the terminal or app that runs it (cmux, tmux,
  iTerm2, Terminal, VS Code, the ChatGPT or Claude desktop app), or copy a
  resume command when it is closed.

Click a row to open it in place; `/` finds, `j`/`k` move, `Esc` closes, `a` / `w`
/ `e` switch between active, needs-you and all.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
Spiritboard from anywhere. No repo checkout or API key is needed; uv manages
the isolated Python environment (Python 3.12+). There are no runtime package
dependencies.

See your own agents. Supported Codex, Claude and opencode storage is discovered
automatically, without opening authentication files or tables:

```sh
uvx --from git+https://github.com/topppg/spiritboard.git@main spiritboard
```

Your browser opens automatically. The terminal prints the loopback-only URL;
if port 8766 is busy, a free port is chosen. Leave that terminal running.
Ctrl-C stops only Spiritboard, never an agent. Running the same command again
reopens the existing board instead of starting a second collector.

Or try an invented scenario with no real agents, tools or models:

```sh
uvx --from git+https://github.com/topppg/spiritboard.git@main spiritboard --demo
```

The demo has separate local history and never discovers real profiles or calls
a model. Add `--no-open` to print the URL without launching a browser.

For a permanent command:

```sh
uv tool install git+https://github.com/topppg/spiritboard.git@main
spiritboard
```

A PyPI release is not published yet; do not substitute `uvx spiritboard` until it is. See the
[distribution guide](docs/DISTRIBUTION.md) for upgrades, removal and publishing.

## Sources and privacy

- **Discovery.** The standard `~/.claude` and `~/.codex` folders, named variants
  such as `~/.claude-work` or `~/.codex-switch/profiles/*`, `CLAUDE_CONFIG_DIR`
  / `CODEX_HOME`, and opencode's `opencode.db` (and release-channel databases) in
  `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`, or `OPENCODE_DB`. The
  opencode database is opened read-only. To choose sources yourself, write a private config and pass it:

  ```sh
  spiritboard discover --write-config ~/.config/spiritboard/profiles.json
  spiritboard --config ~/.config/spiritboard/profiles.json
  ```

  An explicit config replaces automatic discovery, so list every folder you want.
  Equal session IDs or names never merge across profiles, and a profile label is
  not proof of which account is signed in.
- **Text.** Task and reply excerpts are shown by default. Set
  `"include_text": false` on a profile to hide them. Excerpts can contain private
  information and stay in local history, so review the board before recording or
  sharing it.
- **History window.** Transcripts not modified in the last 7 days are skipped
  without being read; change it with `--max-age-days N` (`0` reads everything).
- **Liveness.** Every few seconds Spiritboard checks which sessions a live local
  process still owns, from Claude's `sessions/<pid>.json` and the Codex thread
  locks. opencode records no owner, so a live `opencode` running in a session's
  directory marks only that directory's most recent session open. A session is
  only marked closed when that check can run.

What is read, and the bounds on it, are in the [source guide](docs/SOURCES.md).

## Open a session in its app

Opening a row looks up which local process owns the session and how to show it:
a cmux panel, a tmux pane, an iTerm2 session, an Apple Terminal tab, or the
owning app's window (VS Code, Ghostty, ChatGPT, Claude desktop). On Linux, tmux
panes and `wmctrl` windows are supported.

The button only brings a window forward. It never types into, signals or
controls an agent. The first iTerm2 or Terminal use can trigger a macOS
automation prompt. For a closed session it offers a resume command, such as
`claude --resume <id>` with the profile's `CLAUDE_CONFIG_DIR`, for you to copy
and run; Spiritboard never runs it, and never offers it while the session might
still be running elsewhere.

## Optional goal summaries

```sh
spiritboard --summaries --openrouter-key-file /absolute/path/to/private.env
```

Each active session gets one line describing its overall goal. Claude's own
recaps are used as they are, at no cost. Other sessions are summarized by
`deepseek/deepseek-v4-flash` through OpenRouter, from their title, recap and your
requests, and refreshed only when those change. Excerpts are redacted on a
best-effort basis before they leave the machine, and the header shows known
spend. Read the [summary guide](docs/SUMMARIES.md) before using this on
confidential work. The key can also come from `OPENROUTER_API_KEY`; it is never
served to the browser.

Stop an existing board before changing its launch settings. A second command
with different options refuses to reuse it, rather than silently ignoring your
privacy or source choices, including edits to an explicit source config.

## Report from your own agents

A small SDK lets any Python agent report runs, subagents, activity and tool calls
to the same board:

```python
from spiritboard.launcher import default_data_dir, running_url
from spiritboard.sdk import Client

data = default_data_dir()
token = (data / "token").read_text().strip()
with Client(running_url(data), token, account="example") as client:
    with client.run("Coordinator", "Inspect an invented change") as root:
        with root.child("Reviewer", "Check the example") as child:
            child.activity("Checking the invented test fixture")
            with child.operation("tool", "Read synthetic fixture"):
                pass  # Put your actual operation here; no automatic interception.
        root.activity("Review complete")
    delivered = client.flush(timeout=2)
    print("Delivered:", delivered, client.diagnostics)
```

Start `spiritboard` first; `running_url` finds it on whichever port it bound. With
another `--data-dir`, pass that directory instead. `spiritboard demo` uses the same
lookup to send its synthetic scenario to the running board. Use `heartbeat()` during
long quiet work. Failed delivery never changes your application's result; check
`flush()` and `diagnostics` to detect loss.

## Local storage and cleanup

History stays in a stable per-user directory, independent of your terminal's
working folder:

| Platform | Default directory |
| --- | --- |
| macOS | `~/Library/Application Support/spiritboard` |
| Linux | `$XDG_DATA_HOME/spiritboard`, or `~/.local/share/spiritboard` |
| Windows | `%LOCALAPPDATA%/spiritboard` |

`--demo` uses its `demo` subdirectory. `--data-dir /your/chosen/directory`
overrides either default. Existing checkout-local history is never moved or
deleted; pass its old directory explicitly to keep using it.

On macOS/Linux, existing data directories must be private to your user; unsafe
permissions cause a clear error instead of being changed automatically.

This directory holds `events.sqlite3`, a private ingestion token, an instance
lock and, with summaries on, `summaries.sqlite3`. Keep it and any custom source
config out of version control. Retention is manual:

```sh
spiritboard prune --older-than-days 7
```

This deletes only old observer records, never your agents' transcripts. Stop the
dashboard first; pruning refuses while that data directory is in use. You can
run separate boards with separate `--data-dir` values and ports.

## Development

Contributors still use a checkout:

```sh
uv sync --frozen
uv run spiritboard --demo --no-open
uv run ruff check . && uv run ruff format --check .
uv run pytest
node --check src/spiritboard/static/app.js
```

Node is optional for the Python tests but runs the browser DOM test; check for
skips before relying on that coverage. Contribution rules, including worktrees
and one change per PR, are in [AGENTS.md](AGENTS.md).

More detail: [sources](docs/SOURCES.md), [summaries](docs/SUMMARIES.md),
[validation record](docs/VALIDATION.md), [functional specification](docs/FUNCTIONAL_SPEC.md)
and [reference projects](docs/REFERENCES.md). The `references/` folder holds
shallow research checkouts of other projects, excluded from Git history and never
installed, executed or copied.

## Direction

Spiritboard stays a small local observer: one board for every agent harness you
use, which tells you at a glance what each agent is working on and which one
needs you. It is not a cost or process monitor, and it does not launch, steer or
approve agents.

The look is ghostly rather than spooky: dark amethyst surfaces, Space Grotesk
for reading with Space Mono for numbers and times, violet for activity, amber
for attention, pink for summaries, and motion only when something actually
changes. Labels stay literal (needs you, your turn,
closed, open in cmux) so the board needs no vocabulary of its own.

Next: more harnesses and framework adapters, exact tab focus for Ghostty once a
released version exposes it, and a demo scenario that is safe to record.

## Boundaries

This is an observer, not an agent framework, task scheduler or remote control.
It never kills, steers or approves actions; opening a session's app only brings
an existing window forward. A clear board must never hide lost events or claim
that an unresponsive agent has failed.

## License

[MIT](LICENSE). The bundled Space Grotesk and Space Mono fonts are under the
SIL Open Font License 1.1; their licences ship next to them in
`src/spiritboard/static/fonts/`.
