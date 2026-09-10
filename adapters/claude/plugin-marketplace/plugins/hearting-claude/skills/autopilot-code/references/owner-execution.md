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

Apply steps 1–4 only when compiled `human_gate_bindings` retains `frame-review`:
explicit `hybrid`/`both`/`post-frame-only` standard+ routes seal that human gate
and frame/clones' `human-gate` continuation; explicit composed recipes retain
all declared gates regardless of mode. Shipped `autonomous` (O3) non-composed
routes instead seal no binding and `frame` continues `inline-next`, immediately
starting its actual successor. Read bindings and graph, never infer from mode
or hardcode `plan`: `frame,execute,test` has `execute` as successor. Pre-mode
routes retain their compile-time recipe; no retrofitting this gate onto open
routes. Step 5 defines mode selection.

1. After frame (including standard's alternative) and before its bound
   successor, build `shards/frame/frame-summary.json` from
   `shards/frame/direction-brief.md`: exactly 방향 (direction), 대안
   (alternatives), 위험 (risk), 범위 변경 (scope change), 비용 (cost), total ≤1KB.
   Raise attention by **path**; never embed summary, `required_action` or gate
   fields in stage-advance receipt bodies (seam 3: v2/v3 negotiation in
   `utilities/dispatch_completion_join.py` returns the body by identity when
   no advanced record exists).
   Then build `shards/frame/interview.json`, schema `frame_interview_v1`
   (SD-129), from briefs: `understanding` is one plain sentence for user
   confirmation/correction; `brief` has problem/outcome/affected/constraints/open,
   each a few plain lines; `questions` contains only unresolved user decisions.
   `utilities/frame_interview.py validate --intensity <intensity>` checks these
   rules and `gate --block` refuses violations: no harness words (route, owner,
   gate, node, shard, worker, …); one topic/question; ≤2 short sentences and
   ≤160 chars; 2–4 options with one-line consequences; exactly one recommended
   option, answerable by “yes”; `why` explains why only the user can decide.
   Investigate code/tool-answerable facts instead of asking. Cap questions at
   7 standard+, 3 quick, 1 direct; omit obvious answers. A tired reader must
   answer without opening the plan. Start with frame briefs' “Questions only
   the user can answer”.
2. Raise the existing typed attention path (SD-78/108) with
   `required_action=human-gate:frame-review`, naming
   `shards/frame/interview.json` as the reviewable artifact
   (`workflow-supervisor.py gate --route <route file> --gate frame-review
   --block --artifact <absolute interview path>`); the interview references
   `frame-summary.json` by path. The depth-0 session puts the summary card and
   the questions to the user and records the answers on the release.
3. Wait only through bounded foreground `python3 <agent-home>/utilities/workflow-supervisor.py
   await-release --route <route file> --gate frame-review --max 110
   --answers-out shards/frame/interview-answers.json`, with no intervening work.
   Exit 2: still blocked, repeat the call. Exit 0 (`status=proceed`): human
   release, carrying `released_by`, `artifact`, interview `answers`. Exit 3
   (`revise`): return to frame via `code-refine`, then raise again. Exit 4
   (`stop`): cancel with `abandon_reason=operator-decision`.
   Never self-release (`release`/`gate --release`), sleep, invent polling loops,
   or start the successor before exit 0. Raise seals `release_authority=depth-0`;
   owner release refuses `gate-release-authority-refused`. Every start surface
   refuses unreleased entry gates (`human-gate-unreleased` /
   `human-gate-not-raised`, defect M). Interviews require `frame_interview_v1`;
   other interview schemas refuse `interview-schema-unsupported` at raise.
   The depth-0 person records `workflow-supervisor.py release --route <route
   file> --gate frame-review --decision proceed|revise|stop --actor <actor>
   --answers <answers file>`. Proceed atomically claims/reports the bound
   successor: do not spawn it again on retry. Revise/stop follow the exit
   semantics above; `--answers-out` saves the recorded answers in the cycle.
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
5. Read `confirmation.mode` through `query_confirmation_mode`
   (`profiles/dispatch-defaults.yaml` / `utilities/dispatch-defaults.py`), never
   hardcode it. Default `autonomous` removes only routine already-authorized,
   non-composed autopilot-code's frame binding as above; `post-frame-only`
   makes this the sole confirmation; `hybrid` adds it to pre-plan notify;
   `both` makes both explicit. Composed gates remain. `core/WORKFLOW.md` §0.4
   owns the card text.

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
   write under `shared/` or an active-cutover legacy top-level bucket.
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
