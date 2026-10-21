# Spiritboard

**Your agents. One board.**

Open it and immediately know which agents need your attention, what they're
doing, and where to continue.

Spiritboard is a local dashboard for every coding agent you run. Claude Code and
Codex sessions from your terminals, your desktop apps and each of your accounts
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
  generated title, or the Codex thread name), its goal, your latest request and
  the agent's latest reply or commentary. Subagents sit under their parent,
  named by the task the parent gave them.
- **Jump in.** Open a session in the terminal or app that runs it (cmux, tmux,
  iTerm2, Terminal, VS Code, the ChatGPT or Claude desktop app), or copy a
  resume command when it is closed.

Click a row to open it in place; `/` finds, `j`/`k` move, `Esc` closes, `a` / `w`
/ `e` switch between active, needs-you and all.

## Quick start

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required; there are no
runtime package dependencies.

See your own agents. Supported Codex and Claude storage folders are discovered
automatically, without opening authentication files:

```sh
uv run spiritboard serve --data-dir .spiritboard/live --port 8767
```

Then open <http://127.0.0.1:8767>. Stopping the dashboard never stops an agent.

Or try an invented scenario with no real agents, tools or models:

```sh
uv run spiritboard serve --demo --data-dir .spiritboard/demo --port 8766
```

## Sources and privacy

- **Discovery.** The standard `~/.claude` and `~/.codex` folders, named variants
  such as `~/.claude-work` or `~/.codex-switch/profiles/*`, and `CLAUDE_CONFIG_DIR`
  / `CODEX_HOME`. To choose sources yourself, write a private config and pass it:

  ```sh
  uv run spiritboard discover --write-config .spiritboard/profiles.json
  uv run spiritboard serve --config .spiritboard/profiles.json --data-dir .spiritboard/live
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
  locks. A session is only marked closed when that check can run.

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
uv run spiritboard serve --data-dir .spiritboard/live --port 8767 \
  --summaries --openrouter-key-file /absolute/path/to/private.env
```

Each active session gets one line describing its overall goal. Claude's own
recaps are used as they are, at no cost. Other sessions are summarized by
`deepseek/deepseek-v4-flash` through OpenRouter, from their title, recap and your
requests, and refreshed only when those change. Excerpts are redacted on a
best-effort basis before they leave the machine, and the header shows known
spend. Read the [summary guide](docs/SUMMARIES.md) before using this on
confidential work. The key can also come from `OPENROUTER_API_KEY`; it is never
served to the browser.

## Report from your own agents

A small SDK lets any Python agent report runs, subagents, activity and tool calls
to the same board:

```python
from pathlib import Path
from spiritboard.sdk import Client

token = Path(".spiritboard/local/token").read_text().strip()
with Client("http://127.0.0.1:8766", token, account="example") as client:
    with client.run("Coordinator", "Inspect an invented change") as root:
        with root.child("Reviewer", "Check the example") as child:
            child.activity("Checking the invented test fixture")
            with child.operation("tool", "Read synthetic fixture"):
                pass  # Put your actual operation here; no automatic interception.
        root.activity("Review complete")
    delivered = client.flush(timeout=2)
    print("Delivered:", delivered, client.diagnostics)
```

Start `serve` with its default data directory first. Use `heartbeat()` during
long quiet work. Failed delivery never changes your application's result; check
`flush()` and `diagnostics` to detect loss.

## Local storage and cleanup

The `--data-dir` holds `events.sqlite3`, a private ingestion token and, with
summaries on, `summaries.sqlite3`. Source config and local data are ignored by
Git. Retention is manual:

```sh
uv run spiritboard prune --data-dir .spiritboard/live --older-than-days 7
```

This deletes only old observer records, never your agents' transcripts. Stop the
dashboard first, and do not run two against the same data directory.

## Development

```sh
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

The look is ghostly rather than spooky: dark terminal surfaces, crisp monospace,
mint for activity, amber for attention, violet for summaries, and motion only
when something actually changes. Labels stay literal (needs you, your turn,
closed, open in cmux) so the board needs no vocabulary of its own.

Next: more harnesses and framework adapters, exact tab focus for Ghostty once a
released version exposes it, and a demo scenario that is safe to record.

## Boundaries

This is an observer, not an agent framework, task scheduler or remote control.
It never kills, steers or approves actions; opening a session's app only brings
an existing window forward. A clear board must never hide lost events or claim
that an unresponsive agent has failed.

## License

[MIT](LICENSE).
