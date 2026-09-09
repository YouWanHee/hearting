# Adaptation Contract

This document defines how the neutral harness becomes a runtime-specific setting.
It is the boundary contract for `claude_setting/`, `codex_setting/`,
`opencode_setting/`, and future runtime projections.

## 1. Source Categories

Every file in this repo must fall into one category.

| Category | Meaning | Examples | Runtime projection rule |
|---|---|---|---|
| Portable source | Runtime-neutral semantics. Describes what must happen, not how a vendor runtime invokes it. | `core/`, portable parts of `tools/`, portable guard algorithms | May be symlinked into adapters if the runtime can read plain files |
| Adapter source | Runtime-specific representation of portable semantics. | `adapters/claude/CLAUDE.md`, `adapters/claude/settings.json`, `adapters/claude/commands/` | Projected into that runtime home |
| Adapter projection | Versioned mirror that exposes adapter source under runtime-expected names. | `claude_setting/`, `codex_setting/`, `opencode_setting/` | Symlink or generated output only; no independent semantics |
| Compatibility reference | Historical source kept for parity/drift checks after an adapter-owned realization exists. | `skills/` byte-equivalent to `adapters/claude/skills/` | Not projected as portable source; guarded against drift |
| Compatibility passthrough | Legacy file still consumed directly by a runtime before a true portable/adapted split exists. | Mixed shared hooks or utilities not yet split into invariant + adapter wrapper | Allowed only with an explicit debt note in the adapter |
| Runtime state | Tool-owned mutable local state. | `<runtime-home>/projects`, credentials, session logs, caches, DB files | Never committed to this repo |
| Improvement evidence state | Incidents, candidate fixtures, proposal evidence, approval references, and version-bound realization records. It is not active harness source. | `${XDG_STATE_HOME:-~/.local/state}/hearting/improvement` | Never projected or runtime-discovered; adopted source changes use a separate spec/code/release cycle |
| Continuity state | Cross-project agent worklog/notes data that survives sessions but is not harness source. | `<agent-notes-root>/cards`, `_layer2`, `_triage`, `digests`, `oncall`, `study` | Never committed to this repo; may be versioned in a separate notes/data repo |
| Local board app state | Worklog-board local app workspace, generated output, DB/cache, dispatch logs, and worktrees. | `<worklog-board-app>/.cache`, `.next`, `.dispatch`, `.env*`, `node_modules`, `<worklog-board-app>-wt/` | Never committed to this repo |

## 2. Adapter Rule

An adapter must not claim support for a surface unless it provides one of:

1. A native adapter file.
2. A generated file with a documented source.
3. An explicit compatibility reference or passthrough entry and the reason it is safe.

Plain symlinks are acceptable only as a projection mechanism. They are not proof
that adaptation is complete.

### 2.0. Sibling-Adapter Completion

**A portable change touches all adapters at once. Never deliver, commit, or
report a portable change for one adapter and leave the siblings for "later" — a
single-adapter split is the failure this section exists to prevent, not a normal
increment.** `core/`, `capabilities/`, and `roles/` are the semantic source.
Claude, Codex, OpenCode, and future adapters are equal sibling realizations below
that source; no adapter is the reference implementation or parent of another adapter.

A shared change follows one transaction:

1. update or confirm the portable invariant;
2. generate or edit every applicable sibling realization in the SAME unit of work
   (`generate.py` projects all three — never hand-mirror one and skip the rest);
3. verify each runtime's active discovery surface and required fallback
   (`check-adaptation-boundary.sh` audits all three adapters, not just Claude);
4. report the overall result as `PARTIAL` while any applicable row is deferred,
   unsupported without fallback, or unverified; report `GREEN` only after all
   applicable rows pass.

A sibling that reaches the portable source directly — e.g. Codex/OpenCode invoke
`$AGENT_HOME/utilities/<tool>` through their preflight wrapper instead of holding
an adapter-owned mirror — is a *covered* row, but only when that reachability is
measured, never assumed. State per-adapter coverage from evidence: never tell the
user one adapter is done and the others are "separate work" without having
checked all three first.

Generated output is not exempt from semantic, discovery, or footprint checks.
Runtime syntax may differ, but observable behavior, quality floors, and failure
reporting must remain equivalent.

## 2.1 Runtime Distribution Seam

Installing or exposing the harness in a runtime is its own adaptation seam. A
runtime surface is supported only when the adapter can name the runtime-native
entrypoint and prove that the runtime will discover it.

Use this order when adding a runtime surface:

1. Define the portable invariant in `core/`, `capabilities/`, or `roles/`.
2. Describe the runtime surface as data: kind, destination, invocation syntax,
   conversion rule, hook/config surface, and unsupported fallback.
3. Generate or maintain adapter-owned concrete output from the portable source.
4. Verify runtime discoverability or explicitly mark the surface unsupported.

An adapter must fail closed for unknown or undocumented runtime features. Do not
assume a Claude Code surface exists elsewhere because the purpose is similar.
For example, a runtime with native status, command, skill, hook, or plugin
support should use that native surface first; harness-specific gaps should be
bridged by adapter wrappers.

External reference: GSD Core
(`https://github.com/open-gsd/gsd-core`) uses the same seam shape: canonical
workflow files are transformed into runtime-specific artifacts, while Claude
plugin manifests and Codex skills are concrete runtime projections rather than
portable source. This repo should follow the pattern, not the exact file layout.

## 2.2 Runtime Currentness and Parity Claims

Before answering, planning, or editing adapter projection behavior for modern
runtime surfaces, verify the current runtime documentation and recent practice
instead of inferring from another adapter or from local harness state.

- **Claude Code / Codex surface questions require fresh external research**:
  read the current official documentation first, then inspect local adapter
  realization. Use community posts, issues, or examples only as secondary
  evidence for real-world gaps or practices, and label them as such.
- **Separate existence from parity**: if a runtime supports a feature in some
  form, still state whether it is equivalent to the other adapter's feature.
  Include concrete parity gaps such as model pinning, tool restriction,
  permission inheritance, session/worktree isolation, hook lifecycle, discovery,
  UI visibility, and noninteractive/headless behavior.
- **Plan with verification**: when a projection change depends on a runtime
  capability, the implementation plan must include a current-doc citation or
  note, a local runtime/projection check, and a fallback if the feature is
  unavailable, buggy, or unsupported in this adapter.

## 2.3 Proposal-Gated Runtime Improvement

An improvement proposal adopts a portable invariant, not a permanent runtime
implementation. The evidence loop may observe, reproduce, draft, and compare a
candidate, but it must not edit active source, generated projections, installed
plugins, or runtime-owned config. Adoption is a separate spec/code/release
cycle.

Each runtime realization is version-bound. A runtime, plugin, documentation, or
active-provider fingerprint change requires revalidation; it does not inherit a
past approval. If a native feature satisfies the fixture, retire the custom
realization while preserving the portable invariant and any required fallback.
Semantic conflicts are reviewed, never auto-merged. The operational state
contract is `loops/improvement.md`.

## 3. Portable Role Model

Portable docs use role names, not vendor model names:

| Portable role | Meaning |
|---|---|
| `fast reviewer` | Broad, low-latency review: coverage, style, cross-reference, formatting, simple consistency |
| `fast fact-checker` | Narrow source comparison: citations, years, metrics, verbatim matching |
| `fast writer` | Assembly from verified artifacts |
| `fast implementer` | Routine implementation and refactoring |
| `deep reviewer` | Architecture, methodology, safety, domain correctness, high-risk review |
| `deep maker` | High-judgment creation: planning, synthesis, visual/editorial craft |
| `deep orchestrator` | High-judgment conductor: stage gates, failover, and evidence synthesis for `standard+` dispatch-depth-1 work |
| `external adversary` | Independent reviewer with different model/runtime/process assumptions |
| `orchestrator` | Balanced mechanical coordination of already-decided tooling, paths, and report assembly; not a deep-conductor alias |

Adapters map two independent portable axes: `model_role` describes behavior, while `model_profile` (`deep|balanced-deep|balanced|light|mini`) is the sealed result of the judgment-demand × execution-scope resolver. Each adapter declares concrete models, effort/variant projections, profile granularity, and interactive-main-only families in `adapters/<adapter>/config/models.conf`; every resolver, wrapper, generated agent, lifecycle worker, and documentation table derives from that single source. A route-bound job carries both sealed axes and rejects trailing model/effort replacement. `mini` is unavailable to substantive registered dispatch-depth-1/2 owners, stages, and reviewers. A profile may share another profile's concrete model as long as the resulting execution points stay distinct; the ladder is a set of operating points, not a set of models. An adapter lacking a verified effort/variant distinction may collapse only the explicitly documented operating point (OpenCode balanced to light) with reduced-granularity metadata. Non-route surfaces may retain checked explicit selection or inheritance when the resulting model is execution-surface eligible; main-only or unprovable inheritance is a typed deny.

Adapter and projection edits are derived core-first: change the portable invariant in
`core/` first, read that governing core document in the current session, then update
the adapter realization and generated projection. A runtime marker proves the read
gate only; it does not replace this source-order review.

## 4. Capability Model

A portable capability describes:

- trigger semantics;
- required inputs and artifact roots;
- output contract;
- Verification rigor (intensity-derived) semantics;
- delegation roles using the portable role model;
- deterministic guards and side effects;
- recovery and audit requirements.

A runtime skill/slash command/native instruction describes:

- how that runtime invokes the capability;
- which tools are available;
- how subagents or reviewers are spawned;
- how confirmation, pause, and user input work;
- how hook events are attached;
- runtime-specific file formats and frontmatter.

Current `skills/*/SKILL.md` files are compatibility references. Claude Code
consumes adapter-owned concrete files under
`adapters/claude/skills/*/SKILL.md`. Portable capability meaning belongs in
`capabilities/`.

## 5. Hook Model

Portable hook semantics are named by invariant:

| Invariant | Portable meaning |
|---|---|
| artifact order | New artifacts must be created in the allowed dependency order |
| git state safety | Do not edit during merge/rebase/cherry-pick/detached unsafe states |
| spec read gate | Spec-backed work must read the current blueprint before changing code/spec |
| core first gate | Adapter edits must be grounded in an actual current-session read of the relevant core contract |
| memory write guard | Runtime-native memory files must not bypass the unified memory store |
| memory recall/inject/distill | Inject relevant memory and optionally distill session deltas |
| worklog state signal | Surface the configured notes root and board app status without moving or mutating data |
| peer-session steering ledger | Write one append-only, body-free `peer_message_v1` record per outbound and inbound cross-session message (`OPERATIONS §5.14`) |

Adapters decide whether each invariant is enforced by native hook, wrapper,
manual preflight, or unsupported fallback. Realized (steward role, `OPERATIONS §5.14`,
v56 herdr-unified): Claude — `PostToolUse(SendMessage)` + `UserPromptSubmit` (measured,
`hooks/peer-message-record.py`) for the ledger, `utilities/peer-steward.py wait` →
`herdr agent wait` (measured) for bounded foreground watching, and for detached watching
`utilities/peer-steward.py watch/join/status/rearm/ack` plus two carriers — a
`PostToolUse(Bash)` `asyncRewake` hook (`hooks/peer-steward-rewake.py`, exit 2 wakes,
spec-only until a live-session measurement) and a `UserPromptSubmit` sweep of un-acked
receipts in the same `hooks/peer-message-record.py` (fail-soft, ≤5 lines of
`additionalContext`). The carrier reaches watch state only through the utility's
subcommands, never through the state files, so a runtime without a wake carrier keeps the
same schema. Codex — `herdr agent wait` watching is measured; the portable
`watch/join/status/rearm/ack` subcommands work, but there is no wake carrier and next-turn
receipt recovery is unmeasured (P-7); carrier parity is a separate decision. The managed
gateway's `steer`/`watch-idle` ops are **not implemented** (closed-by-decision, P-1 through
P-5 retired, not pending). OpenCode — `unknown`, pending probe P-6.

## 6. Projection Invariant

Runtime homes keep their expected names. Common docs describe this generically;
adapter docs own the concrete runtime-home paths and bootstrap filenames:

```text
<runtime-home>/<adapter-bootstrap>
<runtime-home>/<runtime-settings>
<runtime-home>/<runtime-command-or-skill-surface>/
```

Those paths may symlink into versioned projection directories such as
`claude_setting/`, `codex_setting/`, or `opencode_setting/`. The projection
directory must make it clear whether each entry is native adapter output,
portable passthrough, or compatibility debt.

**Projection completeness**: a cross-adapter guard that checks whether every
portable source item has a corresponding adapter-side projection must
**enumerate the source domain** (iterate the actual current entries) rather
than assert a hardcoded list fixed at authoring time — a hardcoded list stops
catching new entries the moment the source domain grows, silently reopening
the exact gap the guard exists to close. This applies at minimum to agents,
hook events, tools, utilities, and scaffolds. Any intentional exclusion from
projection belongs in an explicit exemption or name-mapping list next to the
guard, never as a silent omission, so every excluded entry is a declared
decision rather than an accidental leak.

### 6.1. Active Context Budget

Progressive disclosure applies to runtime bootstraps and discovery metadata,
not only Skill bodies. The bootstrap is a router: source order, hard invariants,
and runtime entrypoints stay resident; detailed lifecycle explanations,
examples, and edge cases live in adapter README/ADAPTATION documents or command
help loaded on demand.

- Each always-loaded adapter bootstrap is at most `16,384` UTF-8 bytes.
- Each active Skill metadata discovery surface is at most `7,000` characters,
  including its concrete local Skill paths. Regression baselines normalize
  those paths relative to the surface root so checkout location is not source
  growth.
- Activating two surfaces with the same Skill names is a duplicate-discovery
  failure, not extra assurance.
- The same always-loaded bootstrap must reach a session exactly once. When a
  runtime both auto-loads a bootstrap filename and reads a configured
  instruction list, an adapter picks one carrier and keeps the other empty;
  runtimes commonly dedupe instruction sources by resolved path, so one file
  behind two absolute paths — a symlink and its target, or two projections of
  it — is injected twice rather than deduped. Verification asserts the number
  of carriers, not merely that some carrier exists.
- A stored surface baseline rejects growth greater than five percent unless the
  same change records a reviewed rationale and updates the budget.
- Model-visible surface budget: the nine documents an agent reads to route
  and dispatch work (`core/{CORE,WORKFLOW,CONVENTIONS,OPERATIONS,HOOKS,MEMORY}.md`,
  `adapters/claude/CLAUDE.md`, the `autopilot-code` `dev-pipeline` and
  `owner-execution` references) carry sealed per-file byte and directive caps
  in `tools/surface-budget.json` and a total ceiling in
  `tools/check-surface-budget.py`. Caps are per file and independent: a
  change that grows any one of them fails the boundary check even when
  another shrinks. The only way to grow a file is to reseal in the same
  change with a recorded `--reason`, and a reseal is refused outright when
  the measured total would exceed the code ceiling. Reductions are locked in
  by resealing downward. The ceiling is lowered only in a commit that lands a
  measured reduction and is never raised by editing the budget file.
- Ordinary, unknown, and repeated hook states inject zero bytes. A verified
  pressure-band transition may emit one compact directive of at most 240 UTF-8
  bytes.

These are footprint controls, not token or billing estimators. Static bytes,
code lines, directive counts, and monotonic runtime counters must not be
converted into savings claims. A production savings claim requires at least 30
paired real sessions and separates input, cache creation, output, and billable
cost. Synthetic fixtures prove regression behavior only.

## 7. Completion Delivery Carriers (runtime-owned)

The model-visible contract is one printed field (`core/OPERATIONS.md §5.10a`,
`core/HOOKS.md` "registered-child completion delivery"): a parent obeys
`parent_next=end-turn|bounded-wait`. Everything below is how the runtime
honours that field. It was moved here verbatim on 2026-09-09 from
`core/OPERATIONS.md` §5.10/§5.10a/§5.14 and `core/HOOKS.md` so that no agent
prompt carries the carrier taxonomy (dispatch-complexity diagnosis
`rrev_c5dd77e9` R3); the SD references and the leaf
`utilities/parent_next_directive.py` are unchanged.

### 7.1. Registered owner supervision (SD-14/78/92/113)

- **Runtime-owned completion delivery under SD-14/78:** a registered `standard+` headless owner is launched under an adapter supervisor, not as an unresumable one-shot model turn. The model registers every separable child in the current batch and yields `runtime_wait: registered-children`; the supervisor snapshots only current v2 rows sealed to `parent_attempt_id=$AGENT_DISPATCH_ATTEMPT_ID`, joins every parallel attempt through canonical liveness outside the model/tool loop, and sends the same session exactly one bounded typed receipt when the whole batch is semantically terminal **and execution-quiescent**, or requires typed attention. Child output, transcript text, artifact bodies, source, git state, and liveness prose never enter that receipt. Codex realizes the bridge with one ephemeral App Server thread and repeated `turn/start` after `turn/completed`; a registered Claude owner realizes its internal batch bridge with one `--session-id` followed by `--resume`. An interactive Claude parent uses a separate `PostToolUse(Bash)` `asyncRewake` bridge: only a successful exact `dispatch-owner --start` bound to the same Claude session may arm it, proved either by that start's stdout receipt or — when the caller filtered that stdout away — by the one lock-written registry row carrying the same session, `worker_type=owner`, dispatch depth 1, `parent_completion_delivery=claude-parent-runtime`, and claimed/started evidence, still open inside a bounded recent window; zero or several candidate rows arm nothing, because absence beats misattribution. It watches that one owner attempt to terminal quiescence outside the model, and exits once with a bounded exact-attempt receipt. It never launches a visible background `dispatch-wait`, Monitor, progress recap, or periodic re-arm; explicit `poll-fallback` remains the only model-owned wait. Intermediate turn/result events are withheld from the terminal handoff, and only the final exact three-line envelope is exposed as terminal. Before every model turn the supervisor atomically publishes an attempt-scoped schema-v2 phase state: `parked`, `deliverable`, `running-turn`, `recovery`, or `terminal`. While an undelivered child is open or terminal-but-draining, the native pre-tool policy admits only one exact same-parent `dispatch-batch --action start` for a declared parallel group (or a non-group exact `dispatch-node --action start`), so a first child cannot prevent its checked siblings from registering. Once any delivered child remains open or draining, the policy admits only exact typed harvest for that delivered batch. Both phases reject model waits, raw inspection, liveness, unrelated tools, and shell composition; missing/invalid phase state is recovery-only exact harvest. Codex enforces this through its projected hook and Claude through a command-scoped `--settings` PreToolUse bridge without mutating user-owned runtime settings. Multiple sequential route batches repeat this one-resume transaction. A bounded join timeout is an internal repark checkpoint: it emits no model receipt, does not update the delivered set or consume continuation budget, and makes the same supervisor rejoin the same sealed child set. A second, distinct internal repark checkpoint (SD-119) advances a serial sub-session chain registered under `utilities/stage-session-chain.py`: once the joined child is a chain participant and terminal, the supervisor claims and starts the chain's next index itself, folds the closed predecessor into the delivered set so it is never re-surfaced, and rejoins — again with no model turn and no continuation spend — until the chain either completes (falls through to the ordinary route-level flow) or the joined child carries no chain metadata at all. Only terminal-and-quiescent or a typed attention condition is actionable. An exception with owned open children preserves state and lease in `recovery`; only terminal-and-quiescent completion removes them. A dispatch-depth-0 interactive Codex parent has a separate native realization: a direct registered dispatch-depth-1 attempt bound to the actual `CODEX_THREAD_ID` seals `parent_completion_delivery=codex-stop-hook`; `launch_claimed=0` registration alone never parks the parent, and a start requires the **current exact Stop and PreToolUse hook definitions** to be trusted before it may claim or spawn the process. Immediately after successful spawn, the wrapper atomically binds the exact attempt into hashed-session pending state, then the parent ends its model turn. Stop follows that immutable set even if an orphan watcher has already changed a child row to `done`, joins it outside the model, publishes the delivered phase, and returns one bounded `decision=block` continuation only when exact harvest is ready. While undelivered, PreToolUse admits no model tool—including `dispatch-wait`; after delivery it admits only exact-attempt harvest with `--status all`, so an `open`→`done` watcher transition cannot invalidate the continuation. A valid harvest consumes its exact receipt, and the final receipt removes the session state. A bounded Stop timeout yields one minimal end-turn/re-enter instruction rather than a polling tool loop. Foreign, legacy, registered-only, untrusted, or unstamped rows never enter this path and retain the explicitly reported polling/recovery contract. Runtime support is probed before launch: forced supervised mode fails closed, interactive native Stop delivery fails before spawn when current-hash trust cannot be proved, while other unavailable same-session bridges may use the explicitly reported `poll-fallback` (`dispatch-wait --attempt-id <id> --max 300..600`). After a supervisor has started, protocol/session failure never replays the assignment through a one-shot fallback. Arbitrary detached shell output still does not auto-resume; only the checked completion-delivery surfaces above do. Parent ownership remains exact, foreign or stale rows never wake the owner, and post-exit orphan reconcile remains mandatory and independent.

- **Codex launch-publication settle under SD-14/78:** an App Server turn may deliver the exact `runtime_wait: registered-children` sentinel in the narrow interval after atomic child registration but before every fenced wrapper has appended `launch_started=1`. The Codex owner supervisor therefore performs one short, bounded reread of only undelivered exact-parent rows before issuing `registration-required`. A batch that reaches the existing durable `launch_started=1` fence during that settle window parks and joins normally without consuming a continuation or replaying the dispatch; a row that remains registered-only still receives the existing bounded correction. The settle loop holds no registry lock, accepts no artifact, transcript, PID guess, or stale delivered row as launch proof, and never starts or retries a child itself.

- **Managed interactive Codex boundary under SD-92 (supersedes SD-91 and the interactive clauses of SD-83 and the preceding SD-78 paragraph):** automatic completion delivery is a new-session boundary entered through `utilities/codex-managed-entry.py`; an existing TUI is never hot-upgraded. A user-authorized harness install may make that boundary transparent by installing a reversible launcher for interactive `codex`, `resume`, and `fork`, while preserving the resolved real Codex command and passing every non-interactive or administrative subcommand through unchanged. Plugin metadata or a lifecycle hook alone never claims launcher ownership because both load after process entry. One private owner-only gateway is the sole upstream App Server client for that thread; remote TUI client A owns subscriptions, transcript display, and every approval response, while completion sidecar client B may use only the gateway's private control socket and never connects upstream or acquires approval authority. The gateway serializes manual input and completion delivery under one atomic thread-state claim: completion starts one `turn/start` only when the thread is idle, or one `turn/steer` when a live turn accepts steering. A durable sealed-batch ledger treats `prepared` as retryable, an accepted receipt as replayable without another wake, and an upstream disconnect after send as `sent-ambiguous` with no automatic resend. `clientUserMessageId` is metadata only, not the deduplication primitive. A direct registered dispatch-depth-1 sidecar is launched after immutable registration but before the worker spawn claim, waits for that exact `launch_claimed=1`, then joins only the exact terminal and quiescent batch and submits one bounded typed receipt with no raw child output; absence of an exact launch fails closed. Parent runtime selects the wake adapter independently of child runtime: a Codex parent uses this managed gateway for Codex or Claude children, while a Claude parent keeps its Claude async-rewake/`--resume` supervisor for either child. Managed Codex completion uses neither a Stop continuation prompt nor an all-tool PreToolUse parent park, so the interactive parent remains available while children run. Managed launches also probe the exact effective `default_mode_request_user_input` feature row and, when supported, process-locally enable it in both the App Server and remote TUI children without writing user config; a per-launch disable wins across both processes, and unsupported probing warns once then launches without injection. The same gateway observes only typed `(threadId, requestId)` identity and time for `item/tool/requestUserInput`, publishes content-free `codex-appserver` evidence, and clears only its own evidence on an exact response, `serverRequest/resolved`, turn completion/interruption, or disconnect. It forwards every RPC unchanged and never renders, answers, approves, blocks, or owns user input; the TUI remains sole input and approval owner. Unmanaged clients remain unknown without a real producer, while rollout parsing remains legacy fallback only. An unmanaged interactive Codex parent cannot register or start a new detached dispatch-depth-1 owner through the portable owner selector: the selected child adapter must retain the actual caller runtime and fail before registry mutation or spawn with `managed-entry-required`. The low-level operator-only `--allow-unmanaged-parent-poll` escape hatch preserves a disclosed finite recovery path, is forbidden by `dispatch-owner`, and is never selected automatically by a model route. Sessions with an already-open legacy attempt or trusted `codex-stop-hook` state retain only finite migration/recovery behavior, and exact terminal `--status all --attempt-id` harvest may consume one legacy receipt. Open, stale, foreign, older-attempt, broad-selector, raw-output, and synthetic user/developer-message paths have no wake authority. A registered Codex headless owner continues to use its separate private App Server supervisor. Installer ownership must be manifest-backed, update-repairable, collision-safe, and exactly reversible on uninstall; private runtime state and the real CLI binding fail closed when validation is unavailable. Protocol ambiguity remains fail-closed and is reported as the upstream `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake gap.

**`parent-runtime-supervised` completion delivery (SD-113).** A row whose
`parent_completion_delivery = parent-runtime-supervised` never gets a
pending-delivery record — its completion delivery is owned solely by the
SD-78 supervisor above (§7.1), not by the `delivery_intent`/`RECIPIENT_KINDS`
stamp path in `core/HOOKS.md`.

### 7.2. Completion delivery clarifications (SD-92/97, SD-123/129)

- **Human gate in flight (SD-123 (8), SD-129).** While the armed Claude
  `asyncRewake` hook or the runtime-owned Codex completion sidecar waits on an
  open owner attempt it also watches, once per interval, for
  a pending gate record addressed to this session and raised by that attempt;
  when one appears the hook spends its single wake immediately (exit 2,
  `owner=alive-waiting`), leaves the record `sent-ambiguous` (the wake is
  speculative, so the next-prompt sweep can still re-deliver it once if the
  wake was lost; the release retires it either way), and tells the session to
  put the `[방향 확인]` card and interview questions to the user and record
  the answer with `workflow-supervisor.py release`. That release command — the
  typed `release` or legacy `gate --release` surface, run in the same session
  — is the second arming event: from the `route_id` in its JSON output (never
  from the `--route` literal, which a refused release names too) and the
  registry's one started open depth-1 owner bound to this session, a new hook
  process waits on the owner's completion exactly as the start did. A refused
  release arms nothing silently, with one exception (SD-OPEN-48): a release
  the supervisor refused as already released prints one typed JSON line
  (`refusal: gate-not-blocked` with the `route_id`) and that line arms the
  route's running owner — it is heading for a completion that still owes the
  session a wake; the prose and the `--route` literal never arm;
  a recorded release with no started open owner (the owner ended at the gate,
  or was refused at start) or an ambiguous owner set arms nothing and emits
  one typed `not-armed surface=release` (or `surface=release-refused`)
  notice; the `UserPromptSubmit` sweep still delivers the pending record at
  the next prompt. A raise seals `release_authority` (`depth-0` for an
  interview gate, a binding that declares it, or an artifact that declares it
  about itself; `any` otherwise): a registered headless owner's `release` /
  `gate --release` of a `depth-0` gate is refused typed
  (`gate-release-authority-refused`), and an artifact that calls itself an
  interview under another schema is refused at the raise
  (`interview-schema-unsupported`). While waiting, an announced-but-unclaimable gate record never spins
  the hook: the probe skips records whose reclaim budget is spent (the release
  expires such a record as `receipt-row-superseded`) and sleeps one interval
  after an empty announce, under the same overall deadline; it reads the
  registry once and rescans the recipient directory only when a record was
  written or a lease it saw has expired. The launch fence
  (`dispatch_contract.completion_marker_gate`) refuses a node whose entry gate
  is unreleased only for gates that some node of the route raises through its
  continuation **and** that an owner contract implements
  (`dispatch_contract.FENCED_HUMAN_GATES`, today `frame-review`); a binding the
  topology merely declares is not a mandatory step. The owner itself waits on `workflow-supervisor.py await-release`
  (bounded, read-only), and every launch surface refuses to start a node whose
  entry gate is not released (`human-gate-unreleased`/`human-gate-not-raised`).
  A Codex delivery uses a distinct strict `human-gate` receipt and `hg-dlv-*`
  gateway identity. It binds the route id/hash/file, gate raise epoch, exact
  live owner attempt and sealed batch, immutable registry, recipient thread and
  gateway epoch, artifact, journal, and release authority before claim; the
  gateway validates the same receipt before one start/steer and never interprets
  it as a completion receipt.
  If a malformed historical route or terminal owner makes a `pending` or
  `sent-ambiguous` record impossible to release, an operator may preview its
  cancellation and then repeat with `--apply`:
  `python3 utilities/workflow-supervisor.py recover-gate-delivery --route
  <route.json> --gate <gate> --delivery-id <delivery-id> --recipient
  <parent-session-id> --source-attempt-id <att-id> --raise-epoch <n> --actor
  <operator-id> --reason <audit-reason> --jobs <canonical-jobs.log> [--apply]`.
  Recovery accepts only that exact route/gate/epoch/delivery/recipient/source
  tuple and a terminal registered owner; it rejects a claimed carrier, a live
  owner or different live gate, records an audited `expired` state atomically,
  and never writes a release, deletes the route/registry/marker/record, or turns
  the source attempt into PASS.
- The interactive Claude `asyncRewake` bridge recognizes both an exact
  `dispatch-owner --start` and the quick one-shot
  `dispatch-node --action start` surface. Neither command is wake authority by
  itself: arming still requires exactly one recent, same-session,
  claimed-and-started depth-1 `worker_type=owner` row stamped
  `parent_completion_delivery=claude-parent-runtime`. When several such rows
  share the window — a same-session wave of owner starts — the registry
  fallback first narrows them by the exact `--slug`/`--worktree` literals of
  the observed start command (unexpanded shell variables never match), and
  only an exact single survivor arms; zero, still-ambiguous, stale, foreign,
  or non-owner candidates arm nothing. A start whose receipt proves
  `started=1` (or whose fully-hidden stdout leaves at least one same-session
  registry candidate) but that armed neither path emits one typed
  `not-armed` notice naming the explicit poll-fallback instead of staying
  silent — grep-filtered start stdout is the recurring cause (2026-09-01,
  five fleet owners; the notice never overrides fail-closed arming, it only
  makes the loss loud).
- Managed receipt schema v2 binds the one canonical absolute `job_registry`
  supplied by its completion sidecar. The gateway includes it in the delivery
  digest and names it with `--jobs` in every actionable harvest command, so
  packaged `AGENT_HOME` is never used to reconstruct the registry and an exact
  receipt cannot become `matched=0` by selecting another state root. The receipt
  remains bounded to 2,048 UTF-8 bytes; the complete typed context has its own
  finite bound.
- An SD-92 managed-gateway readiness refusal carries exactly one typed
  `reason_class` from a closed five-member set —
  `expected-thread-not-witnessed`, `lineage-mismatch`, `tui-disconnected`,
  `approval-owner-mismatch`, `upstream-client-count-invalid` — chosen by
  evaluating the conditions in a fixed documented order so exactly one class
  applies. This is diagnosis only: the portable aggregate outcome token
  `managed-gateway-not-ready` and the advanced-thread acceptance rule at
  `core/OPERATIONS.md` §5.10 ("SD-92 advanced-thread clause") are unchanged, and the existing pre-status
  typed reasons (`managed-entry-not-enabled`, `managed-parent-runtime-mismatch`,
  `managed-parent-harness-mismatch`, `managed-parent-thread-mismatch`,
  `managed-control-missing`, the `managed-control-*`/
  `managed-state-directory-unsafe` socket reasons, and the `managed-status-*`
  framing reasons) are disjoint from this set and stay unchanged.

### 7.3. Detached steward watch carrier (SD-122)

**Detached watch realization.** `peer-steward.py watch <target>` takes a blocking exclusive lock on the dedupe
claim, checks the target once with `herdr agent get`, takes the watch lock, spawns one
`setsid` watcher holding that same lock, writes an immutable arm record carrying the
watcher's `{pid, pid_start}`, records `kind=watch … receipt=<watch_id>`, and returns a
typed `state=armed` line without waiting. Every steward line reporting an armed watch
carries the same `parent_next` directive a launch receipt does, and claims `end-turn` only
when the hook arms from *that* line and the watch's wake is the hook; every other line —
`wake=none`, a dedupe hit, a session-printed `rearm` — prints `bounded-wait` with a bounded
`join <watch_id>`. The watcher calls `herdr agent wait` exactly
once, writes `peer_watch_receipt_v1` atomically, records `kind=notice status=received`,
and releases the lock only by exiting — the receipt rename strictly precedes exit. `join`
waits on that lock (a kernel wait, never a poll), so it returns on the watcher's exit
event; it reports `watcher-dead` only when there is no receipt **and** the arm record's
PID identity fails, because lock acquisition alone would misread spawn latency as death.
`status` reports `armed|alive|receipt|acked`, where `alive` needs pid, `/proc` start ticks,
and lock possession together. `rearm` replaces only a dead un-receipted watch, always under
a new `watch_id` so receipt and ack paths never overlap. The receipt carries no screen or
message text: per S4 the steward reads `herdr agent read` and disk itself (idle ≠ done).

On Claude, `PostToolUse(Bash)` `asyncRewake` hook `peer-steward-rewake.py` arms only from a
same-session armed line with `wake=hook` whose receipt sits under the canonical state root,
`join`s within one deadline computed at hook entry, acks, and exits 2. Its survival across
user interrupt, compaction, and session end is **unmeasured**, which is why receipt
durability and the fallback carrier are mandatory: the `UserPromptSubmit` sweep surfaces up
to five un-acked receipts for the current session and acks them, so a dead hook or a
restarted session loses nothing. Wake is at-least-once and display is idempotent — the ack
file is created `O_EXCL` by whichever carrier gets there first.

### 7.4. Carrier taxonomy and selection

A registered headless owner yields after registering a batch; its runtime supervisor joins the exact `parent_attempt_id` batch outside the model and resumes the same owned session once with a bounded typed receipt. A registered Claude owner keeps one realtime stream-input process for the route and submits the next receipt immediately after each non-terminal join; when a freshly verified terminal marker closes every declared terminal gate, the supervisor skips the redundant final owner turn and closes the stream before terminal row reconciliation. An explicit custom-command fallback retains per-turn `--resume`. A Claude interactive parent may instead arm one native `asyncRewake` PostToolUse hook from a successful exact owner-start receipt, or from a successful exact steward watch armed in the same session (`utilities/peer-steward.py watch`, SD-122 §13.37.2-(10)); either way the hook owns exactly one arming event, verifies it against the session that produced it, and never widens to another attempt or watch. It re-reads the exact current row and sealed completion evidence before rendering: every terminal receipt — success or attention — exits two, because Claude Code wakes an idle session for an `asyncRewake` hook only on exit code 2 and delivers exit-0 output no earlier than the next user interaction (corrected 2026-08-29; success additionally carries its structured notification on stdout). A launcher-managed interactive Codex session places one owner-only gateway between remote TUI and App Server. The harness installer may make this checked entry transparent for interactive commands, but plugin or hook loading after process entry is not equivalent. That gateway atomically serializes manual input with an exact completion receipt, uses `turn/start` only while idle and `turn/steer` only for a steerable active turn, and durably suppresses duplicate sealed-batch delivery. The sidecar is prelaunched before the child spawn claim, connects only to the private control socket, never subscribes upstream, never sees or answers approvals, and submits no raw child output. A send followed by an unclassified disconnect is `sent-ambiguous` and is not retried. Outside those checked entries, hooks must not simulate wake by blocking Stop, parking every tool, or injecting a synthetic user turn; the parent remains conversational and uses a disclosed finite fallback. Legacy receipts may be consumed only by exact terminal typed harvest. Runtime-native subagents are a separate surface.

Select delivery by parent runtime, never child runtime: Codex managed parent → Codex gateway; Claude interactive parent → exact owner or exact steward-watch `asyncRewake` with an exit-2 wake for every terminal receipt, plus the SessionStart/UserPromptSubmit sweep that re-delivers any SD-111 pending record or un-acked watch receipt at the next prompt; registered Claude owner → persistent realtime stream with a sealed-terminal fast path (checked per-turn `--resume` fallback); registered Codex headless owner → its private App Server supervisor. Keep TUI client A as the only approval owner and sidecar client B control-only. Require private socket/state paths, exact terminal+quiescent membership, durable idempotency, bounded typed context, and fail-closed ambiguity. A transparent launcher must preserve and validate the real CLI, route only interactive surfaces, repair on update, and restore exactly on uninstall. If those checks are unavailable, report fallback and the missing atomic `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake primitive rather than widening Stop or PreToolUse.
