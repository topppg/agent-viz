# Reference projects

## TL;DR

Seven upstream repositories were shallow-cloned on October 20, 2026 into the
ignored `references/` directory. Learn interaction and event-design patterns;
build our own implementation. Their docs and selected code were inspected, not
executed. Exact revisions are recorded in [references.lock.json](../references.lock.json).

## What to study

| Local checkout / upstream | Useful idea | Important boundary |
| --- | --- | --- |
| `agenthud` — [AgentHUD](https://github.com/IAMMARBIT/AgentHUD) | Small explicit SDK, per-agent cards, initial snapshot followed by live events. | Flat IDs, no real parent tree; task text supplied by the app. Activity animation is not a liveness heartbeat. |
| `claude-agent-monitor` — [Claude Agent Monitor](https://github.com/reubenrajan/claude-agent-monitor) | Overview, collapsible child hierarchy, task/status board and run timeline. | Claude-specific hooks; some child completion matching is heuristic. Do not complete a child by matching a description. |
| `agent-top` — [agent-top](https://github.com/kannandreams/agent-top) | Dense readable overview, process vs logical-child distinction, missing-parent visibility. | Terminal tool and transcript/process discovery, not a general embedded SDK. List-price spend is not a verified invoice. |
| `cyberark-agentwatch` — [AgentWatch](https://github.com/cyberark/agentwatch) | Small-library entry point and separate local browser service. | Interaction graph is not a task tree; HTTP interception can capture sensitive headers/bodies. Do not use that collection model by default. |
| `atc-claude-kanban` — [Atc.Claude.Kanban](https://github.com/atc-net/atc-claude-kanban) | Task-focused board, project groups, active-child panel and file-driven updates. | Native task files are not always available; some labels infer state from transcript activity. |
| `agent-dashboard` — [AgentDashboard / Tnega](https://github.com/YusufKaya00/AgentDashboard) | Unified inventory across runtimes and explicit disclosure of inferred status. | No reuse license at the pinned revision. Concept/reference only; do not copy source or assets. |
| `agentops` — [AgentOps](https://github.com/AgentOps-AI/agentops) | Framework normalization and nested operation records; useful larger-system comparison. | Full app involves much more infrastructure. SDK and app licenses differ; do not copy the dashboard into our project. |

## Useful source anchors in the pinned clones

These are inspection starting points, not dependencies or guarantees of behavior.
Line numbers refer to the local revisions in the lock file.

- AgentHUD: `agenthud/sdk.py:97` has task/activity methods;
  `agenthud/server.py:38` defines flat agent state; `:115` sends the initial
  snapshot; `:167` replaces/removes records on spawn/despawn. The SDK queue and
  transport at `agenthud/sdk.py:203` need stronger bounds and delivery reporting
  for our design.
- AgentWatch: `src/agentwatch/hooks/http/httpcore_hook.py:24` captures HTTP data;
  `src/agentwatch/graph/models.py:19` defines component/interaction nodes. Learn
  the separation of concerns without adopting implicit raw-content capture.
- Tnega: `README.md:254` distinguishes observation from authoritative status;
  `README.md:388` explicitly records the absence of a reuse license.
- AgentOps: compare root `LICENSE` with `app/LICENSE`, rather than assuming the
  root license applies to every subdirectory.

## License and attribution ledger

This ledger records the files inspected; it is not a blanket permission to copy
all code, assets, dependencies, names, or trademarks in a repository.

| Reference | License files / observed scope | Our policy now |
| --- | --- | --- |
| AgentHUD | Root `LICENSE`: MIT. | Learn patterns; no code/assets copied. |
| Claude Agent Monitor | Root `LICENSE`: MIT. | Learn patterns; no code/assets copied. |
| agent-top | Root `LICENSE`: MIT. | Learn patterns; no code/assets copied. |
| CyberArk AgentWatch | Root `LICENSE`: Apache-2.0; `NOTICES.txt` present. | Learn patterns; preserve applicable notices if later reusing code. |
| Atc.Claude.Kanban | Root `LICENSE`: MIT. | Learn patterns; no code/assets copied. |
| Tnega / AgentDashboard | No root license; README says reuse/redistribution rights are not granted. | Reference only; no copying. |
| AgentOps | Root SDK: MIT; `app/LICENSE`: Elastic License 2.0. | Reference only for the app/dashboard; inspect exact source license before any SDK reuse. |

If we later reuse meaningful code from a permissively licensed source, record
the exact source/revision, keep its required copyright/license notices, and review
the relevant subdirectory and asset terms. Do not treat “public GitHub repo” as
“free to redistribute.” Do not publish the local reference clones as our work.

## How these references influence our scope

Adopt the small explicit-event interface, real parent IDs, snapshot-plus-updates,
and a readable active-run view. Improve on fragile patterns: guessing completion
from silence, conflating transport health with task outcome, globally patching
HTTP clients, unbounded buffers, or matching children by display name.

Do not clone another project's skins or create an all-in-one control platform.
The goal is a small useful observer with its own ghostly terminal identity.
