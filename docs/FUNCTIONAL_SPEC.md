# Functional specification

## TL;DR

Build a small library that sends safe agent activity events to a local browser
dashboard. Show current work, real parent/subagent relationships, and honest
status. Prove those functions with tests and a synthetic demonstration before
investing in the ghostly terminal visual design.

This began as the implementation plan. A local functional prototype now covers
the event pipeline, SDK/demo, plain browser, and read-only multi-profile Codex and
Claude adapters. Automatic discovery, opt-in bounded DS4F briefs, and a first
Git-log-inspired terminal theme now extend that baseline. Native adapters were brought forward at the user's request on
October 20. The README and validation record distinguish working behavior from
remaining targets; the ten-minute load target below is not yet measured.

## 1. First user journey

1. Start one local collector and open its browser dashboard.
2. Add a few SDK calls to a Python application; do not replace its agent loop.
3. Run a coordinator that starts a researcher and a tester in parallel.
4. See each run appear under its actual parent with a short task and activity.
5. Watch one run wait for a tool, another request user input, and another finish.
6. Open a run to see its recent operations, state changes, and result summary.
7. Refresh the page or reconnect without losing the tree or inventing outcomes.

## 2. First-version capabilities

| Capability | Required behavior |
| --- | --- |
| Active-run overview | Run name, project label, source, task, activity, state, duration, and last update; filter active/waiting/finished. |
| Nested run tree | Explicit parent IDs, arbitrary supported nesting, expandable children, and direct-child counts; names are labels, never identity. |
| Short activity text | Preserve original task separately from current activity, such as “Investigating the timeout test.” Record who supplied the text. |
| Work status | Show working, waiting for tool, waiting for child, waiting for user, idle, or a confirmed terminal outcome. |
| Telemetry health | Show connection freshness independently from work status, with last-seen time and loss warnings. |
| Run detail | A bounded, paginated timeline with tool/model operation start/end, errors, activity changes, and final summary. |
| Live browser updates | Snapshot on first connection, then ordered updates; recover correctly after page refresh and brief disconnects. |
| Recent history | Retain completed runs locally across collector restarts; provide documented retention and explicit local deletion. |
| Manual SDK | Sync and async application integration; tracking must not crash or block the actual agent when the observer is unavailable. |
| Synthetic demo | The same SDK/collector path as real runs, with parallel children, a nested child, a recovered error, waiting, and lost telemetry. |

Do not make costs, token charts, a workflow editor, or an animated graph a
dependency of the first useful version. A correct tree and readable activity
cards are enough for the first functional browser view.

## 3. Identity and state rules

An **agent definition** is reusable; a **run** is one execution. Every new execution
gets a fresh `run_id`, even if the agent name and task repeat. `parent_run_id`
describes logical delegation, not an operating-system process relationship. A
handoff is not automatically a new child unless an adapter has evidence for it.

Store two separate dimensions:

| Dimension | Values and meaning |
| --- | --- |
| Execution state | `working`, `waiting_tool`, `waiting_child`, `waiting_user`, `idle`, `completed`, `failed`, `cancelled`; changes come from explicit events. |
| Telemetry health | `fresh`, `stale`, `disconnected`, or `unknown`; derived from heartbeat receipt and transport evidence. |

Rules:

- A heartbeat says the producer is reachable, not that the agent is making progress.
- Missing heartbeats produce “last reported working; telemetry stale,” not failure.
- Only confirmed run outcomes create completed/failed/cancelled states.
- A failed tool call is an operation error, not automatically a failed run.
- Parent completion must not finish children. Show remaining active descendants.
- A child arriving first stays visible under an unresolved-parent placeholder;
  attach it when the parent arrives. Reject cyclic and cross-session parent links.
- Parent identity is fixed once declared. Do not guess a parent from similar names.
- Terminal runs cannot be revived by a delayed heartbeat or status event. A new
  execution uses a new ID.
- Inferred adapter states must be visibly marked inferred; “unknown” is preferable
  to false certainty. Codex/Claude transcript adapters are now in the functional
  version; they are observations, not a universal runtime-control protocol.

## 4. Minimal event contract

Common fields:

`schema_version`, `event_id`, `session_id`, `run_id`, `parent_run_id?`,
`producer_id`, `sequence`, `occurred_at`, `type`, and `payload`.

The collector adds its own `received_at` and monotonically increasing stream
cursor. Timestamps are for display; producer sequence is for ordering. Do not
assume different machines or processes have synchronized clocks.

| Event | Payload intent |
| --- | --- |
| `run.started` | Display name, project label, short task, source, optional model label. |
| `run.activity` | Current one-line activity and provenance: application or adapter. |
| `run.state_changed` | Explicit nonterminal execution state and optional short reason. |
| `operation.started` | Unique operation ID, tool/model kind, and a safe display label. |
| `operation.finished` | Matching operation ID, success/error outcome, and safe short detail. |
| `run.heartbeat` | Liveness only; no implied work-status transition. |
| `run.finished` | Completed/failed/cancelled, optional short result summary. |

Schema implementation must bound strings and payload sizes, validate event types,
and define allowed transitions. Parallel operations and retries need distinct
operation IDs. One collector transaction persists accepted events and updates
their current state. Duplicate event IDs must have no duplicate effect; conflicting
duplicates are errors. Detect sequence gaps and delayed events, preserve them in
the audit timeline, and prevent old updates from overwriting newer state. Exact
sequence/reconnect behavior is a milestone-one design decision with tests.

## 5. Integration and transport

Implemented shape: Python 3.12+ SDK; a small standard-library Python HTTP service
using SQLite; browser snapshots plus Server-Sent Events (a one-way live update
stream); buildless HTML/JavaScript. No runtime package dependency is needed.
Full snapshots and a persistent journal ID avoid missed-delta recovery ambiguity.

- SDK concept: start a session, track a root run, create child runs, report safe
  activity/operations, finish runs, and close with a bounded flush.
- Each SDK producer has a bounded background queue, bounded request timeouts,
  retry/backoff, and visible delivery diagnostics. Event loss must never be silent.
- Use bounded at-least-once retry with stable event IDs, not an exactly-once claim.
  Retain pending events until acknowledged; define the overflow policy before code.
- No SDK network work on import, no global HTTP monkeypatching, and no automatic
  prompt/header capture. Observer failure must not change the agent's result.
- Support multiple producers and sessions in one collector. Use one SQLite writer
  path with transactional updates, rather than shared unprotected in-memory lists.
- Browser resume uses its last cursor. If history has expired, send a fresh
  snapshot and an explicit reset instead of silently skipping updates.
- After collector restart, rebuild the same state; previously active runs have
  unknown/stale telemetry until new evidence arrives. Do not mark them completed.
- The manual SDK and bounded read-only Codex/Claude storage adapters work now.
  One Python framework adapter remains future work, provisionally PydanticAI.
  Do not promise automatic discovery or exact liveness of arbitrary running agents.

## 6. Privacy and local boundaries

- Bind to loopback only by default. Validate Host and Origin, use a local ingestion
  token, and reject cross-origin browser writes. “Local” does not mean trusted.
- Store credentials outside browser assets and never include them in recordings.
- Default events contain labels, IDs, states, and short caller-selected text, not
  raw prompts, tool arguments/results, file contents, auth headers, or full paths.
- Even a task label can be private. Document that clearly; bound and escape all
  text, support application redaction, and never treat it as HTML or instructions.
- Avoid URLs as automatic clickable links without validation. Prevent stored text
  from executing in the browser.
- No outbound telemetry, hosted account, automatic external summarizer, or paid
  provider calls. Any future content-based summary must be explicit opt-in.
- Demo fixtures use invented projects and tasks. Keep private recordings ignored;
  only reviewed synthetic media could be published later.

## 7. Acceptance checks before calling the MVP working

1. Two applications can report concurrent sessions without identity collisions.
2. A three-level agent tree survives child-before-parent delivery and page refresh.
3. Same-name runs remain distinct; cycles and invalid parent relationships fail.
4. A parent can finish while a child remains visibly active.
5. All explicit waiting states and confirmed terminal states render correctly.
6. Missing heartbeat changes health only; reconnect restores health without
   erasing the current task or reviving terminal runs.
7. Duplicate, delayed, conflicting, and gapped events have tested behavior. A lost
   acknowledgment followed by retry does not duplicate an operation or result.
8. Parallel operations finish independently; a recovered tool error does not fail
   the entire run.
9. Starting the dashboard after agents, browser reconnection, and collector restart
   all recover the correct available state with explicit loss/reset warnings.
10. Collector downtime, full SDK queues, and shutdown timeouts do not fail the
    agent application; delivery loss is observable and memory remains bounded.
11. Oversized/malformed events, script-like labels, invalid tokens, and untrusted
    origins fail safely. Captured payloads contain no default raw content.
12. A fixed-seed synthetic demonstration runs with no model key and no provider
    calls, through the same SDK path, with recognizable results after each run.

Performance target to measure, not claim yet: 50 visible concurrent runs and
10 events/second for ten minutes, with connected local updates normally visible
within one second. Record environment, p95 delay, queue limits, and any dropped
events. Larger-scale production monitoring is not a first-version claim.

## 8. Build order

| Milestone | Deliverable and exit condition |
| --- | --- |
| 1. Contract and reducer | Event schema, state transitions, tree rules, reconnect/overflow policy, and pure deterministic tests. |
| 2. Local pipeline | SQLite collector, ingest validation, snapshot and resumable event stream; restart and origin/token tests. |
| 3. SDK and demo | Small sync/async client, heartbeat, bounded delivery, parent/child context; runnable synthetic scenario. |
| 4. Functional browser | Plain overview/tree/detail views and real live updates; browser tests for the first user journey. No polished theme yet. |
| 5. Real integration | One framework adapter verified against a public test application; compare displayed events with actual run history. |
| 6. Visual identity | Ghostly terminal visual system, keyboard access, reduced-motion mode, and clearly labeled synthetic capture for GitHub/X. |

Keep independently useful milestones reviewable; do not manufacture a fixed PR
count. No remote creation, publishing cadence, or push is authorized by this plan.

## 9. Deliberately later

Additional CLI sources beyond current Codex/Claude discovery; framework/language adapters; remote
machines; shared/team authentication; stop/steer/approval controls; model-based
summaries; billing dashboards; terminal UI; workflow editing; evaluation or model
routing. Consider each only after the small observer works well.
