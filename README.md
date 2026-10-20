# Agent Viz

A small local dashboard for understanding what your agents are doing, right now.

**Status: working local prototype with a first spectral terminal theme.**
This is a local-first observer, not a hosted service. It does not change native
accounts or install hooks. Observation is local by default. Optional DS4F summaries send
bounded, best-effort-redacted task excerpts to OpenRouter when explicitly enabled.

The first version should answer four questions immediately:

1. Which agent runs are active?
2. What is each one trying to do, and doing now?
3. Which agent spawned which subagents?
4. Is a run working, waiting, finished, or simply no longer reporting?

## Start here

- [Source profiles, privacy, and native-format limits](docs/SOURCES.md)
- [Optional DS4F summaries, privacy, caching, and actual costs](docs/SUMMARIES.md)
- [Validation record and current boundaries](docs/VALIDATION.md)
- [Functional specification and build order](docs/FUNCTIONAL_SPEC.md)
- [Reference projects and lessons](docs/REFERENCES.md)
- [Pinned upstream checkouts](references.lock.json)

Seven shallow upstream clones live in `references/`. They are independent research
checkouts, excluded from this project's Git history, not dependencies or vendored
source. Their existing licenses and notices remain intact. Nothing from them has
been installed or executed.

## Run the working demo

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required; there are **no runtime
package dependencies**. From this directory:

```sh
uv run agent-viz serve --demo --data-dir .agent-viz/demo --port 8766
```

Open [the local dashboard](http://127.0.0.1:8766). Press Ctrl-C in that terminal
to stop it. This is an observer, so stopping it does not stop any real agents.

The invented scenario uses the real SDK, HTTP collector, SQLite journal, and
browser event stream. It creates a coordinator, two same-name workers, a nested
recovery task, and one deliberately silent worker. Four runs finish; the silent
run stays working with stale telemetry. It is **not** silently marked failed.
Tool operations and the approval are simulated; no real tools or models run.
Rerunning a demo appends fresh identities rather than overwriting earlier runs.

## Observe Codex and Claude across profiles

Start the dashboard; supported Codex and Claude storage profiles are discovered
automatically without opening authentication files. New supported profiles are
picked up on later scans:

```sh
uv run agent-viz serve --data-dir .agent-viz/live --port 8767
```

For a manually selected set of sources instead:

```sh
uv run agent-viz discover --write-config .agent-viz/profiles.json
uv run agent-viz serve --config .agent-viz/profiles.json --data-dir .agent-viz/live --port 8767
```

Open [the local source dashboard](http://127.0.0.1:8767). Transcripts not modified in the last 7 days
are skipped without being read; change this with `--max-age-days N` (`0` reads
everything). The configuration
writer refuses to overwrite an existing file. You can select a subset on initial
discovery with repeated `--label 'exact discovered label'` arguments, or edit
your private configuration. Use `--no-discovery` for an SDK-only collector.
Discovery covers supported standard/named storage layouts and environment
selectors, not every possible custom directory or every signed-in account.

Multiple account directories are independent sources: native IDs are namespaced
by profile, so equal IDs or agent names never merge across profiles. A label is
not proof of the currently authenticated account. No login switching is performed.

Native sources are **read-only transcript observations**, not an authoritative
process inventory. State is visibly marked inferred, with last-evidence time.
They can show recent and historical sessions; hundreds of observed runs do not mean hundreds of
currently running processes. A stopped output stream is not a completion signal.

Task/message text is hidden by default. Set `"include_text": true` for selected
profiles in the ignored config to show bounded task/activity excerpts. Those can
contain private information and remain in local history: opt-in is not redaction.
Do not record or share a live-source dashboard without reviewing its contents.

## What works now

- `agtop` browser view: htop-style agent table (status, tree, harness, current
  activity or AI summary, events/min, last seen), per-harness event rates with a
  10-minute stacked chart, active/needs-you/all views, harness filter, search,
  keyboard navigation, run detail, and paginated event history.
- Explicit SDK lifecycle and independent tool/model operation outcomes, with
  separate work state and telemetry freshness.
- Codex and Claude multi-profile discovery, namespaced identities, and available
  native subagent relationships; unsupported or missing evidence stays inferred.
- Recent working branches first, including parents with active descendants;
  source cards, evidence-window filters, and Git-log-inspired delegation rails.
- Optional cached DS4F one-line briefs with reported provider charges, persistent
  request accounting, and explicit stale/unknown-cost warnings.
- SQLite persistence, initial browser snapshots, live updates, reconnect recovery,
  duplicate protection, sequence-gap warnings, and terminal-state protection.
- Bounded SDK buffers, delivery diagnostics, retries with stable IDs, synchronous
  methods and asynchronous flush/close helpers.
- Loopback-only serving, ingestion token, Host/Origin checks, local assets, safe
  text rendering, and no remote telemetry or raw-payload capture by default.

## Optional goal summaries

```sh
uv run agent-viz serve --port 8767 \
  --data-dir .agent-viz/live --summaries \
  --openrouter-key-file /absolute/path/to/private.env
```

Alternatively supply `OPENROUTER_API_KEY` in the environment. Never put a key in
source control or a command argument. The key is not served to the browser.
Each active session gets one line describing its overall goal. Claude recaps
are used directly; other sessions are summarized from titles, recaps and the
user's requests, refreshed only when those change. Summaries never establish
process liveness. Enabling them permits external excerpts even when native text
display is off, and redaction cannot recognize every secret. There is no spend
cap; known spend is shown in the header. See the [summary guide](docs/SUMMARIES.md)
before enabling this on confidential work.

## Small SDK example

Start `serve` with its default data directory first, then:

```python
from pathlib import Path
from agent_viz.sdk import Client

token = Path(".agent-viz/local/token").read_text().strip()
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

The observer does not infer progress from hidden reasoning. Activity text is
application-supplied or explicitly enabled native excerpts. Use `heartbeat()`
during long quiet work when using the manual SDK. Failed delivery never changes
the application result; inspect `flush()` and `diagnostics` to detect loss.

## Validation

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest
node --check src/agent_viz/static/app.js
uv build
```

The initial functional checkpoint passed **63 tests** on Python 3.12 and 3.14, including
the synthetic HTTP demo, four-profile adapter fixtures, state/identity rules,
reconnect/reset behavior, browser DOM safety, and local HTTP access checks. Node
is optional for running Python tests but required for the additional DOM test;
check for skips before claiming that coverage.

Real read-only acceptance loaded all four personal/work profiles. A 50-file
selection per profile produced 199 observations and 85 parent links with no
source errors; a separate CLI/HTTP check using 30 files produced 119 observations
and 57 parent links. Counts are time- and window-dependent, not a benchmark or
an active-process count. No private transcript fixture is committed.

The wheel and source distribution build successfully. Installation and browser
asset presence were checked from a temporary environment outside this checkout.
Browser testing verified the synthetic tree, recovered error detail, state filter,
refresh recovery, and an empty browser error log. See the validation record for
limits, including the untested ten-minute performance target.

The automatic-discovery and summary update passes **98 tests** on the current
Python environment, JavaScript syntax validation, and package builds. Local
acceptance found seven storage profiles and over 700 retained observations with
no reported source errors. These are storage/profile counts, not verified
accounts or live-process counts. One initial live DS4F request summarized three
runs for a total reported charge of **$0.00008050196**; subsequent opt-in refreshes
have their own accounting in the dashboard. Tests use mocked provider responses.

## Local storage and cleanup

The selected `--data-dir` contains `events.sqlite3`, a private ingestion token,
and, when summaries are enabled, `summaries.sqlite3` for cached briefs/accounting.
Source config, validation databases, and build products are ignored by Git.
Retention is manual in this prototype, not automatic:

```sh
uv run agent-viz prune --data-dir .agent-viz/live --older-than-days 7
```

This deletes only eligible old observer records, never native transcripts. It
retains active SDK runs and parents needed by retained children. Stop the
collector before cleanup; do not run a second collector against the same data
directory. There is no remote control or native-history deletion endpoint.

## Product direction

Keep the Python SDK, local collector, and browser view small. Native Codex/Claude
sources were brought forward into the first functional version at the user's
request. Further framework integrations remain future work. Do not require a
particular model, cloud account, or paid summarizer.

The visual direction is **ghostly, terminal-like, and captivating**: quiet dark
surfaces, crisp monospace labels, restrained spectral accents, and clear motion
when something actually changes. The first theme now uses original inline SVG,
local CSS, and Git-like nodes/rails. The rails represent observed delegation,
not Git commits or invented merge relationships. Further visual refinement is open.

A deterministic synthetic scenario should exercise the real event pipeline and
be safe to record for GitHub and X. Demo mode must be visibly labeled; recordings
must not expose private prompts, repository paths, credentials, or real tasks.

## Boundaries

This is an observer, not an agent framework, task scheduler, or remote control
panel. It will not kill, steer, or approve actions in the first version. A pretty
dashboard must never hide lost events or pretend that an unresponsive agent has
definitely failed.
