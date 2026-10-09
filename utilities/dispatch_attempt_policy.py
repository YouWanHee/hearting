"""Portable decisions over a committed attempt and its execution evidence.

This module owns no processes or storage. Callers provide the exact registry
snapshot and the shared process proof; runtime labels never affect the policy.
The semantic result is kept separate from the action still owed. In particular,
success with incomplete cleanup remains success with a cleanup obligation.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from typing import Mapping

OPEN_STATES = frozenset({"open", "running"})
TERMINAL_STATES = frozenset({"done", "killed", "cancelled"})
SUBSESSION_NOTE = "completed-subsession"
REVIEW_BLOCKING_NOTE = "completed-review-blocking"
SUCCESS_NOTES = frozenset({"completed-marker", "completed-supervisor", SUBSESSION_NOTE})

# A completion-budget row closed typed-deferred (not `dead-*`, not a plain
# success note) because the transport that would have proved success timed
# out repeatedly, never because the work itself failed. `SUCCESS_NOTES`
# deliberately excludes this note: without a published marker the row is not
# yet a semantic success, so every consumer must go through
# `deferred_completion`/`verdict_pass`/`success_note` instead of reading
# `note`/`failure_class` directly.
DEFERRED_COMPLETION_NOTE = "completion-deferred"
DEFERRED_COMPLETION_SOURCE = "registered-wrapper-completion-transient-v1"


def terminal_conflict_identity(metadata: Mapping[str, str]) -> str:
    keys = ("note", "failure_class", "classifier_source", "conflicting_terminal_note",
            "conflicting_failure_class", "conflicting_classifier_source")
    value = {key: metadata.get(key, "") for key in keys}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def terminal_conflicts(metadata: Mapping[str, str]) -> dict:
    """All contradictory observations and their exact review dispositions.

    Keeping only the latest observation loses earlier unresolved evidence,
    and keeping only the latest review reopens old, already reviewed evidence.
    Both belong to the same attempt-owned record.
    """
    encoded = metadata.get("terminal_conflicts_b64", "")
    if encoded:
        value = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(value, dict) or not value or any(
            not isinstance(key, str) or not isinstance(entry, dict)
            for key, entry in value.items()
        ):
            raise ValueError("terminal-conflicts-invalid")
        return value
    if metadata.get("terminal_conflict") == "1":
        return {terminal_conflict_identity(metadata): {
            "note": metadata.get("conflicting_terminal_note", ""),
            "failure_class": metadata.get("conflicting_failure_class", ""),
            "classifier_source": metadata.get("conflicting_classifier_source", ""),
        }}
    return {}


def terminal_conflict_digest(metadata: Mapping[str, str]) -> str:
    pending = sorted(key for key, entry in terminal_conflicts(metadata).items()
                     if not entry.get("review_sha256"))
    return hashlib.sha256(json.dumps(pending).encode()).hexdigest()


def terminal_conflict_pending(metadata: Mapping[str, str]) -> bool:
    return any(not entry.get("review_sha256") for entry in terminal_conflicts(metadata).values())


def deferred_completion(metadata: Mapping[str, str]) -> str:
    """Whether this row was closed by the completion budget's own deferral.

    ``""`` when the row was never deferred (an ordinary terminal writer
    closed it). ``"pending"`` once the budget closed it typed-deferred but no
    marker has been published for it yet. ``"completed"`` once the marker
    exists -- the only state in which a deferred row is a semantic success.
    This is the one place that reads ``classifier_source``/``completion_marker``
    to answer that question; every other consumer goes through this function
    (or `verdict_pass`/`success_note`, which call it) instead of re-deriving it.
    """
    if metadata.get("classifier_source") != DEFERRED_COMPLETION_SOURCE:
        return ""
    return "completed" if metadata.get("completion_marker") else "pending"


def verdict_pass(metadata: Mapping[str, str]) -> bool:
    """The one place a caller asks "did this attempt succeed"."""
    state = deferred_completion(metadata)
    if state:
        return state == "completed"
    return metadata.get("failure_class") == "pass"


def readable_result(metadata: Mapping[str, str]) -> str:
    """``"FAIL"`` or ``"BLOCKED"`` when the worker's own final envelope said so, else ``""``.

    That is the worker's result for its owner to act on, never a transport
    death: only a death, a capacity stop or a runtime error is retried
    automatically (ROUTE-AUTHORITY RA-4). A reviewer's finished FAIL is
    `completed-review-blocking` (its readable review artifact); a bare
    `dead-worker-fail` on a review row stays a death, as the round census reads it.
    """
    note, failure_class = metadata.get("note", ""), metadata.get("failure_class")
    if note == "dead-worker-fail" and failure_class == "fail" and metadata.get("worker_type") != "review":
        return "FAIL"
    if note == "dead-worker-blocked" and failure_class == "blocked":
        return "BLOCKED"
    return ""


def success_note(metadata: Mapping[str, str]) -> bool:
    """The one place a caller asks "does this row's note read as success"."""
    state = deferred_completion(metadata)
    if state:
        return state == "completed"
    return metadata.get("note", "") in SUCCESS_NOTES


def committed_outcome(status: str, metadata: Mapping[str, str]) -> str:
    """Interpret only a committed terminal row, never cached watchdog output."""
    if status in OPEN_STATES:
        return "pending"
    if status not in TERMINAL_STATES:
        return "unknown"
    note = metadata.get("note", "")
    if note == "cancelled-by-parent" and metadata.get("failure_class") == "cancelled":
        return "cancelled"
    if note == REVIEW_BLOCKING_NOTE:
        return "review-blocked"
    if deferred_completion(metadata) == "pending":
        return DEFERRED_COMPLETION_NOTE
    if success_note(metadata) or verdict_pass(metadata):
        return "succeeded"
    if note.startswith("dead-") or status in {"killed", "cancelled"}:
        return "failed"
    return "unknown"


@dataclass(frozen=True)
class AttemptDecision:
    outcome: str
    action: str
    responsible: str
    reason: str
    retry_kind: str = ""

    @property
    def retry_allowed(self) -> bool:
        return bool(self.retry_kind) and self.action == "inspect-failure"


def decide_attempt(
    status: str, metadata: Mapping[str, str], *, process_state: str,
    process_reason: str = "", terminal_observed: bool = False,
    result_state: str | None = None,
) -> AttemptDecision:
    """Select the next obligation without changing a committed outcome.

    A quiescent process with an open row needs the terminal writer. A process
    that cannot be observed needs recovery or parent intervention, never a
    fabricated terminal result. Only a closed failure plus quiescence may
    authorize fallback; a completed review with findings remains a review.
    """
    outcome = committed_outcome(status, metadata)
    if status not in OPEN_STATES | TERMINAL_STATES:
        return AttemptDecision(outcome, "recover", "supervision-controller", "registry-status-invalid")
    if metadata.get("parent_close_requested") == "1":
        return AttemptDecision(outcome, "settle-cancellation" if outcome == "cancelled" else "cancel",
                               "execution-boundary", "cancelled-by-parent")
    if process_state == "live":
        return AttemptDecision(outcome, "wait", "execution-boundary", process_reason or "process-alive")
    if process_state != "quiescent":
        return AttemptDecision(outcome, "recover", "supervision-controller", process_reason or "process-unverifiable")
    if status in OPEN_STATES:
        if result_state == "unverifiable":
            return AttemptDecision(outcome, "recover", "supervision-controller", "terminal-evidence-unverifiable")
        return AttemptDecision(outcome, "reconcile", "terminal-writer",
                               "terminal-observed" if result_state == "settleable" or terminal_observed else
                               "terminal-invalid" if result_state == "invalid" else "process-exited")
    if terminal_conflict_pending(metadata):
        return AttemptDecision(outcome, "inspect-conflict", "workflow-owner", "terminal-evidence-conflict")
    if outcome == "succeeded":
        return AttemptDecision(outcome, "advance", "completion-controller", "registry-closed")
    if outcome == DEFERRED_COMPLETION_NOTE:
        # The transport, not the work, is what is unresolved -- no fallback
        # retry is authorized, only the completion writer that already owns
        # this row's marker publication.
        return AttemptDecision(outcome, "complete", "completion-controller", DEFERRED_COMPLETION_NOTE)
    if outcome == "review-blocked":
        return AttemptDecision(outcome, "review", "workflow-owner", REVIEW_BLOCKING_NOTE)
    if outcome == "failed":
        note = metadata.get("note", "")
        # Cancellation is an explicit disposition, not automatic permission
        # to resurrect the cancelled work. Its recovery claim has a separate
        # user/route-authorized admission boundary.
        # A worker's readable FAIL or BLOCKED is its result, not a death to retry.
        retry = ("" if status == "cancelled" or metadata.get("failure_class") == "cancelled" else
                 "capacity" if note == "dead-capacity" else
                 "" if readable_result(metadata) else
                 "fallback" if note.startswith("dead-") else "")
        return AttemptDecision(outcome, "inspect-failure", "workflow-owner", note or status, retry)
    return AttemptDecision(outcome, "inspect-failure", "workflow-owner", "terminal-outcome-unclassified")


def required_action(status: str, metadata: Mapping[str, str]) -> str:
    """The terminal writer/harvest instruction, before transport formatting."""
    if committed_outcome(status, metadata) == "cancelled":
        return "advance-completed"
    if status in OPEN_STATES:
        return "complete-open"
    if terminal_conflict_pending(metadata):
        return "inspect-done-failure"
    outcome = committed_outcome(status, metadata)
    if outcome == "succeeded":
        return "advance-completed"
    if outcome == DEFERRED_COMPLETION_NOTE:
        return "complete-deferred"
    return "inspect-done-failure"
