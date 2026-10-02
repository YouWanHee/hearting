---
name: code-execute
description: "Use only when autopilot-code dispatches the implementation stage for an approved plan. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/code-execute.md
  adapter: opencode
  invocation_class: parent-invoked
---

# code-execute

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-execute.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info code-execute`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/code-execute.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-execute`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-execute`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<plan name or path>`
- Portable meaning: Execute a plan step by step, delegate implementation to the development role, and record an execution log.

## Portable Contract

- Invocation semantics: Execute an implementation plan with progress tracking Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

`code-execute` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through), may call `utilities/artifact_producer.py begin --node <node id>`
on the same route to resume that cycle, and writes durable artifacts only inside
`<cycle_dir>/artifacts/<bucket>/...` within its artifact output scope.
`artifact_producer.py` owns the open cycle and its output paths; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

The route's `source/**`, `source-alternative/**`, and `tests/**` write scopes
refer to files in the assigned `route.cwd` worktree. They are not paths beneath
the artifact directory. Declared durable outputs and artifact scopes resolve
under the canonical cycle output directory. A normal single-session mutation
node sealed with `commit_expected: true` commits its own validated changes;
declared sub-session slices remain no-commit and leave the commit to their
owner after the stage gate.

For a `direct` inline route, the depth-0 interactive caller uses the single
public finish command and its exact receipt under `core/WORKFLOW.md §0.5`.
The same section defines current-session OPEN-route reuse after a proved merge
into an integration worktree. Registered owners and workers retain their
runtime terminal path. Stage-dispatch §13.61 and artifact-path-contract §43
govern the proof and the cycle's completion record; a finalized cycle's files stay editable (artifact-path-contract §45).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.


## Workflow Evidence

- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
