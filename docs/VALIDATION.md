# Functional checkpoint — October 20, 2026

This is a historical validation record, not a current test report or a guide to
present defaults. In particular, native request and reply excerpts are now
shown by default; see the [source guide](SOURCES.md#privacy). The current Python
requirement is in [pyproject.toml](../pyproject.toml).

## Automatic discovery and summary update (October 20, 2026)

The original checkpoint below records the earlier functional baseline. The
updated prototype passed 98 tests in the checkpoint's Python environment, JavaScript
syntax validation, and wheel/source builds. Added tests cover discovery limits,
archived Codex sessions, safe summary inputs, mocked billing, persistent caches,
unknown-cost pauses, and active-first browser rendering.

Automatic discovery on this machine found seven supported storage profiles and
over 700 retained observations with no reported source errors. Copied histories
remain separate namespaces; these are not seven verified accounts or 700 live
processes. Native snapshots and transcript text are not committed fixtures.

One initial authorized live DS4F batch returned three summaries and a reported
total charge of $0.00008050196. It was one request, not three charges. The local
dashboard can perform capped, cached refreshes when explicitly launched with
summaries enabled; its persistent ledger holds the current total. Bounded task
excerpts leave the machine through OpenRouter in that mode. Redaction is best
effort and does not make confidential text safe to share automatically.

The first spectral terminal theme includes source cards, evidence-window
filtering, and Git-log-inspired delegation connectors. Recent working branches
sort before historical evidence. Execution state remains independent of source
freshness and model-generated descriptions. The local user-facing service is
left running by request; nothing has been pushed or published.

## Initial checkpoint summary

Working local SDK, collector, synthetic demo, plain browser, and read-only
Codex/Claude multi-profile observation. No public push, paid provider call,
account change, upstream code execution, or native hook installation.

## Automated checks

- 63 tests pass on Python 3.12.12 and Python 3.14.6. The optional Node DOM test
  ran, not skipped. The project declares Python 3.11+, but 3.11 was not exercised
  in this checkpoint.
- Tests cover strict event schemas, forbidden raw-payload fields, duplicate IDs,
  conflicts, sequence ordering, immutable identity, tree cycles, cross-session
  rejection, child-before-parent delivery, parallel operations, and terminal states.
- Restart and journal replacement, snapshot recovery, SSE cursor precedence,
  paginated history, bounded offline SDK delivery, stable retry IDs, disabled
  redirects/proxies, and explicit observer pruning have regression coverage.
- HTTP checks cover ingestion authentication, invalid/oversized JSON and cursors,
  hostile Host/Origin, escaped text, static asset security headers, and loopback bind.
- Browser DOM tests cover filtering with parent context, nested/orphan visibility,
  script-like text rendered literally, and history reset after a replaced journal.
- `node --check src/spiritboard/static/app.js` passes. `uv build` produces a wheel
  and source distribution; the installed wheel includes the browser assets and
  starts the command-line interface outside the checkout.

## Synthetic end-to-end test

The real HTTP collector accepts the model-free SDK scenario with zero delivery
failures. Five independent identities form a three-level tree; a failed synthetic
tool operation is followed by a successful one. The coordinator finishes before
a worker, four runs complete, and a deliberate unfinished run becomes stale
without acquiring a fabricated failure or completion.

Manual browser QA used only synthetic data. The initial snapshot, live connection,
tree, nested recovery detail, state filter with ancestor context, and page refresh
worked. The browser error/warning log was empty. The view remains intentionally
functional rather than visually polished.

## Existing local profile acceptance

Four selected personal/work storage profiles were tested: two Codex and two
Claude. Account credentials were neither needed nor read. These are storage
labels, not assertions about authenticated identities or active process counts.

Using 50 recent files per profile:

- 199 distinct observations; 85 parent associations; all four profiles represented.
- No source errors through the actual collector HTTP snapshot.
- All native states visibly marked inferred; no private source roots in public
  profile metadata; source text capture disabled.
- Scan windows `1 → 50 → 3 → 50` did not alter native identity or produce errors.
- All 200 selected source-file size/mtime signatures were unchanged during the
  final controlled audit. The code opens native files read-only and does not
  install hooks or modify vendor configuration.

A separate running CLI check used 30 recent files per profile: 119 observations,
57 parent associations, four profiles, no source errors, and no absolute user
paths in the served snapshot. These counts will change as real sessions change.
No private transcript sample, snapshot, task text, or account path is a test fixture.

## Deliberate limits

- Native transcript formats are observed implementation details, not stable vendor
  APIs. SDK events are explicit; file-derived states are not proof of process liveness.
- Claude sidechain/session metadata associates subagents with their owning session.
  It may not reveal the exact direct parent of every nested native delegation.
  Do not interpret inferred associations as verified arbitrary-depth lineage.
- Native event history starts when observed by this collector, not at the beginning
  of the entire vendor transcript. Headers/tails are bounded; middle events may be absent.
- Native text excerpts are opt-in and not guaranteed redacted. Default metadata
  still includes project basenames and profile labels, which can be private.
- SDK methods require application instrumentation; no generic framework adapter
  or automatic progress summarizer ships yet. Heartbeats are explicit.
- SDK delivery is bounded best effort, not durable exactly-once delivery. Look at
  dropped-event diagnostics and sequence warnings. Data is retained until explicit
  pruning. SQLite data directories are intended for one collector at a time.
- The ten-minute / 50-concurrent-run performance target is not yet validated.
- Python 3.11, remote machines, multi-user access, large histories, and production
  deployment are not certified by this checkpoint.

## Reference and skill boundaries

Reference clones remain ignored, with revisions and license notes in the lockfile.
No third-party source or visual assets were copied. Sites guidance kept preview
and browser checks local-only; OpenAI Docs guidance was used to distinguish the
documented app-server API from the prototype's inferred on-disk adapter. Official
read-only thread APIs exist, but attaching to arbitrary already-running app-server
instances is not implemented here: [Codex App Server API overview](https://learn.chatgpt.com/docs/app-server#api-overview).
