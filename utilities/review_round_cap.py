#!/usr/bin/env python3
"""Tier-derived review-round cap leaf (CONVENTIONS §1.1 retry budget).

Moved out of `dispatch-node.py` so `capability-route.py` (SD-116 WP4) can
reuse the same derivation for its sealed `continuation_budget.review_round_cap`
without creating an import cycle: `dispatch-node.py` already loads
`capability-route.py`, so `capability-route.py` importing `dispatch-node.py`
back would cycle. Both now import this leaf instead (route_identity.py /
dispatch_launch_tuple.py precedent -- one definition, no duplication).

SD-153 adds the one shared admission decision (`round_budget`/`RoundBudget`)
that every registered launch surface (dispatch-node, dispatch-batch,
stage-dispatch-fallback) and `capability-route.py`'s owner-closure eligibility
now delegate to, instead of each keeping its own `len(prior)+1 > max_round`
comparison. A round with no verdict (a crashed/capacity-dead attempt, not a
PASS/FAIL/blocking review) does not spend the exhaustion budget by itself --
two of them in a row bind the node to the unregistered fallback chain instead
(`VERDICTLESS_BOUND`), which is a distinct outcome from "the correction budget
ran out on real verdicts"."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from dispatch_attempt_policy import (
    OPEN_STATES,
    REVIEW_BLOCKING_NOTE,
    SUCCESS_NOTES,
    deferred_completion,
    terminal_conflict_pending,
)

# SD-153 rule 3: two rounds in a row with no verdict at all bind the node to
# the sealed fallback chain's remaining unregistered hop rather than spending
# a third registered round on the same kind of silence.
VERDICTLESS_BOUND = 2


def recovery_fields(node_kind, state="exhausted"):
    """A budget ends automatic review, not ownership of the findings.

    `state` selects between the two distinct outcomes `round_budget` can stop
    on: `"exhausted"` (the historical exhaustion guidance, unchanged) and
    `"verdictless-bound"` (SD-153 rule 3 -- two silent rounds in a row, so the
    next action is a hop already named in the sealed fallback chain, not a
    registered retry).
    """
    if state == "verdictless-bound":
        if node_kind == "review-worker":
            return {
                "next_action": "native-subagent",
                "recovery_owner": "owner",
                "recovery_surface": "capability-route complete",
                "recovery_hint": "Two rounds in a row ended with no verdict (a crashed or capacity-dead "
                    "attempt, not a PASS/FAIL/blocking review) -- a third registered dispatch would only "
                    "repeat the same silence. Route the review through the sealed fallback chain's remaining "
                    "unregistered hop (a native subagent, then owner-inline) and record the outcome as "
                    "evidence-bound owner-closure.",
            }
        return {
            "next_action": "owner-inline",
            "recovery_owner": "owner",
            "recovery_surface": "capability-route continue",
            "recovery_hint": "Two rounds in a row ended with no verdict. Hand the remaining verification to "
                "the owner-inline fallback hop instead of spending a third registered round on the same "
                "silence; preserve the exact failing evidence for that hop.",
        }
    if node_kind == "review-worker":
        return {"required_action": "resolve-review-findings", "recovery_owner": "owner",
                "recovery_surface": "capability-route complete",
                "recovery_hint": "Read the blocking reviews and the corrected artifact. Record each finding's "
                    "resolution in an evidence-bound .owner-closure.md and use the existing owner-closure "
                    "completion path. This records owner judgment, not independent PASS. If findings remain "
                    "unresolved, hand them to the parent with the existing artifacts; a fresh route is not required."}
    return {"required_action": "report-unresolved-verification", "recovery_owner": "owner",
            "recovery_surface": "capability-route continue",
            "recovery_hint": "Preserve the completed stages and exact failing verification evidence. "
                "Hand back the remaining verification for an explicit continuation; do not infer PASS from a corrected plan."}


def max_review_rounds(effective_intensity):
    """Tier-derived max round count for a capped anchor.

    `direct`/`quick` run no automatic correction round (max 1: the first pass
    only, matching the table's "One pass"/"None automatically"). `standard`/`strong`
    get one correction (max 2). `thorough`/`adversarial` get two, including the
    adversary pass that is not itself a correction (max 3). This is deliberately a
    per-tier derivation, not a hardcoded `cap=2` -- a tier change moves the cap with it.
    """
    if effective_intensity in ("direct", "quick"):
        return 1
    if effective_intensity in ("standard", "strong"):
        return 2
    if effective_intensity in ("thorough", "adversarial"):
        return 3
    raise ValueError(f"unknown effective_intensity for review round cap: {effective_intensity}")


def classify_round_row(status, metadata, *, worker_type):
    """One round's outcome, in positive-definition order.

    Order: live, then unsettled, then verdict, then everything else is
    verdict-less. A row is `"verdict"` only when it actually says something
    about the work: a recognized success note, a blocking review, or (for a
    non-review worker only) an explicit FAIL. A review worker that crashed,
    ran out of capacity, or died with an invalid envelope never produced a
    review verdict -- it is `"verdict-less"` even though the row is `done`,
    so it does not spend the exhaustion budget by itself (SD-153 rule 1);
    two of them in a row still bind the node via `VERDICTLESS_BOUND` (rule 3).
    """
    if status in OPEN_STATES:
        return "live"
    if deferred_completion(metadata) == "pending" or terminal_conflict_pending(metadata):
        return "unsettled"
    note = metadata.get("note", "")
    if status == "done" and (
        note in SUCCESS_NOTES
        or note == REVIEW_BLOCKING_NOTE
        or (worker_type != "review" and note == "dead-worker-fail" and metadata.get("failure_class") == "fail")
    ):
        return "verdict"
    return "verdict-less"


@dataclass(frozen=True)
class RoundBudget:
    """The one admission decision every registered launch surface reads.

    `state` is the caller's dispatch decision: `"admit"` launches, the two
    `"blocked-*"` states refuse for a reason that already exists elsewhere (a
    still-live sibling, or a row whose outcome is not yet settled) and carry
    no cap semantics of their own, `"exhausted"` is the historical
    correction-budget-spent refusal, and `"verdictless-bound"` is SD-153 rule
    3 -- a distinct outcome that hands the node to the sealed fallback
    chain's remaining unregistered hop instead of a third silent registered
    round.
    """
    verdict_rounds: int
    verdictless_streak: int
    verdictless_rounds: int
    cap: int
    state: str
    next_round: int
    correction_round: int
    round_kind: str
    next_action: str
    closure_check_used: bool


_NEXT_ACTION_BY_STATE = {
    "admit": "dispatch",
    "blocked-live": "wait",
    "blocked-unsettled": "wait",
    "exhausted": "owner-closure",
    "verdictless-bound": "native-subagent",
}


def round_budget(route, node, rows: Sequence[tuple[str, Mapping]], *, revisions=()):
    """The shared launch-admission decision for one route node's round history.

    `rows` is the exact registry census for the node as `(status, metadata)`
    pairs, oldest first -- the shape `capability-route.py`'s
    `_review_round_rows` already returns, and dispatch-node.py's
    `(cols, metadata)` census reduces to via `cols[1]`. `revisions` is the
    node's SD-154 revision history (empty until `capability-route.py` grows
    `publish_revision_locked` -- P3 always passes `()`, so `round_kind` never
    resolves to `"closure-check"` from a P3 call site); once a caller
    supplies it, a node whose last verdict is a blocking FAIL and whose
    revision names that exact row as its answer gets one `closure-check`
    round instead of an ordinary correction.
    """
    continuation_budget = route.get("continuation_budget") or {}
    cap = continuation_budget.get("review_round_cap")
    if cap is None:
        cap = max_review_rounds(route["effective_intensity"])
    worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
    kinds = [
        classify_round_row(status, meta, worker_type=meta.get("worker_type") or worker_type)
        for status, meta in rows
    ]
    verdict_rounds = kinds.count("verdict")
    verdictless_rounds = kinds.count("verdict-less")
    verdictless_streak = 0
    for kind in reversed(kinds):
        if kind != "verdict-less":
            break
        verdictless_streak += 1
    if "live" in kinds:
        state = "blocked-live"
    elif "unsettled" in kinds:
        state = "blocked-unsettled"
    elif verdictless_streak >= VERDICTLESS_BOUND:
        state = "verdictless-bound"
    elif verdict_rounds >= cap:
        state = "exhausted"
    else:
        state = "admit"
    next_round = len(rows) + 1
    correction_round = next_round if next_round >= 2 else 0
    round_kind = "first" if next_round < 2 else "correction"
    closure_check_used = False
    # SD-154 rule 7: after the budget is spent (`state == "exhausted"`), a
    # node whose last verdict round was a blocking FAIL and whose FAIL a
    # revision names as its `answers` gets exactly one extra admitted round,
    # `round_kind="closure-check"` -- eligibility is spent by that verdict
    # attempt itself (the NEXT verdict round, if any, needs its OWN revision
    # naming it), so this needs no separate "already used" bookkeeping.
    # `state == "admit"` (budget still open) also gets the label when the
    # same condition holds, matching P3's original forward-looking behavior.
    if state in ("admit", "exhausted") and revisions and kinds and kinds[-1] == "verdict":
        _last_status, last_meta = rows[-1]
        last_note = last_meta.get("note", "")
        last_blocking = last_note == REVIEW_BLOCKING_NOTE or (
            last_note == "dead-worker-fail" and last_meta.get("failure_class") == "fail"
        )
        last_attempt = last_meta.get("attempt_id")
        if last_blocking and last_attempt and any(
            last_attempt in (revision.get("answers") or ()) for revision in revisions
        ):
            state = "admit"
            round_kind = "closure-check"
            closure_check_used = True
    return RoundBudget(
        verdict_rounds=verdict_rounds,
        verdictless_streak=verdictless_streak,
        verdictless_rounds=verdictless_rounds,
        cap=cap,
        state=state,
        next_round=next_round,
        correction_round=correction_round,
        round_kind=round_kind,
        next_action=_NEXT_ACTION_BY_STATE[state],
        closure_check_used=closure_check_used,
    )
