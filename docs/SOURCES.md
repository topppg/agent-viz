# Local Codex, Claude and opencode sources

## What works

The collector can observe multiple Codex, Claude and opencode storage profiles together.
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

opencode keeps sessions in a SQLite database, `opencode.db` (WAL) in
`$XDG_DATA_HOME/opencode`, default `~/.local/share/opencode`; non-default release
channels use `opencode-<channel>.db` there, and `OPENCODE_DB` (absolute, or relative
to that directory) selects another file. A profile root is the database file (or a
directory holding `opencode.db`). It is opened with a `mode=ro` URI and
`PRAGMA query_only`, never written, checkpointed or vacuumed, and an SQLite
authorizer refuses any read of the `account`, `account_state` and
`control_account` tables. Only the `session`, `message` and `part` tables are
queried; a database without them (for example only the 2.x `session_v2` tables)
yields no sessions and an `unsupported_schema` coverage flag rather than an error.
Each session row is one inventory unit: its key is the session ID, and its cache
signature is the message count and the newest `time_updated` of the session and
its messages (plus its parts, for sessions active in the last 15 minutes).
Archived sessions (`time_archived` set) are never observed and are counted as
`archived_skipped`. Child sessions (`parent_id`) are subagents of their parent.

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
- opencode is `working` while its latest assistant message has no
  `time.completed` or one of its tool parts is `pending`/`running`, otherwise
  `idle`. Permission prompts are not persisted, so it never shows waiting for you.
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

Sessions are named by their own titles when text is shown: a Claude `/rename`
(`custom-title`), else Claude's generated `ai-title`, the Codex thread name
from the profile's `session_index.jsonl` (cached by size and mtime), and the
opencode session `title`. This needs no summaries and makes no provider call.
opencode excerpts come only from non-synthetic `text` parts (at most 4,000
characters each); reasoning parts, tool titles, inputs and outputs are never
selected, and tool names appear only with text enabled.

Claude subagents are named by the short description their parent gave them,
read from the sibling `agent-<id>.meta.json` (bounded regular files only, symlinks not followed),
and their goal is the first prompt the parent sent. Both are text, so a profile
with `"include_text": false` keeps the generic name and hides the prompt.

Every few seconds the monitor checks which native sessions a live local process
still owns: Claude through `sessions/<pid>.json` matched against the live
process start time (`.key` files there are never opened), Codex through the
`thread-writer-locks/<thread>.lock` files a codex process holds open (`lsof`),
and opencode through the working directories (`ps`, `lsof -d cwd`) of live
top-level `opencode` processes. opencode records no per-session owner, so this
is per directory: with N such processes in a directory, its N most recently
updated non-archived top-level sessions and their child sessions count as live.
An older session that is still open in a second terminal in the same directory
can therefore be reported closed. A session confirmed to have no owner shows as **closed**. When the check cannot
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
- **opencode:** the live `opencode` process whose working directory is the
  session's `directory`, when the session is one of the live ones above and that
  directory has exactly one such process. An older session in a directory with a
  live process leaves liveness unknown (no focus, no resume).
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
`X-Spiritboard: focus` header, takes no body, recomputes the target and runs only
fixed argv lists (no shell) with short timeouts: `cmux select-workspace` /
`focus-panel`, `tmux select-window` / `select-pane` / `switch-client`, an
AppleScript that selects a matching iTerm2 session or Terminal tab, `open -b` /
`open -a`, or `wmctrl -i -a`. Errors are generic. Resume commands are built from
the session ID, the transcript's recorded working directory and the profile root.
Every command explicitly sets `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `OPENCODE_DB`
(`cd <directory> && OPENCODE_DB=<db> opencode -s <id>`), including the
default `~/.claude` / `~/.codex` / `opencode.db`, so an existing terminal's account override cannot
select a different profile. Resume is offered only when no live
process is found and liveness could be checked, and never for subagents. The
browser re-checks the target before copying, so a session that came back to life
is opened rather than forked.

## Adding a harness

Each agent harness is one module in `src/spiritboard/harnesses/` that builds a
frozen `Harness` (`harnesses/harness.py`) and is registered in
`HARNESSES` (`harnesses/__init__.py`, keyed by `kind`; registry order is the
discovery order). Profiles, the store, liveness, focus and summaries accept any
registered kind and call only these fields:

| Field | Contract |
| --- | --- |
| `kind`, `name`, `env` | Source kind, display name (`Codex`), and the environment selector naming a storage root (`CODEX_HOME`). |
| `home_roots(home)`, `home_label(entry_name)`, `is_root(root)` | Discovery: `(label, root)` pairs always checked, a label for a matching home entry (or `None`), and whether a root holds session storage. Never open credential or configuration files. |
| `inventory(root, limit, max_age_days, archived)` | The newest `limit` `Session` units (`key`, `path`, `size`, `mtime_ns`, `mtime`; a unit can be a file or a database row, and `(size, mtime_ns)` is its cache signature) and coverage counters; raises `OSError` when no storage is readable. `archived=False` restricts it to live storage (summaries). |
| `observe(profile, session)` | One observation dict (`run_id` via `ns`, `session_id` `<profile>:observed`, `parent_run_id`, `name`, `task`, `activity`, `state`, `last_seen`, `started_at`, `project`, …) or `None`. Text appears only when `profile.include_text`. |
| `latest_request(session)`, `titles(root)` | Deep request search when the bounded read missed it; optional native-ID → title index. |
| `live_sessions(root)`, `owner(native_id)` | Native IDs a live process owns (`None` when unknown) and the native ID whose process owns a (sub)agent. |
| `session_pid(root, session_id)`, `resume(root, session_id)` | `(known, pid)` for focus and the resume command (or `None`); IDs always match `SESSION_ID` (letters, digits, `-`, `_`). |
| `goal(session)`, `free_recaps` | Summary context (`Goal`: redacted title and recap, raw user/assistant messages that summaries filters and redacts) and whether a recap is itself a summary. |

Build on `transcripts.py` (bounded no-follow readers, `walk_jsonl`, `safe`,
`redact`, `ns`, `observation`) and `processes.py` (`ps`/`lsof` argv, no shell).
Adding a harness means the module, its registry entry and synthetic tests; no
other module changes.
