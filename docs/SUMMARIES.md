# Optional goal summaries

With summaries enabled, Spiritboard shows one line per eligible session describing the
**overall problem or goal** it is working on, not its latest step. Summaries do
not establish whether an agent is alive or whether its work succeeded; the
dashboard keeps the original status evidence next to them.

## Connect from the dashboard

Open **AI summaries**, read the sharing notice, and check the consent box before
connecting a provider. Choose a service from **Provider**, then sign in or add
its API key. Connecting enables summaries and makes that service the first choice.

The main panel keeps only connection and consent controls visible. Expand
**How it works** for model details. OpenRouter also offers **Use an API key
instead** below its sign-in button.

Available connections:

- **OpenRouter:** browser sign-in with PKCE (a one-use proof that binds the code
  to this app), or paste a dedicated API key into the password field.
  Model: `deepseek/deepseek-v4-flash`.
- **Continue with ChatGPT:** the official browser authorization flow for
  open-source apps. Model: `gpt-6-luna`, reasoning effort `medium`.
  The model must be available to the authorized account; Spiritboard does not
  silently substitute another model. This flow requires an eligible personal
  Plus or Pro account; Enterprise workspaces are not supported. No installed
  Codex CLI is needed. If OpenAI shows a workspace-policy rejection, do not keep
  retrying the same workspace: choose an eligible personal account for permitted
  data, or another approved provider. Do not send work data through a personal
  account to bypass an organization's policy.
- **Anthropic API:** paste a dedicated Anthropic API key. Model:
  `claude-haiku-5-5`. This uses API billing, not a Claude chat subscription.
- **OpenAI API:** paste a dedicated OpenAI API key. Model: `gpt-6-luna`, reasoning
  effort `medium`. This uses API billing, not a ChatGPT plan or its allowance.

For an already connected service, choose **Use for summaries** to put it first.
If that attempt fails or returns unusable output, Spiritboard can try the other
explicitly connected services for the same batch. Remaining order is OpenRouter,
ChatGPT, OpenAI API, then Anthropic API, excluding the first choice. With only one
connection, only that provider is used. Each attempt is recorded; fallback may
use another provider's credits and is not a free retry. There are no tool calls
or agent actions in any summary request.

T3 Code is a harness that runs underlying coding agents, not a separate summary
model provider. Spiritboard does not borrow its login or offer a T3 sign-in.

Pause with **Enable AI summaries**, cancel pending sign-ins, or disconnect an
individual provider. Connections and the enabled setting survive restarts.
Disconnection is recorded before credential teardown, so an old credential
cannot silently become authorized again if removal fails. Calls already sent
may still consume provider usage; late summaries are discarded after pausing.
Demo mode disables connections and model requests entirely.

The browser sends a pasted key only to the loopback service and clears all key
fields on submission, provider changes, or close. It never stores credentials in
browser storage. OAuth
callbacks validate one-use state; ChatGPT additionally verifies signed identity
tokens, account identity, requested scopes and a login nonce. Spiritboard stores
OpenRouter, Anthropic and OpenAI API keys, and ChatGPT access, refresh and identity
tokens in the native OS
credential store: macOS Keychain, Windows Credential Manager, or Linux Secret
Service. It does not reuse authentication files from discovered Codex or Claude
profiles. No credential values are written into Spiritboard's local files.

Only private, non-secret references remain under `auth/credentials/` in the data
directory. Each data directory has separate credential entries. Larger records
are split into bounded OS-store entries to fit Windows limits. New entries are
read back and verified before their references are committed locally.

If storage is locked, unavailable, denied or corrupt, saving/restoring fails
clearly. No plaintext or plugin-based fallback is used. Monitoring and demo mode
still work without a credential store; explicit launch-provided keys can remain
in memory as described below. On Linux, an unlocked Secret Service collection
and a D-Bus user session are required for saved connections.

This protects credentials on disk; it does not make a compromised computer safe.
In particular, macOS may allow other scripts using the same Python executable
to access its keychain entries without another prompt. See the upstream
[keyring security notes](https://keyring.readthedocs.io/en/stable/#security-considerations).

There is deliberately no migration. Old plaintext connection files are ignored,
not read, imported or automatically deleted. If testing an older build, remove
those old files yourself and connect again. Removing a data directory or
uninstalling the package is not a substitute for disconnecting/revoking provider
access; disconnect before removing the app's state.

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

## Titles

- A session starts with its native title. When a summary refreshes, the same call
  may give it a new title if the goal has clearly moved on, at most once an hour.
  The title it replaced is shown as "was: …" in the expanded row.
- Names you set yourself (a Claude `/rename`, a Pi `/name`) are never replaced.
  Other harnesses don't record who set a title, so any change to the native title
  is treated as a rename: the AI title is dropped and later ones build on it.
- A subagent with only a generic name (no Claude Task description, no DeepSeek
  title) is named once from the task its parent gave it, after it has run for
  30 seconds, in the same batched call. Codex subagent prompts are encrypted, so
  they keep their agent path.

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
of API calls. Direct OpenAI and Anthropic responses report token usage, not a
final billed dollar charge; those calls count as unknown cost, never as free or
as an estimated "actual" charge. When no charge is known, the header says
**cost unavailable**. ChatGPT requests are reported separately as plan calls: they use
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
