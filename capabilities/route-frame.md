# Capability: route-frame

This is the portable contract for `route-frame`, the compiler-internal capability behind the framed route shape. It is not a Claude Skill file, not a native command, and not an owner persona: no person and no model invokes it by name. It is reached only through `capability-route.py compose --shape framed`.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `route-frame` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Compiler-internal framed front end: two top-tier frame legs and a model-less runtime terminal that fixes the route decision. |
| Argument shape | `compose --shape framed --campaign-key <key> --start --prompt-file <task>` |

## Shape

A framed route is sealed at intensity `standard` with exactly three nodes:

| Node | Kind | Runs as | Model |
|---|---|---|---|
| `frame` | map-worker, unit `plan/frame` | registered depth-1 attempt, launched by the depth-0 session | `top` |
| `frame-alternative` | map-worker, unit `plan/frame` | a separate registered depth-1 attempt, never a batch with its sibling | `top` |
| `route-decision` | runtime-terminal | the runtime itself; no attempt, no unit, no role | none |

The two legs never read each other's brief. The terminal depends on both and carries the one `frame-review` human gate at its entry. There is no owner node and no owner launch.

## What the runtime does

1. `start` launches the two legs as separate registrations and joins them.
2. The existing single frame interview (`needs-interview`, then `needs-question`) asks the person once.
3. On `proceed` the runtime fixes one `route_decision_v1` record, completes the terminal with that record as its evidence, closes the route and finalizes the producer cycle.
4. `revise` and `stop` behave as they do for every other frame route.

The record has an immutable `decision` part with its canonical digest and a separate, monotonic `first_leg` part. The record is the terminal's completion evidence; it adds no approval, no required input and no recovery command.

## Hints

`--capability`, `--capability-mode`, `--graph` and `--profile` given with `--shape framed` are recorded as `routing_hints` in the work request. They are never sealed as the route's capability and never refused as an argument conflict. `--pin`, `--campaign-key`, `--parent-cycle` and `--unassigned` apply as for any route.

## Completion

The `route-decision` completion gate is the existing `custom` contract row: the runtime terminal marker over the fixed `route_decision_v1` record. It is accepted only by the exact-shape check for this capability; no other capability may declare a runtime terminal.

## Artifact Ownership

The record is written under the producer cycle as `shards/frame/route-decision.json`. Frame briefs are `shards/frame/direction-brief.md` and `shards/frame-alternative/direction-brief.md`. Durable output stays under the canonical artifact root.
