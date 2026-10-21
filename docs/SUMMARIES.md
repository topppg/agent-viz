# Optional goal summaries

With summaries enabled, Spiritboard shows one line per eligible session describing the
**overall problem or goal** it is working on, not its latest step. Summaries do
not establish whether an agent is alive or whether its work succeeded; the
dashboard keeps the original status evidence next to them.

## Connect from the dashboard

Open **AI summaries**, read the sharing notice, and check the consent box before
connecting a provider. Connecting enables summaries.

The main panel keeps only connection and consent controls visible. Expand
**How it works** for model details, or **Use an API key instead** for manual
OpenRouter setup.

Use either option or both:

- **OpenRouter:** browser sign-in with PKCE (a one-use proof that binds the code
  to this app), or paste a dedicated API key into the password field.
  Model: `deepseek/deepseek-v4-flash`.
- **Continue with ChatGPT:** the official browser authorization flow for
  open-source apps. Model: `gpt-6-luna`, reasoning effort `medium`.
  The model must be available to the authorized account; Spiritboard does not
  silently substitute another model. No installed Codex CLI is needed.

Spiritboard uses OpenRouter first. If that attempt fails or returns unusable
output, it can try the explicitly connected ChatGPT account for the same batch.
With only one connection, only that provider is used. Each attempt is recorded;
fallback is not a free retry. There are no tool calls or agent actions in either
summary request.

Pause with **Enable AI summaries**, cancel pending sign-ins, or disconnect an
individual provider. Connections and the enabled setting survive restarts.
Disconnection is recorded before credential teardown, so an old credential
cannot silently become authorized again if removal fails. Calls already sent
may still consume provider usage; late summaries are discarded after pausing.
Demo mode disables connections and model requests entirely.

The browser sends a pasted key only to the loopback service and clears the form
on submission or close. It never stores credentials in browser storage. OAuth
callbacks validate one-use state; ChatGPT additionally verifies signed identity
tokens, account identity, requested scopes and a login nonce. Spiritboard stores
its own connection files under `auth/` in the private data directory, with private
file permissions on POSIX systems. They are not encrypted at rest. It does not
reuse authentication files from discovered Codex or Claude profiles.

ChatGPT disconnection clears local tokens and attempts remote revocation. If
revocation cannot be confirmed, the settings panel explicitly tells you to
disconnect Spiritboard in ChatGPT settings. Local sign-out alone does not prove
that the remote grant was revoked.

For a terminal setup, `--summaries` enables connected providers and optionally
uses `OPENROUTER_API_KEY` or `--openrouter-key-file`. Such launch-provided keys
stay in memory rather than being copied into connection storage. Without a saved
enabled setting or explicit `--summaries`, a new installation makes no summary calls.

## Sources, in order

1. **Claude recaps.** Claude Code writes `away_summary` records into its
   transcripts. When the latest recap is newer than the latest user request, it
   is shown as-is. No provider request is made.
2. **Session titles.** Claude `ai-title` / `custom-title` records and Codex
   `thread_name` entries from the profile's `session_index.jsonl` replace the
   generic agent name and are included as context.
3. **Connected model provider.** Everything else is summarized by the routing above
   from goal context: the title, any recap (including Codex compaction
   summaries), the user's first and latest three requests, and the agent's latest
   reply as a hint.

## When summaries refresh

- Archived sessions are excluded from summary generation, including local recaps.
- Only sessions seen in the last 15 minutes are eligible: working, waiting or
  idle top-level sessions (idle means the turn ended and it awaits your next
  direction), plus any run, including a subagent, waiting for user input.
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
Spiritboard never executes provider output or changes the original task or status.

## Cost reporting

There is no spend or call cap. Each OpenRouter request's actual charge is read from
OpenRouter's `usage.cost` field and recorded; a timeout or missing cost is counted
as unknown rather than zero. The header shows total known spend and the number
of API calls. ChatGPT requests are reported separately as plan calls: they use
your account allowance, and no dollar charge is invented. A total dollar cost
is unknown if any API attempt has no receipt or any plan call is present.
The refresh rules above keep the request rate low; they do not guarantee a price.

With a `state_path`, a SQLite journal persists calls, known charges, summary
text and content hashes across restarts. It never stores the key or raw input
text. Keep it inside the ignored local data directory.

## Python integration

```python
from spiritboard.summaries import Summarizer

summaries = Summarizer(
    profiles_provider=lambda: profiles,
    store=store,
    api_key=key_loaded_securely_by_the_application,
    state_path=".spiritboard/summaries.sqlite3",
)
summaries.start()
summaries.stop()
```

`tick()` attempts at most one batch and is useful for explicit refreshes or tests.
Native collection is bounded to the 250 newest transcript files per eligible
profile. All module tests use mocked provider responses. Local tests verify the
authorization protocol and failure handling using synthetic identities; they
do not prove that a particular account is eligible for a live grant or model.

The ChatGPT integration follows OpenAI's official
[open-source sign-in flow](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
and [models and inference contract](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
