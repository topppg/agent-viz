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
read from the sibling `agent-<id>.meta.json` (bounded, symlinks not followed),
and their goal is the first prompt the parent sent. Both are text, so a profile
with `"include_text": false` keeps the generic name and hides the prompt.

## Bounds and limitations

Each scan selects at most 5,000 recent JSONL files per profile by default (a
configurable hard cap of 10,000), with a traversal budget of 30,000 entries and
depth seven. This indexes the available history, not just active sessions; the
current local machine's discovered inventories fit within those bounds.
It reads at most
a 256 KiB header and 512 KiB tail per selected file. Unchanged observations are
cached without retaining raw transcript content. Child symlinks are skipped;
file opens use no-follow support where available. Invalid JSON, incomplete
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

## Daily usage scan

The usage heatmap has its own read-only pass, separate from the run scan. It
walks the same transcript trees for `.jsonl` files modified in the last 365
days, regardless of `--max-age-days`, and counts per local calendar day:

- **requests**: real user turns, the same notion as the run's latest request.
  Claude `user` records count unless they are tool results, `isMeta`,
  `isCompactSummary` or `isSidechain` (a subagent prompt comes from its parent
  agent), or their text starts with `<`. Codex counts `event_msg`
  `user_message` events, except in subagent threads.
- **sessions**: distinct Claude `sessionId` / Codex root thread IDs with any
  timestamped record that day; a subagent's activity counts for its parent.

Unlike the run scan, this streams whole files line by line, so the middle of a
large transcript is counted. It is still bounded: at most the newest 128 MiB of
a file is read, records over 4 MiB are skipped, at most 10,000 files per
profile are selected, and a file still being written loses only its incomplete
last line. Only lines that can be user turns or session metadata are parsed as
JSON; the rest are scanned for their timestamp. Per-file results are cached by
size and modification time and hold only counts and namespaced session IDs, so
repeat passes re-read only changed files. A changed file is re-read in full,
so a large transcript that is still being written costs a full read each pass;
there is no total byte budget per pass.
Files older than the window are not opened; records older than 365 days are
dropped. Credential, configuration and `.key` files are never opened.

The monitor runs this pass in its own thread every 60 seconds; the first pass
starts with the dashboard and can take a while on a large history. The snapshot
caches the result for 30 seconds as `usage`:

```json
{"days": [{"date": "2026-10-14", "requests": 23, "sessions": 4,
           "by_harness": {"profile:codex-work": 20, "sdk:local": 3},
           "sessions_by_harness": {"profile:codex-work": 3, "sdk:local": 1}}],
 "window_days": 365, "limited": false}
```

Only days with activity are listed. Native harnesses are `profile:<id>`. SDK
runs come from this observer's own journal, keyed `<source>:<account>`, with one
request per `run.started` event, bucketed by the collector's receive time
(not the producer's clock), so SDK history only covers what was sent to this
data directory. `limited` is true when any profile's usage pass hit a file,
record, traversal or selection bound, a read error, or was unavailable; the
dashboard then marks the totals as partial.
Days are bucketed in the collector's local time zone; the browser lays out the
grid in its own, so both must be on the same machine or zone. Not verified: if a
resumed Claude session copies earlier turns into its new file, they count twice.

These adapters follow locally observed formats, **not stable vendor APIs**.
Unknown formats can produce missing or incomplete observations. There is no
automatic login, transcript modification, hook installation, paid model call,
or process control. Tests use invented four-profile fixtures; testing against
existing local profiles does not start or steer the user's agents.
