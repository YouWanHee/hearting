---
name: autopilot-draft
description: "Use when a new paper, presentation, proposal, or report has an independent audience and document goal. Not for an experiment's own evaluation/result report (lab), existing-document correction, or source-code implementation."
---

# autopilot-draft

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-draft.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info autopilot-draft`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-draft.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-draft ...`; `preflight.sh route autopilot-draft` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info autopilot-draft` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-draft`
- Invocation class: `entry-router`
- Supported modes: `paper, presentation, doc`
- Argument shape: `<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]`
- Portable meaning: Document-drafting pipeline that produces an applicable artifact through strategy, drafting, verification, and editing.



## Workflow Evidence

- Capability grounding only: `adapters/codex/bin/preflight.sh route autopilot-draft [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability autopilot-draft <complete compile arguments>`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
