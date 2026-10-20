# Optional task summaries

Agent Viz can send a small amount of recent task text to OpenRouter and use
`deepseek/deepseek-v4-flash` to produce one-line activity summaries. This feature
is opt-in. The local collector and dashboard do not need an API key when it is
off. Summaries do not establish whether an agent is alive or whether its work
succeeded; the dashboard must retain the original status evidence.

## Data sent

Only active root runs observed in the last 15 minutes are eligible. Working runs
are considered first, followed by runs waiting for a tool, child or user. Each
request covers at most six runs, with at most 1,000 characters per run.

For SDK runs, the input is the supplied task and activity text. For native Codex
and Claude sessions, the observer reads bounded transcript headers/tails and
selects the latest user request and recent assistant prose. It excludes tool
arguments, tool results, reasoning/thinking, system messages, known environment
wrappers and instruction blocks. Native session IDs, profile names and profile
paths are not sent: the request uses opaque IDs such as `r1`.

Common API keys, bearer tokens, credential assignments, email addresses and
absolute paths are redacted before sending. Provider output is sanitized again.
This is a best-effort filter, not a guarantee that arbitrary private information
can be recognized. User/assistant prose may itself contain confidential material.
Enable summaries only for work you are allowed to send to this external provider.
Enabling summaries authorizes transcript excerpts independently of the dashboard's
local `include_text` display preference; default local text hiding is not an
external-sharing control once summaries are explicitly enabled.

The provider sees instructions to treat all excerpts as untrusted data, not as
commands. Replies must be JSON with the exact requested IDs and string summaries.
Only plain text, capped at 140 characters, is retained. Agent Viz never executes
provider output or changes the original run task/status.

## Cost and refresh controls

- At most one request every 120 seconds, even when polling is faster.
- Identical sanitized task content and state reuse the cached summary; timestamps
  and heartbeats alone do not cause new calls.
- A changed SDK task/activity marks its cached summary stale immediately. For
  native observations, activity more than 30 seconds newer than the last checked
  context is marked possibly stale until a bounded context scan verifies it.
  This is a conservative warning, not proof of a changed task. Summaries older
  than 15 minutes are also marked stale. Displaying summary state never scans
  native transcripts or makes a provider call.
- Defaults: ten requests total and a $0.05 local allowance, with a conservative
  $0.01 reserve before the next request. A request is not made unless that reserve
  fits within the remaining allowance.
- The reserve is an estimate, not a provider-enforced financial cap. Actual
  charges come exclusively from OpenRouter's returned `usage.cost` field. Model
  prices/provider billing can change; this code does not invent an estimated
  charge or represent a missing charge as zero.
- A timeout, failed HTTP request or missing/invalid cost makes the total unknown
  and pauses future requests. A malformed summary still counts as a request and
  its actual charge is recorded when present.
- No automatic retries or secondary model calls. Requests disable reasoning,
  request JSON output and cap output at 600 tokens.

With a `state_path`, an independent SQLite journal persists request counts, known
charges, unknown-charge blocks, summary text and content hashes across restarts.
It never stores the key or raw input text. A pending request left by a crash is
treated as unknown billing. Restarting does not reset the allowance. Keep this
file inside the ignored local data directory. Clearing it discards accounting
history and is not a safe way to resolve a charge; first reconcile billing with
OpenRouter. There is intentionally no automatic "assume free" escape hatch.

The browser receives total known cost separately when the complete total is
unknown. A per-summary `actual_cost_usd` is the charge of its shared batch, not an
additional charge attributable to that run. Do not sum per-summary costs.

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
# Merge summaries.public_state()["summaries"] into a COPY of the run snapshot.
# Do not replace the original stored task or inferred status.
summaries.stop()
```

`tick()` attempts at most one batch and is useful for explicit refreshes or tests.
It still enforces the interval, budget, cache and unknown-cost rules. Native
collection is bounded to the 250 newest transcript files per eligible profile
and stops as soon as the eligible roots are found; it is not a complete
historical scan. The background worker checks every 15 seconds and always
enforces the 120-second minimum between provider requests.

All module tests use mocked provider responses. They do not make paid calls.
