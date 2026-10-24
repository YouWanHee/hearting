# code-execute

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Resolve the plan path from arguments, read `plan.md` from disk, and never depend on prior-stage conversation. This is the only source-mutating stage. Its write class is assigned source files (including tracked docs/configuration examples), `checklist.md`, `dev_logs/`, `_internal/dev_reviews/`, and plan-frontmatter `status`. Source ownership is defined by scope and tracked content, not filename extension. A commit-expected single session commits all validated owned source paths; artifact shadows, runtime state, and unrelated dirty files are excluded. The stage runs as the compiled route's `dev/*` unit (sealed at compile); it never dispatches units itself, and any unforeseen narrow scaffolding uses an ephemeral native helper without unit semantics.

> **Plan resolution**: Treat [arguments-and-decisions.md#plan-resolution](../../autopilot-code/references/arguments-and-decisions.md) as the single authority for resolving `$ARG` to a plan path.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../../autopilot-code/references/arguments-and-decisions.md). Do not infer a fixed execution-log or report language from this skill file.

## Slice Mode

If the assignment is a parallel sub-session slice (a phase brief, fixed files, `stage_authority=0`), this is all you do: edit only the fixed files, run only the slice's verify command, and report the changed files and the verify result. Skip the Git Safety Checkpoint, phase commits and rollback below — a slice makes no git writes (no add, commit, checkout, restore, stash, reset), reads git state with `GIT_OPTIONAL_LOCKS=0`, and leaves `checklist.md` and the dev log to the owner. If a file outside the list is needed, stop and hand off.

## Source state and commit ownership

Use the runtime-provided worktree and preserve its entry HEAD and existing dirty
state. Do not fetch, pull, create a safety commit, or commit per phase. A
commit-expected single-session stage commits only its validated owned paths;
a sub-session slice makes no Git writes. The route/runtime owns completion and
recovery mechanics. Record the changed paths and handoff in the declared artifact
locations.

## Initialization

1. Read the resolved plan at `$ARG`.
2. Set `{log_dir}` to the task root two levels above `plan.md`.
   - Example: `$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor_engine/plan.md` → `$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor_engine/`
3. Detect resume state.
   - If `{log_dir}/checklist.md` contains `[x]`, `[FAIL]`, or `[SKIP-DEP]`, update its `Safety commit:` line, skip completed steps, and continue at the first `[ ]` step.
   - Otherwise create a fresh checklist.
4. Run `mkdir -p {log_dir}/dev_logs {log_dir}/_internal/dev_reviews` and write `{log_dir}/checklist.md` directly from the canonical plan:

   ```text
   Safety commit: {$SAFETY_COMMIT}

   Phase A: [description]
     [ ] Step 1: [file] — [what to change]
     [ ] Step 2: [file] — [what to change]
   Phase B: [description]
     [ ] Step 3: [file] — [what to change]
   ```

Use the checklist as the sole orchestration tracker. Keep plan files immutable during execution. Mark each step `[x]`, `[FAIL]`, or `[SKIP-DEP]`.

## Execution Rules

- Read the checklist before every step and execute steps in dependency order.
- Implement each step as the node's `dev/*` unit (default `dev/backend`; the compiled route seals the member) with concrete file and change instructions.
- Give each worker `{log_dir}/dev_logs/` and require a step log such as `dev_logs/step_01_model_py.md`.
- Run independent steps in parallel when the active workflow supports in-session parallel delegation.
- After a successful step, mark it `[x]`. The phase review owns syntax and import verification.
- Continue until every processable step has a terminal checklist mark.

## Assurance

Use the intensity and selected stage graph as the assurance authority. A plan's
`qa_level` is descriptive and does not override selected roles, reviewer counts,
or the graph. Do not choose extra reviewers or passes from file count.

## Handoff

Record each implementation step in the declared dev log with the changed files,
commands and outcomes, deviations, unfinished items, and relevant risks. The
selected sibling review node owns implementation review; do not dispatch it or
require an additional pass from this procedure. Keep correction and recovery
within the compiled route and preserve unrelated work.

## Final Report

Read the checklist and report the overall verdict in the selected audience language. List only `[FAIL]` and `[SKIP-DEP]` steps with reasons; when all steps succeeded, a concise success verdict is sufficient. End by recommending `code-test <plan file path>` for functional verification. In a standalone invocation, this is the only user-facing progress report.

## Task

Execute the plan at: $ARG
