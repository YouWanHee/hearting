---
name: autopilot-lab
description: "Use when experiment setup, empirical evaluation, failure reproduction, or the experiment's own analysis and result report is needed, including fixed results without new measurements. Not for reusable evaluation-program implementation, independent paper/presentation drafting, or fixed-data document layout/caption edits."
---

# autopilot-lab

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-lab.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info autopilot-lab`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-lab.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-lab ...`; `preflight.sh route autopilot-lab` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info autopilot-lab` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-lab`
- Invocation class: `entry-router`
- Supported modes: `setup, eval`
- Argument shape: `<task description> [--mode setup|eval|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--report] [--from spec|scaffold|run|eval|summary]`
- Portable meaning: Experiment training setup, evaluation/analysis, and result reporting, including reports from fixed results.



## Workflow Evidence

- Capability grounding only: `adapters/codex/bin/preflight.sh route autopilot-lab [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability autopilot-lab <complete compile arguments>`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
