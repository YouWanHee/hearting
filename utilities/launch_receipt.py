#!/usr/bin/env python3
"""The lines every adapter wrapper's launch receipt shares.

A parent reads the same receipt whichever harness ran its child, so the
fields that describe completion delivery, the registry attempt, the launch
lifecycle and the worker's terminal result are printed here once. Each
wrapper keeps only the lines that translate its own runtime (model and
effort, permission or sandbox posture, nested homes). The OpenCode receipt
used to lack `completion_delivery`, `registry_lock`, `terminal_verdict` and
the sidecar fields because each wrapper kept its own copy (audit §4 #8, A21).
"""
from __future__ import annotations

from typing import Iterable

from parent_next_directive import receipt_lines as parent_next_receipt_lines


def completion_lines(args) -> list[str]:
    """How the parent learns this attempt finished, and the completion sidecar's state."""
    return [
        f"completion_delivery={getattr(args, 'resolved_completion_delivery', None) or '-'}",
        f"completion_delivery_reason={getattr(args, 'completion_delivery_reason', None) or 'not-applicable'}",
        f"parent_completion_delivery={getattr(args, 'parent_completion_delivery', None) or '-'}",
        f"parent_completion_reason={getattr(args, 'parent_completion_reason', None) or 'unspecified'}",
        f"parent_completion_reason_class={getattr(args, 'parent_completion_reason_class', '-')}",
        f"managed_sidecar_state={getattr(args, 'managed_sidecar_state', 'not-started')}",
        f"managed_sidecar_reason={getattr(args, 'managed_sidecar_reason', '-')}",
        f"managed_sidecar_pid={getattr(args, 'managed_sidecar_pid', '-')}",
        f"managed_sealed_batch_id={getattr(args, 'managed_sealed_batch_id', '-')}",
        f"managed_sidecar_log={getattr(args, 'managed_sidecar_log', '-')}",
    ]


def terminal_fields(terminal: dict | None) -> dict[str, str]:
    """Bounded typed terminal metadata for the launch receipt."""
    value = terminal or {
        "state": "absent",
        "source": "none",
        "verdict": "-",
        "artifact_state": "unchecked",
        "blocker_reason": "-",
    }
    artifact_state = str(value["artifact_state"])
    return {
        "handoff_state": str(value["state"]),
        "handoff_source": str(value["source"]),
        "handoff_verdict": str(value["verdict"]),
        "artifact_state": artifact_state,
        "artifact_readable": "1" if artifact_state == "readable" else "0",
        "artifact_path_b64": str(value.get("artifact_path_b64", "-")),
        "blocker_reason": str(value["blocker_reason"]),
    }


def attempt_lines(args, *, jobs, registry_source: str, action: str, launch_state: str,
                  after_lifecycle: Iterable[str] = (), before_early_death: Iterable[str] = ()) -> list[str]:
    """The registry attempt, the parent's next action, the lifecycle and the worker's result.

    `after_lifecycle` and `before_early_death` are the wrapper's own translated lines,
    kept at the place each wrapper printed them before.
    """
    lines = [
        f"job_registry={jobs}",
        "broker_lifecycle=retired",
        "governor_reservation=" + str(getattr(args, "governor_reservation", {}).get("state", "-")),
        f"registry_authority={registry_source}",
        f"preview={1 if action == 'dry-run' else 0}",
        f"attempt_id={args.attempt_id or '-'}",
        f"launch_authority={args.launch_authority}",
        f"fallback_ordinal={args.fallback_ordinal}",
        f"fallback_hop={args.fallback_hop}",
        f"execution_surface={args.execution_surface}",
        f"registered_worker={int(bool(args.registered_worker))}",
        f"registry_lock={jobs}.lock",
        f"duplicate_attempt={0 if args.attempt_claimed or action == 'dry-run' else 1}",
        f"launch_state={launch_state}",
        f"registered={1 if args.attempt_claimed else 0}",
        f"started={1 if action == 'start' and args.attempt_claimed else 0}",
    ]
    spawned_child = int(action == "start" and bool(args.attempt_claimed)
                        and bool(getattr(args, "child_pid", None)))
    lines.append(f"child_spawned={spawned_child}")
    if spawned_child:
        # The receipt states the parent's next action itself, so a parent does not
        # carry the completion-delivery taxonomy in its own instructions.
        lines += parent_next_receipt_lines(getattr(args, "parent_completion_delivery", ""),
                                           args.attempt_id, agent_home=args.agent_home)
    lines += [
        f"child_pid={getattr(args, 'child_pid', None) or '-'}",
        f"child_pid_start={getattr(args, 'child_pid_start', None) or '-'}",
        f"launch_heartbeat={getattr(args, 'launch_heartbeat', 'not-started')}",
        f"launch_lifecycle={args.launch_lifecycle}",
        f"launch_lifecycle_requested={args.launch_lifecycle_requested}",
        f"launch_lifecycle_reselection={args.launch_lifecycle_resolution.reselection}",
        f"launch_lifecycle_override={args.launch_lifecycle_resolution.override}",
        *after_lifecycle,
        f"worker_exit={getattr(args, 'worker_exit', '-')}",
        f"worker_failure={getattr(args, 'worker_failure', None) or '-'}",
        f"terminal_verdict={getattr(args, 'terminal_verdict', None) or '-'}",
        *(f"{key}={value}" for key, value in terminal_fields(getattr(args, "terminal_inspection", None)).items()),
        *before_early_death,
    ]
    early_death = getattr(args, "early_death", None)
    if early_death:
        reason, reset = early_death
        lines += [f"early_death={reason}", f"early_death_reset={reset or '-'}",
                  f"row_closed=done,note=dead-{reason}"]
    else:
        lines.append("early_death=-")
    return lines
