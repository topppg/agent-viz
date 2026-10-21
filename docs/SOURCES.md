# Local Codex and Claude sources

## What works

The collector can observe multiple Codex and Claude storage profiles together.
Each profile has its own stable ID, label and transcript directory. The same
native session ID in two profiles creates two separate dashboard runs. Labels
describe storage profiles; they do **not** prove which account is signed in.

For native observations, the dashboard's `session_id` identifies a stable
**observer collection**, `<profile-id>:observed`, not a vendor conversation.
All observed runs from one storage profile share that collection. Native
conversation/thread identity remains in the namespaced `run_id`; explicit
parent IDs provide the actual run tree. This keeps identity stable when a
parent appears later or the recent-file window rotates, including older logs
without a root conversation ID. Different profiles cannot share a collection.

By default discovery runs again on every background scan, without requiring a
four-profile configuration or restarting the dashboard when another profile
appears. It checks the standard `.codex` and `.claude` directories, named
`.codex-*` and `.claude-*` directories, `.codex-switch/profiles/*`, and the current process's
`CODEX_HOME` / `CLAUDE_CONFIG_DIR` selectors. Aliases pointing to the same storage
directory are deduplicated. Distinct storage directories are **not** collapsed
because they contain identical native session IDs: copied histories and separate
accounts stay distinct. No account, authentication, key or credential file
is opened. Previously discovered roots that disappear remain visible with an
unavailable-source warning; existing observations remain in local history.
Profiles can also be configured explicitly with `load_profiles`; an explicit
list disables automatic discovery and is an intentional manual override.

This is portable directory-pattern discovery, not a scan of the entire computer
or a claim to discover every account. Arbitrary homes outside these patterns
need an environment selector or explicit configuration. Logging into an account
without producing transcripts does not make an observable source.

Codex discovery accepts `sessions/`, `archived_sessions/`, or both. When both
exist, indexing combines them into one newest-first selection and one shared
per-profile entry/file budget. Archived runs preserve their native identity;
moving a transcript into the archive does not make a new dashboard run. Claude
uses its `projects/` transcript tree. Coverage metadata lists only these generic
tree names and counts, never absolute storage paths.

```json
{
  "profiles": [
    {"id": "codex-personal", "kind": "codex", "label": "Codex personal", "root": "/example/codex-personal"},
    {"id": "codex-work", "kind": "codex", "label": "Codex work", "root": "/example/codex-work"},
    {"id": "claude-personal", "kind": "claude", "label": "Claude personal", "root": "/example/claude-personal"},
    {"id": "claude-work", "kind": "claude", "label": "Claude work", "root": "/example/claude-work"}
  ]
}
```

Keep real configuration in the ignored local state directory. Do not publish
personal storage paths or screenshots of private sessions.

## Honest status

Native sources are **observed transcripts**, not SDK producers. Their status is
always marked inferred. A recent file is not proof of a live process. Missing
updates do not imply failure, completion, or a disconnected agent. The dashboard
shows the most recent recorded activity and its timestamp.

- A recorded user turn or model activity suggests `working`.
- A recorded tool call suggests `waiting_tool`; a tool result suggests `working`.
- A recorded end of a turn suggests `idle`, not completed: a session may resume.
- Unsupported records do not fabricate an outcome or a waiting-for-user state.
- Polls do not generate fake agent heartbeats or change `last_seen`.

Codex parent links use explicit `parent_thread_id` metadata, including
`source.subagent.thread_spawn`. Claude subagents use `isSidechain`, `agentId`
and `sessionId`. A Claude `parentUuid` is a message link, **not** an agent link.
No parent is inferred from filenames, similar titles, process trees, or project
names. A parent may be outside the selected recent-file window; the collector
must keep an unresolved parent visible rather than inventing metadata.

## Privacy

By default the dashboard receives generic labels such as “Tool call recorded,”
not prompts, assistant text, tool names, arguments, results, instructions or
account identifiers. Project labels contain only the last directory name.
That name can still be private.

Profiles show the bounded latest request and assistant excerpts by default. Set
`"include_text": false` on a profile to hide them. Excerpts can contain private
information, are not redacted, and become part of local dashboard storage. Tool arguments/results and hidden reasoning are never exported by
these adapters. Turning text off prevents future capture; it does not erase
existing local history.

Claude subagents are named by the short description their parent gave them,
read from the sibling `agent-<id>.meta.json` (bounded regular files only, symlinks not followed),
and their goal is the first prompt the parent sent. Both are text, so a profile
with `"include_text": false` keeps the generic name and hides the prompt.

Every few seconds the monitor checks which native sessions a live local process
still owns: Claude through `sessions/<pid>.json` matched against the live
process start time (`.key` files there are never opened), Codex through the
`thread-writer-locks/<thread>.lock` files a codex process holds open (`lsof`).
A session confirmed to have no owner shows as **closed**. When the check cannot
run (no `sessions/` or lock directory, no `lsof`, a lookup error, or transcripts
synced from another machine) the dashboard falls back to transcript evidence
and never says closed.

## Bounds and limitations

Each scan selects at most 5,000 recent JSONL files per profile by default (a
configurable hard cap of 10,000), with a traversal budget of 30,000 entries and
depth seven. This indexes the available history, not just active sessions; the
current local machine's discovered inventories fit within those bounds.
It reads at most
a 256 KiB header and 512 KiB tail per selected file. Unchanged observations are
cached without retaining raw transcript content. Child symlinks are skipped;
file opens use no-follow support where available. Transcript, subagent metadata
and session-title index readers use nonblocking opens and verify regular files
before reading, so named pipes and other special files cannot stall discovery
or summaries. Invalid JSON, incomplete
trailing records, unreadable and disappearing files are skipped. Large middle
sections are not parsed: older events and initial task text may be missing.

The snapshot's `source_stats` reports discovered-file count (`files_seen`),
selected count, accepted observation count, unsupported-file/read-error counts,
and explicit `truncated` / `discovery_limited` flags. Entry/depth limits,
unreadable directories, and deliberately skipped symlinks have separate counters.
Skipped symlinks also mark coverage limited rather than pretending their targets
were indexed. If discovery is limited,
`files_seen` is only a lower bound, **not** a total history count. Dashboard
coverage must not be described as complete when those flags are true. A source
can contain metadata-only or unsupported JSONL files that do not produce runs.
`duplicate_observations` counts repeated native IDs within the same storage
profile; only the newest observation is retained. IDs from different profiles
are never collapsed this way.
All counts concern files/observations, not authenticated people or processes.

Unchanged file results are cached, so steady-state polling rechecks metadata
rather than repeatedly parsing historical content. The monitor publishes each
completed discovery/coverage scan atomically through `public_state()`.

These adapters follow locally observed formats, **not stable vendor APIs**.
Unknown formats can produce missing or incomplete observations. There is no
automatic login, transcript modification, hook installation, paid model call,
or process control. Tests use invented four-profile fixtures; testing against
existing local profiles does not start or steer the user's agents.

## Opening a session's app

Only when a native row is opened (`GET /api/runs/<id>/focus`) does the collector
look for the live process that owns it; SDK runs are never eligible.

- **Claude:** `<profile root>/sessions/<pid>.json` files are parsed, only names
  of the form `<digits>.json`, bounded and without following links. The adjacent
  `<pid>.<hash>.key` files are **never opened or read**. A match needs the same
  `sessionId` and `pid`, a live process, and a `procStart` equal to that process's
  start time, so a reused PID is not mistaken for the session. A matching live
  process without usable start-time metadata leaves liveness unknown; it cannot
  enable resume or establish that the session is closed. Subagents use their
  parent session's process.
- **Codex:** the session's rollout transcript is located by native ID under the
  profile's `sessions/` and `archived_sessions/` trees, and `lsof -t` reports the
  Codex process holding it open. Archived transcripts are treated as stopped; a
  file held only by some other program leaves liveness unknown (no button).
- **Process details:** `ps` supplies the parent chain, terminal device, start
  time and executable path (to find an enclosing `.app` bundle). The environment
  is read with `ps eww` after stripping the process arguments (or
  `/proc/<pid>/environ` on Linux), and only these
  strictly validated keys are kept: `CMUX_WORKSPACE_ID`, `CMUX_PANEL_ID`,
  `CMUX_SOCKET_PATH`, `TMUX`, `TMUX_PANE`, `TERM_PROGRAM`, `__CFBundleIdentifier`,
  `ITERM_SESSION_ID` and `WINDOWID`. Every other value is discarded, and none is
  returned to the browser or logged.

The browser receives only `available`, `kind` (`focus`, `resume` or
`unsupported`), a display label and, for resume, the command text.
`POST /api/runs/<id>/focus` requires a same-origin `Origin` and an
`X-Agent-Viz: focus` header, takes no body, recomputes the target and runs only
fixed argv lists (no shell) with short timeouts: `cmux select-workspace` /
`focus-panel`, `tmux select-window` / `select-pane` / `switch-client`, an
AppleScript that selects a matching iTerm2 session or Terminal tab, `open -b` /
`open -a`, or `wmctrl -i -a`. Errors are generic. Resume commands are built from
the session ID, the transcript's recorded working directory and the profile root.
Every command explicitly sets `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, including the
default `~/.claude` / `~/.codex`, so an existing terminal's account override cannot
select a different profile. Resume is offered only when no live
process is found and liveness could be checked, and never for subagents. The
browser re-checks the target before copying, so a session that came back to life
is opened rather than forked.
