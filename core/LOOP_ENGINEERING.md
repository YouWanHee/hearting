# Hearting Development Philosophy — Loop Engineering

Hearting owns the loop that carries a user's intent all the way to a verified
result and its handoff. It is judged not by how many mechanisms restrict
execution, but by its ability to finish work without losing it in normal,
failure, and interruption cases.

Here Loop Engineering is the name for Hearting's development principles. It
does not mean adopting a particular outside methodology. It adds no procedure to
read or submit for each new task. Maintainers apply it when judging whether a
structure or a change is sound.

## 0. Three standing goals

The user set these as Hearting's core goals (2026-10-06). Sections 1–6
describe how to reach them; judge any structure or change against these first.

1. **Light dispatch surface.** An agent that dispatches work states only the
   goal, inputs, scope, and completion condition. Whatever the runtime already
   knows or can settle on its own — provenance, paths, parent and identity,
   default model and harness, access, retry and recovery commands — Hearting
   fills in. (See §2.)
2. **Free but systematic planning at frame.** The frame stage shapes the plan
   to the task instead of forcing a fixed template, fitting the work like a
   tailored suit, yet the plan names its stages, parallel parts, verification,
   and completion conditions clearly enough to run, resume, and hand off
   without deciding them again. Recipes and presets are only default shapes.
3. **Harness-independent consistency.** The same request means the same thing,
   is judged the same way, and finishes the same way on Claude, Codex, and
   OpenCode. Decisions live once in the shared layer; adapters only translate
   execution. There are no per-harness exception branches or duplicated
   judgments. (See §3 and §5.)

## 1. The unit of a feature is a responsibility carried to the end

`intent → execution → observation → result settlement → follow-up or recovery → closure → handoff`

Each transition has exactly one decision owner. Several observers may collect
evidence, but none of them may overturn a success or create a retry on its own.
When responsibility for work changes hands, what was handed over and who
received it must be recorded. The original owner keeps its obligations until
the handoff is confirmed. Process exit, report writing, task completion, and
user notification are different facts.

| Decision | Responsibility that must carry through |
|---|---|
| Allow execution | Bind the exact execution identity to its lifetime manager. |
| Defer or refuse execution | Return the cause, the remaining obligations, the owner, and the supported next action. |
| Settle completion | Check content, evidence, and cleanup state, and preserve the settled result. |
| Retry | Check the existing result and actual cleanup first, and prevent duplicate execution. |
| Observation failure or stall | Preserve the unsettled state and keep the responsibility to re-observe or hand off to a person. |
| Closure | Leave unfinished follow-up stages, artifact sealing, and failure cleanup in a recoverable state. |
| Notification | Distinguish support, queue acceptance, delivery, and receipt, and connect a follow-up handoff path on failure. |

Recording an owner's name does not by itself implement this responsibility. The
real execution path must be bound to that owner, and the obligations must be
recoverable after exit or restart.

## 2. Keep the surface simple and the defaults replaceable

Users and agents state the goal, inputs, scope, and completion condition.
Hearting assembles paths, state, call order, and recovery commands. Do not make
anyone repeat the same content across several flags and documents to satisfy an
internal contract.

Capabilities and recipes are useful default configurations, not grounds for
forcing the same stages on every task. Keep work stages such as planning and
verification separate from dispatching a separate process. Dispatch when
independent review or parallel work pays off, and keep the same result and
recovery responsibility when direct execution is allowed.

Model, profile, and capacity policies are defaults too. An easy task may choose
light, and an explicit choice is honored within the supported range. Explain
constraints such as authentication, real availability, approval boundaries, and
evidence integrity separately. Do not silently change a choice, and do not turn
a simple preset difference into "cannot run".

## 3. One shared meaning, runtime-specific execution

Claude, Codex, and OpenCode are equal adapters. The shared layer owns the
meaning of input, completion, retry, cleanup, and handoff. Process start,
resume, and notification mechanisms are implemented to fit each runtime. No
harness is the reference implementation for another.

Do not mark an unsupported transport as supported. Confirm the available
fallback path and who takes the next action. Matching surface names or ledger
states are not evidence of equal responsibility.

## 4. Do not dress uncertainty up as failure or success

A process that cannot be observed is unknown. Do not declare it dead because it
is invisible from another PID namespace or because its log is late. A stall
alert deadline is a deadline to re-observe and notify, not authority to end or
retry the work. The workload declares the progress standard for long work; a
uniform time cap does not replace it.

Preserve settled PASS and FAIL results. A later conflict may hold back current
consumption but never overwrites a past result. Follow-up work connects exact
lineage and current evidence instead of rewriting the original. Reusing existing
work is distinct from producing a new verification PASS.

## 5. Remove duplicated decisions before stacking exceptions

A repair does not end at letting the one failing branch pass. Find the writers,
readers, watchers, and adapters that make the same decision and gather them
into one shared judgment and transition. When a new path replaces an old one,
also remove the duplicate implementation and the caller procedures it made
unnecessary. Do not hide a responsibility gap by removing every guard or by only
adding new refusal rules.

Documentation explains behavior and preserves the reasons for decisions. Do not
build the product on a protocol that only ends cleanly when a model perfectly
remembers long operating documents.

## 6. Prove completion through the consumer path

Verification of a fix covers not only normal completion but also late results,
duplicate retries, unobservable state, supervisor exit and restart, delivery
failure, and a busy owner. Choose regressions that fit the change's risk, and
do not claim real receipt from registration or a passing fixture. Any needed
live measurement connects the exact execution, result, and receipt.

A completion report states the behavior fixed, the duplicate decisions and
manual procedures removed, the final owner, and the boundary between what was
actually verified and what was not. Keep the versions of source, release,
installed copy, and running session distinct. Measure needless model turns,
restarts, manual intervention, and boundary wait time, but do not present a
smaller document or fewer lines of code as token or cost savings.

## 7. Recovery follows lineage, not replay

Between an original attempt and its recovery, work changes hands in ordinary
ways: a supervisor seat is handed over, an owner is replaced, the parent moves a
harness or model pin, a predecessor fails or is paused by a guard, or only an
observation step fails. A retry, replacement, continuation, or resume accepts
these changes through one shared lineage rule. It does not rebuild the original
record from current values and refuse at the first difference.

The shared recovery judgment reads stored inputs before reconstructing missing
legacy inputs from their original sealed route and exact original rows. Current
selection and authority are checked afresh at registration and launch. Work,
review subject, fixed inputs, session scope and granted addresses remain bound;
parent, owner and runtime selection follow their recorded lineage. A claim alone
does not adopt a continuation. Process quiescence and terminal result availability
are separate observations consumed by resume, reconciliation and settlement alike.

Exact comparison protects sealed evidence from tampering; it is not a reason to
refuse the normal ways work moves. When a recovery path still refuses, the
refusal names what differs and the supported next step. A state with no
supported way forward is a defect in the path, not a task for the operator.

## 8. Let real work reshape capabilities and routes

Capabilities, shapes, and routes are revised from the record of real work, not
defended as fixed contracts. Periodically read what users and agents actually
ran, where they worked around a capability, where work stalled, and where a
person had to step in. Widen a capability that work keeps stretching, merge or
drop one that is rarely used or keeps blocking, and loosen a route rule that
refuses approved work. Changing the registry or route rules must not strand
routes that are already running.

## 9. Move hard parity outside the adapters

When a behavior has to be built separately inside each runtime and keeps
diverging — waking a parent, watching a session, session identity, pane
placement, remote compute access — build it once in a runtime-neutral layer
outside the adapters and let all three harnesses use it, as herdr with
`peer-steward` does for sessions, `compute-hosts` for remote GPUs, and Fleet for
observation. Adapters then only translate. Prefer one outer mechanism that every
harness shares over three inner realizations kept in step by hand. Shared
meaning stays in the core (§3); a shared mechanism may live outside it.

## 10. The operator sees delegated work at a glance

Fleet is how a person sees the work they delegated, and for dispatched sessions
it is often the only way. It opens quickly (first frame in about five seconds)
and shows, for every live session and dispatched owner, what is happening now:
the model turn, the running command, or the resource the owner is waiting on
with its host, GPU, and progress. An empty NOW while work is running is a
defect. A slower or emptier view is a regression even when every check passes.

## Basis and current scope

These principles condense the completion standard the user repeatedly corrected
in September 2026. An earlier structural diagnosis pointed at boundary costs and
operating procedure leaking to the model, and the dispatch responsibility repair
record keeps both the real completions and the failures. Adopting this document
does not mean unrepaired paths are complete. Each change must prove itself
against the standards above.


Sections 7–10 restate a memo the user wrote in Hearting's v1 era, made standing
direction on 2026-10-09. On that day seven defects of the §7 kind (PR #427–#432
and related) were each fixed by a case-specific allowance, one Fleet render took
46 seconds, and three dispatched owners waiting on resources showed an empty NOW.
The Korean original and the memo text are kept with the internal documents.
