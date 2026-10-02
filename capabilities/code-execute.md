# Capability: code-execute

This is the portable capability contract for `code-execute`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `code-execute` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Execute a plan step by step, delegate implementation to the development role, and record an execution log. |
| Argument shape | `<plan name or path>` |

## Invocation Semantics

Execute an implementation plan with progress tracking

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

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

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/code-execute/SKILL.md` and `skills/code-execute/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-execute/SKILL.md`, while `skills/code-execute/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info code-execute`. Use `adapters/codex/skills/code-execute/SKILL.md` as the native Codex Skill projection; do not consume `skills/code-execute/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info code-execute`. Use `adapters/opencode/skills/code-execute/SKILL.md` and `adapters/opencode/commands/code-execute.md` as native OpenCode projections; do not consume `skills/code-execute/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/code-execute/SKILL.md` and `adapters/claude/skills/code-execute/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-execute/SKILL.md`, while `skills/code-execute/SKILL.md` remains the compatibility reference kept for parity/drift checks.
