# Optional goal summaries

With `--summaries`, Agent Viz shows one line per active session describing the
**overall problem or goal** it is working on, not its latest step. Summaries do
not establish whether an agent is alive or whether its work succeeded; the
dashboard keeps the original status evidence next to them.

## Sources, in order

1. **Claude recaps.** Claude Code writes `away_summary` records into its
   transcripts. When the latest recap is newer than the latest user request, it
   is shown as-is. No provider request is made.
2. **Session titles.** Claude `ai-title` / `custom-title` records and Codex
   `thread_name` entries from the profile's `session_index.jsonl` replace the
   generic agent name and are included as context.
3. **OpenRouter.** Everything else is summarized by `deepseek/deepseek-v4-flash`
   from goal context: the title, any recap (including Codex compaction
   summaries), the user's first and latest three requests, and the agent's latest
   reply as a hint.

## When summaries refresh

- Only active sessions seen in the last 15 minutes are eligible: top-level
  sessions plus any run (including a subagent) waiting for user input.
- A summary refreshes only when its goal context changes: a new user request,
  title or recap. Assistant progress, tool calls and heartbeats never trigger one.
- A session's summary refreshes at most once every 10 minutes, and the service
  makes at most one request every 120 seconds, covering up to six sessions.
- While a refresh is due, the row keeps its previous summary, marked as stale.

## Data sent

Each entry is at most 2,000 characters. Tool arguments, tool results,
reasoning/thinking, system messages, known environment wrappers and instruction
blocks are excluded. Native session IDs, profile names and profile paths are not
sent: the request uses opaque IDs such as `r1`.

Common API keys, bearer tokens, credential assignments, email addresses and
absolute paths are redacted before sending, and provider output is sanitized
again. This is a best-effort filter. User/assistant prose may itself contain
confidential material, so enable summaries only for work you may send to this
provider. Enabling summaries authorizes excerpts independently of the local
`include_text` display preference.

The provider is told to treat all excerpts as untrusted data. Replies must be
JSON with exactly the requested IDs; only plain text up to 160 characters is kept.
Agent Viz never executes provider output or changes the original task or status.

## Cost reporting

There is no spend or call cap. Each request's actual charge is read from
OpenRouter's `usage.cost` field and recorded; a timeout or missing cost is counted
as unknown rather than zero. The header shows total known spend and the number
of calls. A request costs a fraction of a cent, and the refresh rules above keep
the request rate low.

With a `state_path`, a SQLite journal persists calls, known charges, summary
text and content hashes across restarts. It never stores the key or raw input
text. Keep it inside the ignored local data directory.

## Python integration

```python
from agent_viz.summaries import Summarizer

summaries = Summarizer(
    profiles_provider=lambda: profiles,
    store=store,
    api_key=key_loaded_securely_by_the_application,
    state_path=".agent-viz/summaries.sqlite3",
)
summaries.start()
summaries.stop()
```

`tick()` attempts at most one batch and is useful for explicit refreshes or tests.
Native collection is bounded to the 250 newest transcript files per eligible
profile. All module tests use mocked provider responses.
