#!/usr/bin/env python3
"""What a wrapper does once its foreground worker process has exited.

Every adapter wrapper waits for its worker in the foreground lifecycle and
then has to decide, from the worker's own log, whether the attempt ended
with a verdict that closes its registry row now. That decision was written
out in the Claude and Codex wrappers and missing in the OpenCode one, which
closed a row only when the process itself failed -- a clean exit that
reported FAIL, BLOCKED or a malformed envelope stayed open until a later
reap or reconcile.

`settle_foreground_exit` is the one decision. The log is read by
`inspect_terminal_attempt`, which already understands each harness's native
turn boundary (Codex `turn.completed`, Claude `result`, OpenCode
`step_finish` stop); that parsing is the translation. The rules here are
shared:

- a valid envelope's own failure note closes the row (FAIL, BLOCKED, a
  completed blocking review);
- an actual process failure overrides the envelope (SD-72: final text is a
  semantic observation, not an exit receipt);
- a PASS writes no close here: the join, reap watch or supervisor settles it;
- when the typed close does not land after a process failure, the wrapper's
  legacy close runs.
"""
from __future__ import annotations

from typing import Callable

from codex_dispatch_terminal import ARTIFACT_HINT_KEYS, REVIEW_BLOCKING_NOTE, inspect_terminal_attempt
from dispatch_contract import close_attempt_row


def terminal_evidence(terminal: dict, terminal_note: str, log_path, outcome=None) -> dict[str, str]:
    """Evidence sealed on the row by the foreground tail close.

    A finished review (`completed-review-blocking`) also seals the in-root
    artifact it named, exactly like the supervisor join does, so the
    owner-closure gate can re-verify it from the row.
    """
    evidence = {
        "detected_by": "foreground-terminal-handoff",
        "failure_class": terminal.get("failure_class", "runtime"),
        "terminal_event": terminal.get("terminal_event", "-"),
        "log_file": str(log_path),
    }
    if outcome is not None:
        evidence["process_exit"] = str(outcome.exit_code)
        if outcome.failure:
            evidence.update(
                detected_by="foreground-process-exit",
                failure_class="runtime",
                reconcile_reason=outcome.failure,
            )
    for key in ARTIFACT_HINT_KEYS:
        if terminal.get(key):
            evidence[key] = str(terminal[key])
    if terminal_note == REVIEW_BLOCKING_NOTE and terminal.get("artifact_path_b64"):
        evidence["review_artifact_b64"] = str(terminal["artifact_path_b64"])
    return evidence


def settle_foreground_exit(jobs, attempt_id: str | None, log_path, outcome, *, worktree, artifact_root,
                           worker_type, legacy_close: Callable[[str], object]) -> dict:
    """Read the exited worker's log, close its row when the result says so.

    Returns `{inspection, verdict, note, closed, worker_failure}`: the
    inspector's record, the envelope verdict when the envelope is valid, the
    close note (empty when nothing closes here), whether the typed close
    landed, and the failure the wrapper reports.
    """
    from dispatch_completion_join import materialize_after_terminal_close

    terminal = inspect_terminal_attempt(log_path, worktree=worktree,
                                        artifact_root_metadata=artifact_root, worker_type=worker_type)
    valid = terminal.get("state") == "valid"
    note = terminal.get("failure_note", "") if valid else ""
    if outcome.failure:
        note = f"dead-{outcome.failure}"
    closed = False
    if note and attempt_id:
        closed = bool(close_attempt_row(jobs, attempt_id, note,
                                        evidence=terminal_evidence(terminal, note, log_path, outcome)))
        if closed:
            materialize_after_terminal_close(jobs, attempt_id)
    if outcome.failure and not closed:
        legacy_close(outcome.failure)
    return {"inspection": terminal, "verdict": terminal.get("verdict") if valid else None,
            "note": note, "closed": closed,
            "worker_failure": (outcome.failure or note) if note else outcome.failure}
