# autopilot-code

Code-work entrypoint. Detect spec context and close the `plan → execute → test → report` loop at the selected intensity. This file defines routing and stage contracts; load the relevant reference only when its detailed policy is needed.

## Quick Contract

- Default output: `<artifact-root>/plans/<date>_<slug>/`. `direct` creates no durable plan; `quick` uses a micro-plan; `standard+` writes the plan, checklist, `pipeline_summary`, development logs, and test logs.
- When a spec exists, emit a one-line `spec-significance` judgment before editing code. Route spec-significant changes through an `autopilot-spec` update first.
- Recheck git and worktree state at entry and immediately before durable write-back or commit. Stop on an active merge/rebase, detached HEAD, or an unexpected HEAD change.
- Do not parallelize QA at every stage. Scale `plan-check` and final `code-test` from the rigor derived from intensity (CONVENTIONS §1.1).
- Follow an explicit artifact or audience language for user-facing reports. Otherwise, use the conversation language.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `context-and-guards.md` | Every invocation (required) | Artifact, spec, and git guards; spec-mode detection; design/app/library/API/CLI/research boundaries; experiment-ready input; invocation routing |
| `arguments-and-decisions.md` | When interpreting arguments, `--from`, pause/resume, or active-plan conflicts | Argument parsing, defaults, active/partial/complete plan handling, and plan-path resolution |
| `dev-pipeline.md` | When running `--mode dev` | Stage orchestration, plan check, retry behavior, and `analyze-project` update |
| `debug-audit.md` | When running `--mode debug` or `audit` | Debug diagnosis and fix flow; audit fan-out and autofix workflow |
| `pipeline-summary-safety.md` | At terminal, failed, partial, rollback, or summary states | Summary template, terminal-state reporting, and common safety rules |

## Argument Shape

`--mode dev|debug|audit <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`

Defaults:

- `--mode`: default to `dev`; infer `debug` when the request is centered on an error log or traceback.
- `--intensity`: choose from scope and risk. Use `direct` for a one-line task, `quick` for a small scoped change, and `standard+` for multi-stage or multi-file work. Verification rigor is derived from intensity rather than selected separately (CONVENTIONS §1.1).
- `--user-refine`: enable only when the user explicitly requests a review or note-taking pause.

## Stage Graph

| Intensity | Graph | Durable artifact | Review policy |
|---|---|---|---|
| `direct` | intake → produce → sanity/report | None | No independent QA |
| `quick` | intake → orient-lite → micro-plan → plan-check-lite → produce → verify-lite → report | None by default | Inline check with 3-4 questions |
| `standard` | (`frame` + `frame-alternative`) → code-plan → plan-check → code-execute → impl-review → code-test → code-report | Required | Run the route-declared 2-way framing exploration with `balanced-deep` + `light` profiles and distinct perspectives |
| `strong` | 3-way frame → 2-way plan → plan-check arbitration → execute → 2-way implementation review → test → report | Required | Spend cheap asymmetric breadth early, then converge through the declared arbiters; every group remains cross-harness-first. `execute` carries the SD-103 subdivision permission from `standard` up (routing-flex correction) — check it before dispatching a single long session (dev-pipeline Step 3) |
| `thorough`/`adversarial` | strong graph + 3-way plan and 3-way implementation review + deeper rigor | Required | Use the registry-declared third implementation-risk/failure-mode legs; never invent or widen a group outside the sealed route |

**`standard+` dispatch**: Run every durable compiled node as dispatch depth 2. Start each sealed `parallel_group` of 2–4 legs with one `dispatch-batch --parallel-group` transaction, never member-by-member. The dispatch-depth-1 conductor passes artifact paths, reads only verdict/status, and yields while the adapter supervisor joins the exact child batch. Cross-harness means at least two harness families across the group; model-profile and perspective asymmetry are independently sealed and reported. Use `dispatch-wait` only for an explicit `poll-fallback`. Only `direct` and `quick` keep micro-stages inline. The owner never sets `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN` or any other `AGENT_DISPATCH_*` lifecycle override, and never reads harness utility sources looking for one — the launcher evidence-binds that assertion to the launcher's own observed scope, and a registered headless owner inside a tool sandbox cannot make it. If the runtime hands a foreground `dispatch-batch` call to the background, that call has not failed: poll `dispatch-current --route <id>` or wait at the runtime join. Do not switch lifecycle.

## Mode Routing

- `dev`: add features, refactor, or implement. `direct|quick` shorten the full pipeline; `standard+` uses framing, `code-plan`, durable `plan-check`, optional `code-refine`, `code-execute`, `impl-review`, `code-test`, and `code-report`.
- `debug`: diagnose the root cause before planning a fix. Proceed when the cause is clear; ask for a choice only when materially different causes remain plausible.
- `audit`: inspect a codebase or app comprehensively and apply low-risk fixes. Keep review fan-out read-only; make and verify changes in a worktree based on current HEAD before harvest.

## Critical Gates

Before merge/commit: (1) run `python3 tools/generate.py`, (2) record new `utilities/*` and `tools/*` in the projected/deferred census, (3) run `python3 tools/generate.py --check`, and (4) run `./tools/check-adaptation-boundary.sh`. Step 4 is not redundant: `generate.py` fills Claude counterparts only for `loops/`, `scaffolds/`, `tools/memory`, `tools/install`, `tools/integrations`, and `tools/fleet`, so a new **top-level** `tools/<file>` passes `--check` and still fails the boundary guard — and `pre-push` runs both. Stage generated output with source; do not edit projections manually.

1. Resolve the artifact root by preferring `.agent_reports` and falling back to legacy `.claude_reports`.
2. Run git-state preflight and remember starting `HEAD`.
3. If `spec/` exists, read `spec/prd.md` and emit `spec-significance`.
4. Choose stage graph from intensity before QA.
5. Before source write-back or commit, re-run git-state preflight.
6. On any terminal state, write `pipeline_summary.md` before reporting to the user.

> Treat the [Reference Index](#reference-index) as the single source for reference files, load points, and contents.

## Post-Frame Direction Gate (SD-123)

This gate is mode-conditional, not universal. `standard+` `autopilot-code` routes
compiled under an explicit `hybrid`/`both`/`post-frame-only` `confirmation.mode`
seal `human_gates: ["frame-review"]` and the `frame` node's (and any frame
parallel-group clone's) continuation as `{"kind": "human-gate", "gate":
"frame-review"}` — steps 1-5 below apply to those routes. A non-composed route
compiled under the shipped `autonomous` default (O3) instead realizes that same
continuation as `{"kind": "inline-next"}` and seals an empty
`human_gate_bindings` for this gate: whatever node the compiled graph actually
places after `frame` (`plan` in the common graph, but `execute` in a graph like
`frame,execute,test` that has no `plan` node) starts immediately and steps 1-4
below do not apply. An explicit composed recipe keeps every gate it declares
regardless of the default confirmation mode, exactly like the explicit-mode
routes above. Whether this gate binds at all, and what it binds to, is always
the compiled `human_gate_bindings` and the graph's actual successor node —
never inferred from the mode name or a hardcoded node id. A route sealed before
`confirmation_mode` existed at all keeps whatever the recipe declared at compile
time and is never retro-fitted onto either shape; do not attempt to apply this
gate to an already-open route regardless of which shape it sealed.

1. After `frame` (and, at `standard`, `frame-alternative`) completes, before
   dispatching the compiled bound successor (read from `human_gate_bindings`
   and the graph's actual successor node — `plan` in the common graph, but
   not always): build `shards/frame/frame-summary.json` from
   `shards/frame/direction-brief.md` — exactly the five fields 방향 (direction),
   대안 (alternatives), 위험 (risk), 범위 변경 (scope change), 비용 (cost), total
   size ≤1KB. Reference it by **path** when raising attention; never embed the
   summary, a `required_action`, or a gate field inside a stage-advance receipt
   body (seam 3 — `utilities/dispatch_completion_join.py`'s v2/v3 receipt
   negotiation returns its body by identity when no advanced record exists).
   Then build the **interview** `shards/frame/interview.json` (schema
   `frame_interview_v1`, SD-129) from the briefs: `understanding` (one plain
   sentence restating what the user wants, which the user confirms or
   corrects), `brief` (problem / outcome / affected / constraints / open, each a
   few plain lines), and `questions` — only the decisions the briefs leave to
   the user. Rules, all checked by `utilities/frame_interview.py validate
   --intensity <intensity>` and refused by `gate --block` when broken: no
   harness words (route, owner, gate, node, shard, worker, …); one topic per
   question; at most two short sentences (≤160 chars); 2–4 options, each with
   a one-line "what choosing it means"; exactly one `recommended` option so the
   user can answer "yes" and move on; a `why` naming why only the user can
   decide it — a fact you can establish by reading code or running a tool is
   never a question, investigate it instead; at most 7 questions at
   `standard+` (3 at `quick`, 1 at `direct`), and nothing whose answer is
   already obvious. A tired reader must be able to answer every question
   without opening the plan. Questions the frame legs listed under "Questions
   only the user can answer" are the first candidates.
2. Raise the existing typed attention path (SD-78/108) with
   `required_action=human-gate:frame-review`, naming
   `shards/frame/interview.json` as the reviewable artifact
   (`workflow-supervisor.py gate --route <route file> --gate frame-review
   --block --artifact <absolute interview path>`); the interview references
   `frame-summary.json` by path. The depth-0 session puts the summary card and
   the questions to the user and records the answers on the release.
3. Wait for the release on the one checked surface (SD-129), in bounded
   foreground calls, doing nothing else in between:
   `python3 <agent-home>/utilities/workflow-supervisor.py await-release --route
   <route file> --gate frame-review --max 110` — exit 2 means still blocked:
   call it again; exit 0 (`status=proceed`) means a person released the gate,
   and the payload carries `released_by`, `artifact`, and any interview
   `answers`; exit 3 (`revise`) returns to `frame` under the `code-refine`
   retry boundary and the gate is raised again afterwards; exit 4 (`stop`)
   cancels the route with `abandon_reason=operator-decision`. Never release
   your own gate (`release`/`gate --release`) to move on: the frame gate is
   sealed `release_authority=depth-0` at the raise, so a registered owner's
   release is refused typed (`gate-release-authority-refused`) and would
   otherwise unblock a plan nobody confirmed. Write the interview in the
   `frame_interview_v1` shape only: an artifact that calls itself an
   interview under another schema is refused at the raise
   (`interview-schema-unsupported`). Never sleep, never write an ad-hoc polling
   loop, and never spawn the bound successor while `await-release` has not
   returned 0 — every launch surface refuses a successor start whose entry
   gate is not released (`human-gate-unreleased` / `human-gate-not-raised`,
   defect M). The person records the answer from the depth-0 session with
   `workflow-supervisor.py release --route <route file> --gate frame-review
   --decision proceed|revise|stop --actor <actor> --answers <answers file>`.
   `proceed` claims and reports the bound successor atomically (never spawn
   it a second time on retry); `revise` returns to `frame` under the
   `code-refine` retry boundary; `stop` cancels the route with
   `abandon_reason=operator-decision`. Pass `--answers-out
   shards/frame/interview-answers.json` to `await-release` so the recorded
   answers land in your cycle directory.
4. On `proceed`, render the agreed intent before anything else:
   `python3 <agent-home>/utilities/frame_interview.py render-intent --interview
   shards/frame/interview.json --answers shards/frame/interview-answers.json
   --out shards/frame/intent.md`. `intent.md` is the brief the bound successor
   reads first. When `successor == plan`, pass its absolute path in the plan
   prompt as `Intent:`; a plan that contradicts a recorded decision is a
   plan-check blocker. Any other successor receives the recorded intent
   through its own normal handoff, not an invented plan node. When the user
   corrected your understanding (`status: agreed-with-correction`), fold the
   correction into the successor's prompt/handoff verbatim. If the answers
   open a genuinely new decision, you may raise the gate once more with a
   round-2 interview (`round: 2`, same caps). A third interview raise is
   refused by the validator (`round` ≤ 2); remaining doubts go to the plan's
   risk section when `successor == plan`, and a third raise, if a route ever
   needs one, carries the frame summary alone.
5. `confirmation.mode` (`profiles/dispatch-defaults.yaml` /
   `utilities/dispatch-defaults.py`, default `autonomous`) governs this gate
   and has four values: `autonomous` removes this binding entirely, but only
   for a non-composed route doing routine, already-authorized `autopilot-code`
   work (`frame`'s continuation realizes as `inline-next`, steps 1-4 above do
   not apply); an explicit composed recipe keeps every gate it declares
   regardless of `confirmation.mode`. `post-frame-only`
   makes this gate the sole confirmation point; `hybrid` layers it onto the
   existing pre-plan notify; `both` makes both stages always explicit — read
   it via `query_confirmation_mode`, never hardcode a mode.
   `core/WORKFLOW.md` §0.4 owns the user-facing card text.

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-code.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-code
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `plans/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/plans/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Run `capability-route.py complete` for the terminal node(s) before running
   `capability-route.py close` on the same route — `close` reads the terminal
   completion markers to decide `terminal_gate_proven`, and by default now
   refuses (`route-close-before-complete`, exit 64) instead of permanently
   sealing a `false` proof when `complete` has not run yet. `--allow-unproven`
   exists only to record an intentionally abandoned route's honest `false`
   outcome for recovery/cutover bookkeeping — never pass it to route past a
   terminal node's ordinary completion. Never pass `--output` to
   `complete`: it targets an existing artifact path 1:1 and a collision would
   silently overwrite that artifact; the canonical completion marker location
   is written regardless.
5. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
6. Only then, if this capability owns a shared kind, `admit-shared`.
