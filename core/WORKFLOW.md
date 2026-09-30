# Autopilot-* Routing Map — Agent-Facing Core

> A compact map for the main agent to route a task request to capabilities and roles. Each adapter maps them to its runtime-native skills, commands, agents, or profiles. Do not force symmetry; separate work according to its nature.
>
> The root `README.md` owns the user-facing meaning map and entry list. `CONVENTIONS.md` owns QA, model, and folder definitions. This document contains routing tables only, avoiding duplicated narrative and invocation examples.

---

## 0. Invariants — One Router and the Artifact Order Convention

This is the single routing contract for spec-backed projects that contain `.agent_reports/spec`, with legacy `.claude_reports` compatibility. Read it on demand when the adapter's status or reminder surface indicates routing is due; hooks expose runtime state but do not replace or eagerly inject this contract.

Every task first passes through the work-nature map in §2. Direct work, runtime plugins, and built-in Skills are used only where this router places them. Adapter and runtime projection work also remains core-first: establish the portable invariant in `core/`, read its governing document, then change adapter or generated output.

After the §0.1 read-only exemption and §0.2 semantic precedence, a request that
clearly matches one manifest `entry-router` positive trigger and none of that
entry's exclusion boundary uses that entry as the primary route. If multiple
entries match, resolve them through §0.2 and the work-nature map instead of
silently dropping to capability-free work.

Capability-free inline work is limited to read-only orientation, status, a
simple factual or explanatory answer that requests no new durable artifact, or
an explicit conversational/no-files constraint.
`direct` is an intensity inside the selected entry route, not a bypass around entry routing.

Before material work or read-only context recovery proceeds, confirm that the
current main-session prompt received its bounded capsule candidate probe. The
probe searches mechanically but does not decide relevance; inspect a candidate's
full record before applying it and ignore unrelated candidates. If the prompt
hook is unavailable or failed, record `recall` with one focused query or `skip`
with a short contextual reason through `mem recall-gate`. Neither path stores the
raw prompt, classifies it, or prescribes topic categories. Registered route-bound
workers do not run this main-session lifecycle and remain exempt from its receipt.

### 0.1. Read-Only Orientation Before Capability Routing

Before selecting a capability or Skill, distinguish read-only orientation from
work that creates or refreshes a persistent artifact. A request whose desired
outcome is to understand the project, recover prior context, resume from the
current state, or report status is orientation when it does not also ask for a
new analysis or a modification. These examples describe intent; they are not a
keyword classifier.

Read-only orientation invokes no capability and writes no artifact. Recover
context in this order:

1. Record `recall` at the memory opportunity gate with one targeted query from
   the task, then search before broad discovery. This is an agent judgment for
   orientation, not a prompt-keyword classifier. A shortened, ellipsized, or
   otherwise insufficient hit is only an index: read the full body by record ID
   before using it as evidence. Record `applied` or `miss` against the gate id
   after the evidence decision.
2. Use the adapter status surface and `utilities/artifact-root.sh` to resolve
   the project-wide canonical artifact root. In a linked worktree, ignore its
   tracked artifact snapshot and read the primary worktree's canonical root.
   Prefer canonical `.agent_reports/`; only when it is absent, use an existing
   legacy `.claude_reports/`.
3. Read existing state before a broad source census: the newest relevant
   `pipeline_summary.md`, `pipeline_state.yaml`, `summary.md`, `REPORT.md`, or
   `STORY.md`; the latest experiment contract and `experiments/_RUNLOG.md`;
   and the current `spec/prd.md` or task-specific specification. Read only the
   subset needed to orient, and follow any relevant pointers from memory.
4. Inspect primary code, data, and raw logs only when recovered contracts leave
   a material question unanswered or must be checked against live behavior.

Resolve conflicts with this evidence precedence:

```text
latest specification or user-confirmed decision
  > durable project fact
  > latest experiment contract
  > legacy document
```

Live primary behavior is validation evidence, not permission to silently
rewrite an explicit current contract. When a lower-priority source differs,
report the drift and identify both sources; do not merge their meanings or
quietly choose the legacy value.

An explicit request to analyze existing primary code, paper, or document
materials selects `analyze-project` as the default primary when no usable
persistent analysis exists. Treat the analysis request as artifact-producing
unless the user explicitly asks for conversational/read-only analysis or no
files. Existing analysis that is demonstrably stale for downstream work, or an
explicit refresh request, also makes `analyze-project` eligible. Artifact
absence by itself never selects the capability, and empirical evaluation,
external research, implementation, and completed-artifact inspection retain
their §0.2/work-nature primaries. When analysis already exists, read it before
deciding that reanalysis is needed.

This boundary was strengthened after a 2026-07-14 incident where a context
recovery request in a spec-backed project was routed to `analyze-project` before
its existing legacy artifact root and memory-linked artifacts were read.

**Hard artifact order:**

```text
[code] research / analyze-project(code) → autopilot-spec (spec/) → autopilot-code (plans/)
[docs] research / analyze-project(paper or doc) → autopilot-draft → autopilot-refine
```

- **No code without a spec:** if a code request has no `spec/`, run `autopilot-spec` first. A one-off throwaway is the only exception; repeated work graduates to a spec.
- **No spec without prior evidence:** if neither `research/` nor `analysis_project/` grounds the spec, run `autopilot-research` or `analyze-project` first. Enforce this more strongly in unfamiliar domains and for new intent.
- Hook write gates are retired; artifact creation order remains a routing convention.

**The owning capability also owns revisions.** The routing reminder and convention govern edits.

| Artifact | Sole update path | Version location |
|---|---|---|
| `spec/` blueprint | `autopilot-spec` update | `_internal/versions/v{N}/` |
| code work under `plans/` | `autopilot-code` | `plans/<date>_<slug>/` |
| documents | `autopilot-draft` or `autopilot-refine` | `_internal/versions/v{N}/` for major refinement; minor history in `pipeline_summary.md` |
| experiments | `autopilot-lab` | `_RUNLOG.md` |
| DB records with `type=profile` | `analyze-user` or `post-it --scope user` | changelog inside the record body |

This document plus the runtime adapter bootstrap is the routing source of truth. Violation signals include ad-hoc artifact edits, code before its gates, or updating an artifact through a capability that does not own it.

### 0.2. Semantic Primary Routing

Choose the primary capability from the purpose of the work the request performs, not
from the artifact the user names or the surface verb such as "update", "fix",
or "정리". A request that ends in "update the report" still has its primary
decided by the evaluation, implementation, or independent document goal it serves.

Precedence, highest first:

1. Experiment evaluation and its result report belong to `autopilot-lab`
   (`eval` for evaluation, analysis, and result reporting; `setup` for new
   training). This includes new inference, synthetic failure reproduction,
   metric/ablation computation, and figures or media that produce new empirical
   comparisons. It also includes interpretation, limitations, listening
   comparisons, and conclusions from already fixed results, without new
   measurements. Prose, Markdown, or HTML does not change this ownership.
   Reuse existing metrics, checkpoints, and media when sufficient; select only
   needed stages, including the existing `report` / `editorial/report` stage,
   under §0.2.1. Reporting alone requires neither a new training/inference run,
   the full eval loop, nor a separate draft cycle. Preserve the selected
   intensity, verification, and experiment lineage.
2. With no new empirical work, correcting only the wording, structure, or
   errors of an existing document makes `autopilot-refine` the primary
   (`autopilot-apply` for an approved patching guide). Re-rendering fixed data
   with different size, fonts, layout, captions, or table order is document
   correction or work within the existing artifact owner, not new empirical
   evaluation. Updating a paper with finalized metrics stays refine/apply;
   interpreting and reporting the experiment itself stays lab/eval.
3. A change to requirements, evaluation policy, or any blueprint surface adds
   `autopilot-spec` update as a secondary spec-sync step; it never replaces
   the execution primary.
4. `autopilot-draft` owns a new independent document goal: a paper,
   presentation, proposal, or report with its own audience and document brief.
   Writing inside an experiment report uses lab's report/editorial support
   without changing primary or creating a separate cycle for prose.
5. Durable result routing may offer the canonical artifact to the optional
   `artifact-sink` extension, always secondary and last. The portable harness
   owns only the closed `artifact.completed` receipt and a local registration
   check; it has no note, DB, credential, routing, or UI semantics.

   Extension absence is normal and silent. A registered handler that reports
   unavailable produces `skipped/extension-unavailable`; an activated handler
   failure is `failed/artifact-sink` and remains retryable without invalidating
   the primary result. Hooks are not activation authority. The extension owns
   product-specific setup guidance, identity, upsert behavior, and publication.

   A report-bundle offer uses receipt schema v2 containing only the common
   event/status/timestamp envelope plus `bundle_id`, `version`, and `entrypoint`
   (`report/index.html`). It omits v1 `source_path`, `source_capability`, and
   `project_root`; the three bundle fields are all-or-none. Neither an absolute
   bundle path nor file payload is passed to the sink. Offers without bundle
   metadata remain the exact receipt v1 contract for compatibility.

   | Primary capability | optional artifact-sink policy |
   |---|---|
   | `autopilot-code`, `autopilot-draft`, `autopilot-lab`, `autopilot-refine`, `autopilot-research` | Topology-sealed after the declared durable terminal; offered only while an extension handler is registered and available. |
   | `analyze-project` | Same optional extension offer after persistent analysis completes; this pre-capability has no entry-recipe topology row. |
   | `autopilot-apply`, `autopilot-design`, `autopilot-ship`, `autopilot-spec` | No automatic offer until the recipe declares a concrete durable source output. |
6. A secondary capability must never substitute for the primary execution
   capability, and the primary never absorbs a secondary's artifact ownership.

| Request shape | Primary | Secondary |
|---|---|---|
| "Reevaluate the model on a new test set and update the report" | `autopilot-lab --mode eval` | existing lab report stage; `autopilot-spec` on policy change; optional artifact sink |
| "DSC final evaluation: infer with two fixed checkpoints, reuse frozen aggregates, and report the comparison" | `autopilot-lab --mode eval` | existing media/report stages |
| "Report the experiment's conclusions and limitations from fixed metrics; no new inference" | `autopilot-lab --mode eval` | existing report/editorial support only as needed |
| "DSC analysis: synthesize cases to reproduce the field failure" | `autopilot-lab --mode eval` | analyze-project for existing code/graphs when useful |
| "TF paper: reduce figure height and reorder table columns using the same data" | `autopilot-refine` | apply an approved guide, or keep the existing artifact owner |
| "Put finalized experiment metrics into the existing paper" | `autopilot-refine` | `autopilot-apply` for an approved guide |
| "Write an independent paper or presentation from the evaluation" | `autopilot-draft` | consume existing lab results |
| "Implement a reusable evaluation driver or fix the HTML generator's path bug" | `autopilot-code` | lab only if a separate empirical evaluation is requested |
| "Fix only the typos and sentences in REPORT.md" | `autopilot-refine` | — |
| "Change the evaluation mixing policy to unscaled and reevaluate" | `autopilot-lab --mode eval` | `autopilot-spec` update; neither replaces the other |

An "analysis" label does not select a capability: reading existing code or
papers uses analyze-project/research as appropriate; new synthesis, failure
reproduction, or model comparison uses lab/eval; implementing a reusable
evaluation program uses code. Small experiment-support scripts remain within
lab's existing implementation stages and do not automatically open a code cycle.
Likewise, code review within an implementation/debug cycle belongs to
`autopilot-code --mode audit`; independent inspection of completed work belongs
to `audit`. Both may inspect code; purpose, not file type, decides the owner.

Collecting existing artifacts into a delivery archive uses
`autopilot-ship --mode package`: reuse the delivery template, named model
versions, requested input/output list, and existing reports; check the included
files, relative links, versions, and archive. This is a delivery goal, without
an automatic deployment, installation, security/release-review loop, new
evaluation, or report rewrite. Keep the sample list within the request: nearby
datasets, historical models, and a larger earlier delivery are not implicit
inputs. A later approval of a larger batch applies only to that batch.
Missing sample inference is lab/eval work for those samples, with packaging as
the delivery step; implementing a reusable packager is code work. Ship without
a mode retains its existing deployment/release behavior (`default`).

| Delivery request | Primary | Boundary |
|---|---|---|
| "Reuse the delivery template, named model and requested samples/outputs/reports; make a ZIP" | `autopilot-ship --mode package` | Existing results first; only requested files and minimal link/version/archive checks |
| "Infer on the received field samples and one or two simulations, then package them" | `autopilot-lab --mode eval` | Only missing requested inference; ship/package for delivery, no full-dataset expansion |
| "Fix the reusable archive builder" | `autopilot-code` | Program implementation, not packaging an existing result |
| "Write an independent presentation for the recipient" | `autopilot-draft` | New document goal, not file collection |
| "Deploy the application to production" | `autopilot-ship` | Existing default deployment authorization and verification remain |

Added after a 2026-07-14 incident where a checkpoint reevaluation with report
regeneration was routed to `autopilot-refine` as primary from its surface
artifact and the entire evaluation ran inline in the main session.

### 0.2.1. Shape Before Preset (SD-135)

The precedence above decides which capability **owns the artifacts** of a
request. It does not oblige the session to run that entry's whole recipe.
An entry's recipe is an assembly preset: the default when nothing else is
chosen and an example of how its parts fit together. The route is the sealed
stage list, assembled from the part catalog (`capabilities/topologies.json`
recipes plus `part_catalog`, SD-165).
Before proposing a route, choose the **shape** of the work from its size; the
shape and explicit choices determine the route; defaults only fill omissions:

| Shape | When | Route |
|---|---|---|
| `direct` | one atomic, reversible change the session makes and checks inline | `capability-route.py compose --campaign-key <stream> --slug <slug>` — the inline node, dispatch depth 0 |
| `solo` | one bounded piece of work that deserves its own registered session but no separate stages | `compose --shape solo` — one registered dispatch-depth-1 owner, no dispatch depth 2 |
| `staged` | work with separate stages | `compose --shape staged` uses the capability's standard recipe; optional `--graph <stage,…>` selects a subgraph — list a capability's parts (stage ids, summaries, inputs/outputs, units, human gates, `shareable`, `start_approval`, optional and borrowable parts) with `capability-route.py stages [--capability <cap>]` before guessing at `--graph` |

For execution, use `compose --campaign-key <stream> --start --prompt-file
<task>` with the selected shape/graph (the campaign choice is required:
an existing or new stream key, `--parent-cycle`, or an explicit
`--unassigned`; the refusal lists the root's active keys); `--profile light` or `--owner <harness>` is an explicit choice.
The task file contains the requested work, not instructions for running the
parent. The runtime seals it, prepares its cycle, starts the frame pair when
declared, and returns one receipt. Reuse that receipt's `resume_command` after
wakes or input corrections. It reuses exact attempts, carries partial admission
obligations, and never retries a failed attempt merely because it was called
again. Follow its `parent_next` while work is running. An expired bounded wait
returns `needs-attention`: report the pending work; runtime watchers retain
execution/cleanup responsibility and the deadline grants no retry authority.
`needs-interview` means both frames have been checked: compare their results,
fill the returned semantic template, and use `start --route <file> --interview
<question.json>`. It registers the gate before returning `needs-question`.
After the native answer, use the same command with `--answers <answers.json>`;
it records intent, releases the gate, and starts the owner. Already answered
questions can supply both files without asking again. `--decision revise|stop`
records those choices; repeated answers reuse the recorded decision.
Runtime settlement closes the route and cycle before owner success is delivered.
Direct start also prepares the inline cycle and returns its `artifact_env`;
use that output path without a separate producer `begin`. Replaying start on a
proven closed route returns its outcome without launching or reopening work.
The model does not assemble launch tuples or artifact variables, harvest, or
finalize ordinary work. Recovery commands name the exact
attempt; a changed scope still belongs to the user.

Without `--start`, `compose` returns the selected stages, profiles, human gates
and canonical `route_file`; full evidence stays in that file. `--help-all`
documents the advanced machine-compatible inputs, including `--full-record`.
`compose` preserves the chosen stages and derives their dependencies. Inherited
parallel presets that do not fit the selected graph are omitted; missing preset
stages are not mandatory. The sealed result shows the realized stages and omitted
defaults before execution. Input/output, explicit human gates, and terminal proof
remain binding contracts for the selected work.
`compile` remains the low-level explicit interface. Callers need not switch to it
to obtain a complete recipe. `compose` fills omitted flags from the checkout: cwd, artifact root
(`utilities/artifact-root.sh`), tracking and workflow mode by shape, a
default drift verdict, the spec-read gate (it refuses with
`compose-spec-read-required` when a `spec/prd.md` exists under the cwd or the
artifact root and the caller has not named what was read; the latest `prd.md` of
a `shared/spec/<ref>/` revision only adds one `[경로]` card line with its path), and both
eligibility probes (`dispatch-readiness`). The result is sealed, verified,
bound, and guarded exactly like a recipe route: `selection.route_origin`
records `compose` or `preset`, `selection.shape` records the shape, a staged
route is `composed: true` with its recipe embedded, and every staged node
keeps its unit, gate, write scope, model profile, and permissions from the
owning capability's recipe. A stage id may name a unit from the node's
declared `unit_choices` (`execute:dev/refactor`). A `capability:stage` token
(`autopilot-research:retrieval`) borrows a shareable part of another recipe:
it keeps its unit and gate, is written under `parts/<capability>/<stage>/`
inside the host's artifact scope, and seals the name mapping as `part_io`; the
host capability still owns the artifacts, and a token that is not a registered
shareable part is the same unknown-node refusal as any other. A human gate a kept node
raises is rebound to the entry of the node that now follows it and dropped
when nothing follows; a declared parallel group survives only when its anchor
is kept and is not the new terminal. Composition changes route *shape* only —
the write, spec, and core gates, the dispatch-depth-3 ban, and every
completion gate are unchanged, and a preset-free route is still a route the
§0.4 gate and the route-participation invariant apply to.

`compose` runs the same probe an owner launch re-checks, so it is called from
the dispatch-depth-0 session in the final worktree; inside a registered owner
(`AGENT_DISPATCH_DEPTH` ≠ 0) it needs `--dispatch-evidence` or
`--registered-headless-evidence` from the parent, exactly like `compile`.
The `[경로]` line it prints on stderr is the notice §0.4 uses for small work.

### 0.3. Pre-Execution Gate for Long-Running Work

Before starting a long-running command, GPU or checkpoint evaluation, bulk
figure/media generation, or a full report regeneration, the main session
answers this gate; it does not enter long-running execution inline without it:

1. What is the semantic primary capability under §0.2?
2. Does the work create new empirical output?
3. Is the intensity `standard+`?
4. Are two or more separable stages present under `OPERATIONS §5.10`
   separability?
5. If main intends to run anything inline, which recorded exception applies?
6. Have native sub-agent limits and headless worker limits been checked as
   separate surfaces (`OPERATIONS §5.10` delegation surfaces)?
7. Does the plan preserve existing experiment lineage and the append-only
   `_RUNLOG`?

A gate answer that selects dispatch follows `OPERATIONS §5.10` registry and
liveness rules; an inline answer for `standard+` separable work requires the
recorded reason.

### 0.4. Primary Entry Confirmation

For material work, the main agent proposes the route and the user confirms the
intent before capability execution begins. The agent fills every field from
the request and recovered context; the user reviews a completed proposal rather
than recalling capability names, invocation syntax, or pipeline options.

Render labels in the user's communication language while preserving these five
fields and this order. In Korean, the canonical card is:

```text
[실행 확인]

작업: <무엇을 어떤 결과로 만들지>
이유: <현재 배경과 작업이 필요한 이유>
경로: <primary entry capability> · <mode/intensity> — <선택 이유>
범위: <포함 범위와 중요한 제외 대상>
완료: <산출물과 검증 기준>

→ 진행 / 수정: <틀린 부분> / 중단
```

When the runtime exposes a native structured-question surface (Claude
`AskUserQuestion`, Codex `request_user_input`), deliver this confirmation
through it: the five fields form the question body and the options are exactly
진행 (recommended) / 수정 / 중단. The plain-text card above is the fallback when
no such surface exists or it fails. The structured form changes only the
delivery surface — never the five fields, their order, the one-time approval
semantics, or the exemptions below.

Keep each value to one line. Do not include alternative menus, internal
sub-Skills, or extended reasoning. The card applies when any of these observable
conditions holds:

1. source, document, configuration, or a durable artifact will be created or
   changed;
2. two or more of research, analysis, implementation, or verification are
   needed;
3. the work requires a test, build, deploy, external-system mutation, or
   separate correctness evidence; or
4. a spec-backed project will create or update a capability-owned artifact.

Read-only orientation, status reporting, explanations, and simple factual
answers are exempt. Card exemption is not evidence exemption: an explanatory
or factual answer still follows `roles/response-policy.md` "Local evidence
before recall" when the repository holds covering research or document
artifacts. `direct` is an explicit route shown in the card with its
reason, not a silent no-route decision.

**Small-work notice (SD-136).** When the compiled route is `direct` or `solo`
(`quick`) and its sealed `small_work_confirmation` is `notice` — the shipped
default, `profiles/dispatch-defaults.yaml` `confirmation.small_work` — the
blocking card above is replaced by one non-blocking line: the `[경로]` line
`compose` prints (capability · shape · route id · human gates) plus one clause
of scope, and the work proceeds in the same turn. The card stays blocking,
whatever the sealed value, when the work is destructive (data, history, or
worktree loss), mutates an external system, deploys, or the user asked to be
asked. `card` restores the blocking card for every small route. The
route-participation invariant below is unchanged: notice or card, source work
still needs the bound route, and the 0–1 / 1–3 inline questions of the frame
interview (below) are asked only when they exist. A current or immediately preceding user
instruction that already approves the same route and scope satisfies the gate;
do not repeat the card. After approval, capability-owned stages, validation,
records, commits, dispatch, and handoffs proceed without further confirmation.
Reconfirm only a material change to the primary capability, scope, completion
criterion, destructive risk, or touched external system.

Material source work has an additional deterministic participation invariant:
before a source Edit/Write-family mutation or a commit containing source
changes, the acting session must hold a current, cwd-bound route record emitted
by `utilities/capability-route.py compile` or `compose`. For code, that route is
`autopilot-code` at no less than `direct`. A Skill invocation, the prose card,
an earlier session's record, or a stale record from another cwd is not route
participation. Hotfixes do not bypass this floor. This invariant was hardened
after the 2026-07-24 Cairn incident in which a route card was shown but no
route was entered and the feature was edited, committed, and deployed through
silent no-route work. The runtime write gates are retired, so no command
enforces or answers this invariant; it is the session's own obligation, and
`capability-route.py verify`/`status` only inspect a route that exists.

Before approval, choose from compact manifest routing metadata and §0.2; do not
load the full entry Skill body or its references merely to propose a route. At
`standard+`, the dispatch-depth-1 owner reads the selected capability contract and each
dispatch-depth-2 worker reads only its stage contract. `direct` or `quick` acting
sessions read the detail they need after approval. If a runtime automatically
injects a selected Skill body into main, do not duplicate that read; record the
runtime limitation rather than claiming total-token savings.

**Two-stage confirmation (SD-123).** For the five frame recipes at `quick+`,
depth-0 submits `compose --start`; the runtime first compiles/binds the route,
issues its producer cycle, and launches the two frame nodes. These steps authorize frame artifacts, not owner
execution. After both briefs arrive, depth-0 presents this blocking direction
card and the interview. User approval precedes the owner and the §0.4 notice:

```text
[방향 확인]

방향: <채택한 방향 한 줄>
대안: <기각한 대안과 이유>
위험: <frame이 찾은 최대 위험·가정>
범위 변경: <처음 요청 대비 증감, 없으면 "없음">
비용: <frame 실소비·하네스 구성과 남은 단계·강도>

→ 진행(권장) / 수정: <틀린 부분> / 중단
```

Deliver this card through a native structured-question surface when one is
available, the plain-text form otherwise — the same fallback rule as the §0.4
card. Only once the direction is confirmed does the owner start, and the §0.4
gate then arrives as a non-blocking `[실행 통지]` — the same five fields, in
order. It announces the route the confirmed direction produced; it does not
re-ask a direction the user has already settled.
`confirmation.mode` (`profiles/dispatch-defaults.yaml`, default `hybrid`)
governs that ordered pair: `hybrid` is the shape above (blocking direction
gate first, notice after), `both` makes the later notice blocking as well, and
`post-frame-only` drops the notice entirely. A route compiled before this
cycle keeps `frame.continuation=inline-next` and is never retro-fitted onto
this gate.

**The frame interview (SD-129).** The `[방향 확인]` card is not the whole
gate. The gate record names its exact cycle-local interview: a one-sentence
restatement of what the user wants, a plain-language brief, and the few
decisions the frame legs could not settle without the user. The depth-0
session builds that record from the joined frame legs and does the interview
itself, in this order, and never leaves it to a helper:

1. Ask first whether the restatement is right — that sentence, verbatim,
   with 예 / 아니오(고쳐 말하기) — and record a correction in the user's words.
2. Put the `[방향 확인]` five-field summary as the card above.
3. Ask each interview question through the native question tool, one topic
   per question, the recommended option first and labelled
   (권장), each option with its one-line meaning; never paraphrase a question
   into harness vocabulary, and never add questions the interview does not
   carry. A tired reader must be able to answer without opening the plan.
4. Put the actual responses in the returned `answers_template` and submit
   `start --route <file> --answers <file>`. The runtime renders intent and
   records the release that authorizes owner launch. A repeated submission
   reuses it. Legacy recovery: `workflow-supervisor.py
   release --route <route file> --gate frame-review --decision proceed --answers <file>`.

`OPERATIONS §5.10b` owns selector mechanics and the one-time `top`→`deep`
demotion; the public work entry owns launch calls and artifact context. Both legs
are always waited for — past the hard limit depth-0 stops and asks, rather
than proceeding on one — and differing direction verdicts go side by side,
nothing downstream starting until the user picks one. Step 1 is asked even
when the interview carries zero questions; `understanding_confirmed` records
that answer.

Question counts are bounded by intensity, and `QUESTION_CAP` in
`utilities/frame_interview.py` owns those numbers — do not restate them here.
The validator refuses an interview that breaks the plain-language rules
before it reaches anyone; the cap and the wording rules are machine-checked at
the raise for every route carrying the gate, `quick` included, while steps 1–3
above stay obligations on the acting session that nothing checks mechanically.
Only code/design/draft/refine/spec run the frame pair at `quick` and above,
before the owner. Other recipes retain their topology. `direct` asks its
question inline in the §0.4 card and records the answer in the plan or work log.
The recorded answers become `shards/frame/intent.md`, rendered by depth-0.
The runtime supplies the released task and decisions to the owner and every
subsequent worker, including graphs without `plan`. The assigned stage narrows
that task; a role preset does not replace it. Workers cite applicable question
ids and report decisions they cannot honor. An interview gate may be raised at most twice
per route (`round` ≤ 2).

Entry routers therefore have two deterministic load phases: manifest-owned
metadata before approval, then the selected portable owner contract after
approval. A router may expose one direct owner-reference index, but no
pre-approval reference may contain execution procedure. The confirmation is
one-time for an unchanged approved route and scope.

### 0.5. Post-Execution Completion Report

After a material-work attempt, the user-facing main agent closes the flow with
a fully populated five-field report card. Render labels in the user's
communication language while preserving these five fields and this order. In
Korean, the canonical card is:

```text
[완료 보고]

작업: <수행한 작업>
결과: <완료 | 부분 완료 | 실패 | 차단 — 실제 결과>
검증: <실행한 검증과 결과>
산출물: <사용자가 확인할 경로 또는 없음>
남은 사항: <미완료 항목, 위험, 차단 요인 또는 없음>
```

Keep each value to one line. `결과` names the honest outcome status —
completed, partial, failed, or blocked, localized for the audience — and must
not present partial, failed, or blocked work as completed. Use `없음` (or its
audience-language equivalent) when there is no artifact or remaining item.

If any review gate of the work closed without an independent reviewer's PASS —
the route outcome's `review_independence_degraded`, or a
`completed-review-degraded` line from `complete` — `검증` must say so and name
the node. That list covers both `degraded` (the owner reviewed its own work) and
`owner-overridden` (the owner ruled over a review that returned FAIL). Either is
a real result and neither blocks the route, but reporting it as review would
make "reviewed" mean nothing (`OPERATIONS §5.10`, SD-OPEN-41(b)). Within the
`degraded` case, a marker whose `round_census.closure_class` reads
`owner-override-unlinked` names the sharper shape — an inline completion that
landed directly over an unresolved blocking FAIL without the evidence-linked
owner-closure procedure — and `검증` should say so by that name (SD-153 rule 5,
SD-134 A75-9).

For dispatched work, the parent follows the runtime's next-action receipt and
checks the completed artifact before reporting. Normal success needs no harvest.
A process exit or intermediate stage verdict alone is not task completion.
Read-only orientation and status replies use concise prose instead.

New registered owners carry `workflow_completion=runtime-v1`. Their completion
controller owns the exact workflow/route/cycle closing transaction after PASS
and child cleanup. It records COMPLETE only after sealing, retries interrupted
closure without a model turn and sends a recovery notice while closure remains
pending. The owner and parent have no separate close/finalize procedure.

The runtime closes a route once nobody works on it; no session has to
remember to. `compose` and `campaign-status` close an open route whose Claude
session this host saw end and that then stayed quiet for an hour, or that saw
no writes for a week; `campaign-close` also closes its campaign's member routes
that belong to the closing session or stayed quiet for an hour. Routes of the
calling session, routes a dispatch attempt, a pending owner settlement or an
unended resource run still holds, routes waiting on a human gate and routes
whose cycle a live process holds open stay open, as does everything when that
evidence cannot be read. The closure claims no proof: it records what the
completion markers show, seals the cycle completed only when that proof holds
and abandoned otherwise, and names its reason in `autoclose`. A session that
returns to such a route is not refused: `finish` reports it closed, and
`start` returns the compose command that begins the work again.

A session finishes its own `direct` inline route to record real evidence.
Supply a readable, nonempty artifact from the route's exact open cycle and the
final summary; the command records terminal proof, closes the route, seals
that cycle and returns a receipt only after rechecking all three. A partial
transaction remains `finish-pending` and is resumed with the same intent.

```text
python3 utilities/capability-route.py finish --route <route.json> --evidence <cycle-local-file> --summary-file <file> [--commit <full-sha>]
```

Ineligible inline routes and legacy recovery retain their explicit guarded
completion and close steps; use their recorded route and producer evidence:

```text
python3 utilities/capability-route.py close --route <route.json> [--commit <sha>] [--summary <line>]
python3 utilities/capability-route.py status --artifact-root <dir> --open-only
```

For producer-backed work, the controller's transaction is **terminal proof →
route close → exact cycle seal → workflow COMPLETE**. Eligible direct inline
work uses `finish`; legacy recovery uses complete, close and finalize in that
order. Shared admission applies only to
shared kinds after sealing; it is never a prerequisite for the terminal marker.
A cycle sealed while its route is open (`finalize --allow-open-route`) stays
`active` in its manifest; a later close, proven or not, leaves it there, and
campaign closure lists it as `sealed-unproven` with the route's recorded proof
state.

During the same session, a verified OPEN route for the same named campaign may
also govern writes in a related integration worktree. The guard requires the
same canonical repository and artifact root plus proof that the route branch
was actually merged: an exact merge command, matching in-progress
`MERGE_HEAD`, or a descendant of the exact merge or recorded fast-forward.
It keeps the route's source, scope, core, spec and recall checks. A sibling
branch with only a shared ancestor, a different campaign, or a closed route
requires a new route; integration reuse needs no manual bind command.

`close` writes an outcome sidecar beside the immutable route record — the record
itself cannot carry the closure, because `route_hash` covers every other field.
`status --open-only` then answers "what was started and never finished" directly.
An unclosed route is indistinguishable from abandoned work, and the same applies
to the other things a finished attempt leaves behind: a merged worktree and
branch, a spec `pipeline_state.yaml` still naming a live phase, and a memory
handoff still `pending` after its obligation is met. Close what the attempt
opened, or the next session pays for it in reconstruction.

### 0.6. Tracked-Workflow Lifecycle and Continuation

A process exiting is not a workflow completing. **Workflow completion is the
state in which every terminal node of the approved stage DAG holds its
completion gate and no human gate remains open.** An intermediate stage that
succeeded — a training run that reached its last epoch, a green CI check, a
worker that returned `PASS` — is stage evidence, never workflow completion. No
acting agent, dispatch-depth-1 owner, supervisor, or runtime lifecycle hook may
declare completion from an intermediate success.

Committed success and remaining cleanup are separate facts. A marker does not
make a live descendant quiescent: the execution boundary retains cleanup,
and the common supervisor retains the wait or recovery notice until it ends.
Exact inspection reads the worker's result; requesting more failure detail is
optional and cannot become a second completion gate.
The runtime acknowledges a delivered notification after its receiving turn
finishes. Delivery does not restrict the owner's tools or prove stage success;
launch dependencies, write authorization and terminal cleanup scopes own those
decisions. Repeating a notification cannot declare the owner abandoned.

A steward's idle-notify subscription is observation, not a continuation; it never
satisfies the registered-continuation obligation below (`core/OPERATIONS.md §5.14`).

This section is capability-independent. `autopilot-lab`, `autopilot-code`,
`autopilot-ship`, spec/research, CI and GitHub check cycles, external-state
monitors, detached resource processes, registered workers, and loop-driven work
all use the one state machine below. A capability's stage graph *extends* this
contract by declaring nodes, gates, and continuations; it never redefines what
continuation or completion mean.

**Common states.** `capabilities/topologies.json` is the machine-readable
vocabulary and `utilities/workflow_state.py` is its executable form:

```text
CREATED → READY → RUNNING → STAGE_SUCCEEDED → NEXT_REGISTERED
        → NEXT_RUNNING → TERMINAL_VERIFY → COMPLETE
```

Failure states are `BLOCKED_HUMAN_GATE`, `FAILED_RETRYABLE`,
`FAILED_TERMINAL`, and `CANCELLED`. `COMPLETE` is reachable only from
`TERMINAL_VERIFY`, and `TERMINAL_VERIFY` only once every declared terminal node
carries its completion marker — so a workflow cannot become `COMPLETE` before
its terminal node. `BLOCKED_HUMAN_GATE` never advances automatically; only an
explicit human release returns it to `RUNNING`. `FAILED_*` never advances a
downstream stage.

The completion writer validates and records the remaining legal path through
`STAGE_SUCCEEDED` and `TERMINAL_VERIFY` to `COMPLETE`. Repeated or interrupted
closure resumes from the journal. Markers cannot erase unresolved human gates,
failures, or cancellation.

**Every non-terminal stage declares exactly one continuation.** A stage graph
that leaves a stage with no way to reach the next one is the defect this
contract exists to prevent, so the declaration is mechanical, not editorial:

| Continuation | Meaning |
|---|---|
| `inline-next` | the same checked payload runs the next stage before it returns |
| `supervised` | a registered continuation supervisor observes child termination and starts the next stage exactly once |
| `human-gate` | an explicit human gate named in the recipe's `human_gates` blocks the successor — e.g. each recipe's depth-1 bootstrap frame pair continues into gate `frame-review`, which fences that recipe's first work node and releases with `workflow-supervisor.py release --gate frame-review --decision proceed\|revise\|stop` (§0.4, SD-123) |
| `monitor` | a checked monitor waits on an external state change and reports a typed condition match |

A detached resource process can never continue itself, so a `resource-runner`
node must declare `supervised` and may never be a terminal node. A node with no
dependents must declare `terminal: true` with its `terminal_gate`; a node with
dependents must declare a continuation and must not declare `terminal`. Every
declared human gate binds to the exact node it gates (`entry` before that node,
`terminal` after the terminal node). **A recipe or composed graph that violates
any of these is rejected at route compile, and a launch bound to such a graph is
rejected before the process starts** — the refusal is the point, because the
alternative is silent abandonment.

Human decisions have no elapsed-time default. A question window closing or an
empty response does not release, reject, or cancel the durable gate. Its owner
keeps the exact question and gate available for a later real answer; independent
authorized work can continue. Managed question surfaces disable automatic empty
answers where the runtime supports this. Otherwise the owner leaves a plain-text
question for a later reply instead of repeatedly opening expiring prompts.
An owner that raises a gate calls the bounded `await-release` once; when the gate
is still blocked it ends its turn with `verdict: BLOCKED`, naming the gate as its
blocker. That parked stop is a pause, not a failure, and no model turn runs while
the person decides. `resume_command` then reports `waiting-human-gate` with the
exact release command, and a person's `release --decision proceed` starts one
continuation owner when no owner is live. That continuation uses the route's one
automatic replacement, so a replacement or continuation owner, which its prompt's
recovery context identifies, keeps calling the bounded `await-release` at a later
gate instead of parking. A revise or stop recorded while the owner is parked keeps
its meaning and starts nothing automatically.

A continuation route projects this contract onto its suffix. It drops a gate
whose gated entry node was cut, never rebinds a retained raising continuation to
an arbitrary successor, and never treats a reused predecessor's completion as a
human decision. If the suffix keeps the gated entry but cuts its raiser, the
builder may remove the runtime gate only after sealing and revalidating the exact
source route, gate, raise epoch, proceed decision, journal authority, and
raise/release entry digests; otherwise continuation compilation fails closed.

Grounded by the 2026-08-04 BC_ResNet_tf incident: training and its hard-negative
loop finished, the wrapper contained no evaluation stage, the documentation
named "separate eval" with no owner or trigger, the resource runner had no
completion callback, the resource row was invisible to Fleet, and the acting
agent ended its turn with no follow-up mechanism registered. Each of those five
is now a mechanically checked condition rather than a convention.

**Acting-agent obligation.** Do not end a turn while a tracked workflow has a
non-terminal stage with no registered continuation. Before the turn ends,
either the continuation is registered (supervisor armed, next stage dispatched,
human gate recorded, or monitor armed) or the same turn states plainly that
automatic follow-up is impossible and names the checked fallback the user can
run. A promise to act "when it finishes" is not a continuation. Status claims
cross-check PID identity, sentinel/exit evidence, log modification time, and
declared artifacts; a registry status word alone is not state. Reaching a
terminal condition never widens authority: stop only for a human gate or a
genuinely new external permission.

`OPERATIONS §5.12` owns the supervisor, resource-lifecycle, and Fleet
projection mechanics; `CONVENTIONS §3` carries the cross-document invariants.

## 1. Four Tracks

```text
[research and experiment] research / analyze-project(code) → autopilot-spec ↻ → autopilot-code ↻ → autopilot-lab ↻
[library and CLI]         analyze-project → autopilot-spec ↻ → autopilot-code ↻
[documents]               research / analyze-project(paper or doc) → autopilot-draft → autopilot-refine ↻ → autopilot-apply
[apps]                    autopilot-spec ↻ → autopilot-design → autopilot-code ↻ → autopilot-ship ↻
```

`↻` marks an iteration point. Common post-work capabilities are read-only `audit` and Markdown correction through `autopilot-refine`. Cross-project capabilities are `analyze-user` and `post-it --scope user`.

## 1.1. Pipeline Intensity Routing

Autopilot entrypoints choose `intensity`; verification rigor is derived from it under `CONVENTIONS §1.1`, not from a separate `--qa` axis. Intensity selects the stage graph and dispatch depth, while derived rigor scales plan checks, selected independent review, and final verification.

| Request shape | Default | Routing |
|---|---|---|
| One-off answer, typo, rename, or explicit no-artifact work | `direct` | No plan stage, plan check, or durable plan |
| Small localized change that misses at least one atomic-direct predicate and has no promotion signal | `quick` | A depth-0-run bootstrap layer runs first — two frame legs with distinct personas and usage-aware harness selection, joined and interviewed directly by the depth-0 session, the anchor a tier above the owner's own model profile. Then a registered-headless dispatch-depth-1 one-shot conductor with orient-lite, micro-plan, plan-check-lite, focused verification, and concise report; no dispatch depth 2 |
| Work with a promotion signal or separable durable stages | `standard` | Same depth-0-run bootstrap frame layer as `quick`, then durable plan/checklist; a dispatch-depth-1 conductor (default `deep`) dispatches selected stages with file-only handoff and realizes only registry-declared parallel groups (plan/implementation-review, not frame) |
| Important multi-file or risk-bearing work | `strong` | A `deep` owner plus the declared plan/review groups; selected high-value anchors may widen to a third profile/perspective leg while other groups remain width two |
| Complex cross-domain or cross-harness work | `thorough` | Bounded dispatch-depth-2 perspective and verifier workers |
| High-stakes, irreversible, security, or external-facing work | `adversarial` | Thorough plus an explicit adversary, failure-mode, or security pass |

Only `direct` has no plan. Every other autopilot graph includes a plan check, but independent QA is not repeated after every sub-stage by default. Independent passes use route-declared bounded groups with separate execution contexts and distinct personas, retaining model-profile/perspective asymmetry where declared; every review — down to a `direct`/`quick` self-check — carries the refute-by-default adversarial stance. `CONVENTIONS §1` is canonical for the graph.

## 2. Work-Nature Map

| Work | Prior research or analysis | New intent or blueprint | New or existing asset work |
|---|---|---|---|
| Documents: papers, presentations, reports, proposals, rebuttals | academic or market research plus `analyze-project` in paper/doc mode | `autopilot-draft` | `autopilot-refine` |
| Code: libraries, research, apps, CLI, and API | academic or technical research plus `analyze-project(code)` | `autopilot-spec` in app/library/api/cli/research/composite/auto mode | `autopilot-code`, routed by spec mode |
| ML or one-shot experiment prototype | Four code-analysis inputs: experiment conventions, readiness, cleanup, and similar models | No spec for a fast cycle | Iterative `autopilot-lab`, graduating to `autopilot-code` |
| Visual assets and design | — | `autopilot-design` for a new design-first cycle | Substantial direction, token, layout, structure, or built-app design evolution goes through `autopilot-design`, updating the token contract and code from a real render. Only a trivial tweak goes directly through `autopilot-code`. Design tokens are the single contract under `DESIGN_PRINCIPLES §9`. |
| User profile | — | `analyze-user init` | `analyze-user update` |

One-line prose/config edits, pure renames, cleanup, and one-off reviews that need
no plan or log may bypass autopilot and use direct editing or the implementation
role. A source-code Edit/Write or a commit containing source changes may not:
even an atomic hotfix enters an explicit `autopilot-code` route at `direct` and
produces the current-session route record required by §0.4. Use heavier
autopilot tiers only when work needs their tracking or accumulated artifacts.
`DESIGN_PRINCIPLES §4` and each capability's quick tier define minor versus
major. When one request spans several rows of this map, resolve the primary with
the §0.2 semantic precedence.

## 3. `autopilot-spec` Modes

| Mode | Use | Scaffold: PRD plus skeleton |
|---|---|---|
| `app` | User application such as Next.js or Expo | Component and Deployment diagrams plus application skeleton |
| `library` | Public npm, pip, or crate package | Packaging config and public API skeleton following reference exports |
| `api` | Backend API without UI | Component and Deployment diagrams plus FastAPI or Express router skeleton |
| `cli` | Command-line tool | argparse or typer entry plus command skeleton |
| `research` | Experiment roadmap (step ladder plus decision protocols) and reproducibility | train/eval/config and model skeleton plus Phase 1.5 checkpoint preflight |
| composite or `auto` | Multiple aspects or inferred mode | Common contract plus independent sections per selected mode, with confirmation after inference |

Reference priority is internal `similar_models` or `--ref`, then `research/<topic>/code_resources`, then generic scaffolds. Prepend conventions from `analysis_project/code/experiment_conventions.md`; fall back to `mem profile 07_coding_convention`, with project-local conventions winning conflicts.

## 4. Atomic PRD Updates

When a code or intent change affects the spec, update every affected surface in one transaction. `CONVENTIONS §6.3a` is the mapping source of truth.

| Change | Affected surfaces |
|---|---|
| Endpoint, request/response body, or error | API contract, Component, and optionally Sequence |
| DB entity or field | Data model, backend Component, and optionally ER |
| UI flow | UI flow, frontend Component, and optionally Activity |
| External service integration | API auth contract, Deployment, deploy record, and `.env.example` |
| Stack replacement | Stack decision, Component, and Deployment |
| Public API change in a library | Public API, examples, semver impact, and module-dependency Component |
| CLI command or option change | Commands, options, exit codes, README examples, and command-tree Component |

`autopilot-spec refine` identifies the impact list, confirms it, and updates it atomically. If `autopilot-code` detects spec impact, it plans the bundle, confirms, and jumps back to `autopilot-spec`. After the final code report, Step 7 updates `analysis_project`: edit small changes directly or run incremental `/analyze-project --mode code --skip-qa` for large ones.

## 5. Entrypoint-to-Worker Routing

The main agent proposes one primary entrypoint under §0.2, the user confirms it
under §0.4, and internal routing is automatic. Portable model roles come from
`CONVENTIONS §2`.

| Entry | Internal routing |
|---|---|
| `autopilot-research` | Research-survey and fact-check roles plus browser-fetch, PDF-extract, and web-image-search material roles |
| `analyze-project` | One capability analyzing code, paper, or document mode itself |
| `autopilot-spec` | Planning role for PRD, material role for research import, and setup logic for hosting and CI/CD |
| `autopilot-design` | Design maker and critic plus material web-image-search |
| `autopilot-code` | Direct is dispatch-depth-0 inline. From `quick`, depth-0 first runs the two-leg frame bootstrap (see above). Quick defaults to `balanced-deep` and standard+ to `deep`; explicit profile choices take precedence. Each uses one registered dispatch-depth-1 owner. At `strong+`, plan/implementation-review open asymmetric declared groups; `thorough+` adds implementation-risk and failure-mode legs. Planning, implementation, test, report, and task-aware review remain separate file-handoff stages. |
| `autopilot-code` in app mode | General code flow plus design critique at plan review and after render, DB migration safety, and automatic deploy after an authorized push |
| `autopilot-draft` | Material figure/data/reference work, writing implementation, editorial polish, and research fact-check |
| `autopilot-refine` | Reuse the draft roles plus editorial review |
| `autopilot-lab` | Setup uses research plan review, implementation scaffold, and QA smoke tests. Evaluation uses functional QA, figure generation, and research survey; at `standard+`, checkpoint evaluation, media generation, report assembly, and independent verification dispatch as stage workers under the eval execution topology in `capabilities/autopilot-lab.md`. The actual long-running training run is asynchronous and human-gated through RUNLOG ⏳ rather than a stage-worker dispatch. |
| `analyze-user` | Cross-project material collection plus editorial review |

For every durable stage at `standard+`, use an independent headless session under `OPERATIONS §5.10`; the named team roles run inside that session, and the dispatch-depth-1 conductor passes only artifact paths. Direct stays dispatch depth 0, and a depth-0-run bootstrap layer — two frame legs with distinct personas and usage-aware harness selection, joined and interviewed directly by the depth-0 session, the anchor a tier above the owner's own model profile — runs ahead of quick, which stays one registered-headless dispatch-depth-1 one-shot conductor.

Each entrypoint is an explicit unit of intent. The §0.4 confirmation is the
single top-level route handshake. Capability-local review controls such as
revise into v2, back-jump, `--confirm`, or `--user-refine` remain opt-in and do
not repeat that handshake. Ask a separate question only when intent is genuinely
ambiguous after presenting the completed proposal. The runtime adapter bootstrap
owns concrete invocation syntax.

## 6. Artifact Folders

Code uses sibling `spec/` and `plans/` buckets.

| Kind | Folder |
|---|---|
| Code blueprint | `spec/`: current `prd.md`, `stack.md`, optional `design/`, `ship.md`, `pipeline_state.yaml`, and prior specs under `_internal/versions/v{N}/` |
| Code work | `plans/<date>_<slug>/`: plans, dev logs, test logs, and `_internal`, regardless of whether a spec exists |
| Experiment prototype | `experiments/<date>_<slug>/` plus `experiments/_RUNLOG.md` |
| Document | `documents/<date>_<name>/` |
| Prior research and analysis | `research/<topic>/` and `analysis_project/<mode>/` |

Numeric prefixes such as `00_`, `01_`, `02_`, and `05_` are retired. Use plain names inside `spec/`, separating user-facing files from machine-oriented `_internal/`. The spec transaction helper snapshots the exact prior `prd.md` automatically whenever an existing PRD changes, regardless of intensity; initial creation and no-op updates do not allocate a version. See `CONVENTIONS §§5 and 6.5`.

**Producer lifecycle (W7C).** Every folder above is a bucket inside one producer cycle once the write-cutover is active: `begin` issues the campaign/cycle/producer IDs before the first write, artifacts land under `campaigns/<campaign-locator>/<cycle-locator>/artifacts/<bucket>/…`, stage workers join the owner's open cycle through the `AGENT_ARTIFACT_*` environment, and `finalize` commits `manifest.json` after route closure. Before that, `artifact_producer.py checkpoint` publishes a live open cycle's interim manifest — the same schema with `cycle.state: open` — at `.runtime/artifact-producer/v1/open-manifests/<cyc>.json`; stage completion, supervisor polls and session turn ends trigger it automatically (at most once per 15 minutes per cycle, weights and oversized trees excluded), and `finalize` keeps its artifact IDs, then removes it. Legacy top-level writes are allowed only in the pre-activation compatibility window. See `core/CORE.md §3`.

## 6.1. Cross-Project Continuity Layer

`<agent-notes-root>` is separate from each project's artifact root. The artifact root holds research, spec, plans, documents, and experiments for one project; the notes root reads across projects and presents Layer 1 and Layer 2 continuity state.

| Layer | Owner | Example | Update path |
|---|---|---|---|
| `<agent-notes-root>/cards/` | User | Layer 1 task and project cards | Worklog-board UI or direct user edit |
| `<agent-notes-root>/_triage` | Retired review history | Read-only legacy records | Preserved until daemon cleanup |
| `<agent-notes-root>/digests`, `oncall`, `study`, `manual` | Loops and operators | Digests, reports, and manuals | Loop or board UI |

`_layer2/`, the two active queues, the retired `_triage` history, and the local board DB are mutable runtime or user state and must not be committed to the harness repository. They may live in a separate notes repository, still independent of harness core and adapters. `<worklog-board-app>` displays this root and processes feedback or review. Changes to the app belong to `autopilot-code` in the app repository; harness migration must not move or delete board data.

## 7. Routing Changes After the Initial Build

In a spec-backed project, a later fix or feature—especially in a new session—must not start with an ad-hoc edit. Follow understand existing artifacts → analyze → spec → implementation.

0. **Understand existing artifacts first:** follow the read-only orientation order in §0.1 before editing or choosing a capability, then read `spec/prd.md`, `pipeline_state.yaml`, and recent `plans/*`. Reading the spec that governs the declared work scope — root `prd.md` or the relevant `spec/<slug>/prd.md` — is a hard gate in a spec-backed cwd; which candidate governs remains agent judgment, recorded via route-record `spec_read.source`. Adapter-native markers and gates deny entry to spec-changing capabilities when a current spec of this project has not been read in the current session or has changed since the read.
1. **Refresh analysis when needed:** if `analysis_project/code/` is stale or the domain is unfamiliar, run incremental `analyze-project --mode code` first.
2. **Require a spec:** when absent, route to `autopilot-spec` before development. A single throwaway is the only exception, and repetition should graduate to a spec.
3. **Check spec drift before code:** compare the request with `spec/prd.md`. A route, schema/entity, UI-flow, external integration, migration, or existing code drift is spec-significant and routes through `autopilot-spec` update; when the transaction changes an existing PRD, the helper preserves its exact pre-image under `_internal/versions/v{N}/`. Proceed autonomously and report when drift is clear; ask when it is genuinely ambiguous. Record “no spec impact” for within-spec implementation details. `autopilot-code` repeats this verdict in preflight Step 0 as a backstop.
4. **Run `autopilot-code`:** intensity selects the graph. Direct performs inline production plus sanity/report. Quick uses one registered-headless dispatch-depth-1 one-shot conductor for micro-plan, plan-check-lite, focused verification, and report with no dispatch depth 2. Only `standard+` creates a durable `plans/<date>_<slug>/` cycle. Derived rigor never creates a separate plan cycle by itself.

These rules close three gaps: a broken trail caused by over-creating plans for quick work, spec drift that bypasses versioned spec update, and blind editing in a new session. Both `autopilot-spec` and `autopilot-code` are iterable; post-build change is another invocation of the same capability, not a new workflow family.
# Capability route topology

Every entry capability resolves through `capabilities/topologies.json`, the machine-readable execution-topology source. Intensity, topology class, worker kind, transport, DAG nodes, write scopes, promotion signals, and completion gates remain separate axes. `utilities/capability-route.py` compiles an immutable route bound to the registry digest (plus a `capability_registry_digest` over only the parts that route derives from: shared sections, its own capability's recipes and the gate contracts they cite, so an edit to another capability does not make it stale), source commit, physical absolute working directory, artifact root, and transport evidence. Adapters may project compact summaries and pointers, but must not copy the graph into bootstrap or Skill metadata.

The route compiler is **enforced** (promoted from report-only, 2026-07-22): every node
references a unit in `roles/units/`, and routing happens at entry only — a
dispatch-depth-2 worker never routes and never selects another worker. Enumerated
recipes are curated fast paths, not the default. For a request no recipe fits, the
entry composes its own route from the same catalog (**compose-on-demand**, §0.2.1):
`capability-route.py compose` seals a `direct`, `solo`, or `staged` shape. Staged
uses the capability recipe unless `--graph` selects a subgraph. Both use the same
validator and seal; a subgraph embeds its recipe as `composed: true`. Confirmation
follows §0.4 (`[경로]` for small work, the card or SD-123 pair otherwise).
Composition changes route *shape only* — it never bypasses the §0.1
spec/artifact-order gates, never grants dispatch depth 3, and never substitutes for a
capability's own completion gates.
