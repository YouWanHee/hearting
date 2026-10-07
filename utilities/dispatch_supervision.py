"""Shared wait ownership and durable, non-terminal parent handback.

The canonical registry owns outcomes. This controller schedules bounded recovery
and keeps waiting; its timeout never authorizes killing a worker or retrying it.
The existing pending-delivery queue owns a notice until a carrier accepts it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
from typing import Callable

from dispatch_attempt_policy import decide_attempt, terminal_conflict_digest
from dispatch_receipt_identity import receipt_digest
from dispatch_registry_cache import registry_lines
import dispatch_pending_delivery as pending_delivery

KIND = "supervision"
REASONS = frozenset({"owner-input-undelivered", "no-progress", "process-unverifiable", "join-deadline", "supervisor-exited", "join-observer-failed", "terminal-evidence-conflict", "workflow-completion-pending", "closure-blocked", "watch-deadline", "receiver-unavailable", "answer-awaiting-parent"})
# Another session answered a BLOCKED owner; only the route's parent launches the continuation.
ANSWER_AWAITING_PARENT = "answer-awaiting-parent"


class SupervisionError(ValueError):
    pass


def join_process_error(returncode: int, receipt: object, parent_attempt_id: str) -> str:
    """Preserve the join protocol's typed failure, never arbitrary child output."""
    reason = "unverified-error-receipt"
    if (isinstance(receipt, dict) and receipt.get("schema_version") == 2
            and receipt.get("state") == "contract-error"
            and receipt.get("parent_attempt_id") == parent_attempt_id
            and receipt.get("children") == []):
        candidate = receipt.get("reason")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", candidate):
            reason = candidate
    return f"join-process-contract-failed:exit={returncode}:reason={reason}"


def _rows(jobs: Path) -> dict:
    from dispatch_contract import parse_registry_metadata
    rows = {}
    for line in registry_lines(jobs):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        aid = meta.get("attempt_id")
        if aid:
            if aid in rows:
                raise SupervisionError("supervision-attempt-ambiguous")
            rows[aid] = (fields[1], meta)
    return rows


def _root(rows: dict, attempt: str) -> str:
    seen = set()
    while attempt in rows and attempt not in seen:
        seen.add(attempt)
        meta = rows[attempt][1]
        parent = meta.get("parent_attempt_id")
        if parent in (None, "", "-"):
            return attempt
        attempt = parent
    raise SupervisionError("supervision-lineage-unresolved")


def _awaiting_answers(jobs: Path, aid: str) -> list[str]:
    """The kept answers of a BLOCKED owner whose continuation has not launched yet."""
    from dispatch_owner_input import blocked_owner_answers
    from dispatch_replacement import _replacement_in_flight, _rows as replacement_rows
    answers = blocked_owner_answers(jobs, aid)
    if not answers:
        return []
    rows = replacement_rows(Path(jobs).read_text(encoding="utf-8").splitlines())
    if aid in rows and _replacement_in_flight(jobs, rows, rows[aid][1]):
        return []
    return [item["id"] for item in answers]


def _pending(rows: dict, attempts: list[str], jobs: Path, reason=None) -> bool:
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved
        return any(unresolved(jobs, aid) for aid in attempts)
    if reason == ANSWER_AWAITING_PARENT:
        return any(_awaiting_answers(jobs, aid) for aid in attempts)
    from dispatch_contract import observed_attempt_liveness
    from codex_dispatch_terminal import terminal_envelope_observed
    for aid in attempts:
        if aid not in rows:
            raise SupervisionError("supervision-attempt-missing")
        status, meta = rows[aid]
        # Route settlement can retire its completion wait while process cleanup
        # remains an independent obligation. Never let route close hide a live,
        # unknown, or conflicting execution.
        from dispatch_notice_state import route_obligation_closed
        route_closed = reason == "supervisor-exited" and route_obligation_closed(meta, jobs)
        from dispatch_contract import observed_attempt_liveness
        from codex_dispatch_terminal import terminal_envelope_observed
        proof = observed_attempt_liveness(status, meta,
            terminal_envelope=terminal_envelope_observed(meta.get("log_file")),
            terminal_receipt_gate=True)
        decision = decide_attempt(status, meta, process_state=proof.process_state,
                                  process_reason=proof.process_reason)
        if decision.action in {"wait", "recover", "reconcile", "inspect-conflict"}:
            return True
        # A supervisor-exited notice may be only a lagging route-settlement
        # signal. Once that exact route is closed, do not keep it alive solely
        # because the workflow ledger is still flushing; closure-blocked remains
        # a distinct reason and does not take this branch.
        if meta.get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import owner_completion_pending
            if not route_closed and owner_completion_pending(jobs, status, meta):
                return True
    return False


def _obligation_revision(rows: dict, attempts: list[str], reason: str, jobs=None) -> str:
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved_revision
        return hashlib.sha256(json.dumps([(aid, unresolved_revision(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason == ANSWER_AWAITING_PARENT:
        return hashlib.sha256(json.dumps([(aid, _awaiting_answers(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason != "terminal-evidence-conflict":
        return ""
    value = [(aid, terminal_conflict_digest(rows[aid][1])) for aid in attempts]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def materialize(jobs: Path, attempts: set[str], *, reason: str) -> list[dict]:
    """Idempotent handback per exact root/batch/reason, with no row mutation."""
    if reason not in REASONS:
        raise SupervisionError("supervision-reason-invalid")
    jobs = jobs.resolve()
    rows = _rows(jobs)
    groups: dict[str, list[str]] = {}
    for aid in sorted(attempts):
        groups.setdefault(_root(rows, aid), []).append(aid)
    results = []
    for owner, monitored in groups.items():
        meta = rows[owner][1]
        recipient = meta.get("parent_sid", "")
        kind = meta.get("parent_completion_delivery", "")
        if not recipient or kind not in pending_delivery.RECIPIENT_KINDS:
            raise SupervisionError("supervision-parent-carrier-unbound")
        receipt = {
            "schema_version": 1, "kind": KIND, "state": "attention",
            "owner_attempt_id": owner, "monitored_attempt_ids": monitored,
            "recipient_thread_id": recipient,
            "sealed_batch_id": meta.get("managed_sealed_batch_id", ""),
            "job_registry": str(jobs), "reason": reason,
            "obligation_revision": _obligation_revision(rows, monitored, reason, jobs),
            "responsible": "supervision-controller", "required_action": "inspect-recovery",
        }
        key = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        delivery = "delivery-" + key
        receipt["pending_delivery_id"] = delivery
        record = pending_delivery.create(jobs.parent, delivery_id=delivery,
            recipient_kind=kind, recipient_key=recipient,
            # The work obligation outlives gateway connections. The courier
            # proves the current recipient generation when claiming it.
            session_generation="", session_generation_supported="0",
            attempt_ids=[owner], parent_attempt_id=owner,
            route_id=meta.get("owner_route_id") or meta.get("route_id") or "route-free",
            route_node=meta.get("route_node") or "supervision",
            receipt=receipt, receipt_digest=receipt_digest(receipt),
            row_revisions={owner: "supervision:" + key})
        results.append(record)
    return results


def validate(receipt: dict, *, expected_thread: str | None = None,
             expected_epoch: int | None = None) -> dict:
    keys = {"schema_version", "kind", "state", "owner_attempt_id", "monitored_attempt_ids",
            "recipient_thread_id", "sealed_batch_id", "job_registry",
            "reason", "responsible", "required_action", "pending_delivery_id", "obligation_revision"}
    if (not isinstance(receipt, dict) or set(receipt) != keys
            or receipt.get("kind") != KIND or receipt.get("schema_version") != 1
            or receipt.get("state") != "attention" or receipt.get("reason") not in REASONS
            or receipt.get("required_action") != "inspect-recovery"
            or receipt.get("responsible") != "supervision-controller"):
        raise SupervisionError("supervision-shape-invalid")
    text_keys = keys - {"schema_version", "monitored_attempt_ids"}
    if (any(not isinstance(receipt[key], str) for key in text_keys)
            or not Path(receipt["job_registry"]).is_absolute()
            or any(ord(char) < 32 for key in text_keys for char in receipt[key])):
        raise SupervisionError("supervision-field-invalid")
    attempts = receipt["monitored_attempt_ids"]
    if (not isinstance(attempts, list) or not attempts
            or any(not isinstance(a, str) or not re.fullmatch(r"att-[A-Za-z0-9._-]+", a)
                   for a in [*attempts, receipt["owner_attempt_id"]])
            or attempts != sorted(set(attempts))):
        raise SupervisionError("supervision-attempts-invalid")
    if expected_thread is not None and receipt["recipient_thread_id"] != expected_thread:
        raise SupervisionError("supervision-thread-mismatch")
    # Unlike an approval gate, recovery is attempt-scoped, not tied to a
    # particular connection. The common transport fences its live epoch.
    return dict(receipt)


def validate_digest(receipt: dict, supplied: str) -> None:
    if receipt_digest(receipt) != supplied:
        raise SupervisionError("supervision-digest-mismatch")


def validate_receipt(receipt: dict, *, jobs: Path, expected_thread_id: str,
                     expected_epoch: int, expected_attempts: set[str],
                     expected_sealed_batch_id: str, validate_live: bool = True) -> dict:
    receipt = validate(receipt, expected_thread=expected_thread_id, expected_epoch=expected_epoch)
    if (not jobs.is_absolute() or jobs.is_symlink() or str(jobs) != receipt["job_registry"]
            or receipt["owner_attempt_id"] not in expected_attempts
            or receipt["sealed_batch_id"] != expected_sealed_batch_id):
        raise SupervisionError("supervision-binding-mismatch")
    rows = _rows(jobs)
    owner = receipt["owner_attempt_id"]
    if owner not in rows:
        raise SupervisionError("supervision-owner-missing")
    meta = rows[owner][1]
    if (meta.get("parent_sid") != expected_thread_id
            or meta.get("managed_sealed_batch_id", "") != expected_sealed_batch_id
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    # A recovered batch must not receive an obsolete intervention request.
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        raise SupervisionError("supervision-resolved")
    if validate_live and not _pending(rows, receipt["monitored_attempt_ids"], jobs, receipt["reason"]):
        raise SupervisionError("supervision-resolved")
    record = pending_delivery.read(jobs.parent, expected_thread_id, receipt["pending_delivery_id"])
    if record is None or record.get("receipt") != receipt:
        raise SupervisionError("supervision-authority-missing")
    return receipt


def validate_pending_record(record: dict, **kwargs) -> dict:
    receipt = validate_receipt(record.get("receipt"), **kwargs)
    if (record.get("delivery_id") != receipt["pending_delivery_id"]
            or record.get("attempt_ids") != [receipt["owner_attempt_id"]]
            or record.get("parent_attempt_id") != receipt["owner_attempt_id"]
            or record.get("recipient_digest") != pending_delivery.recipient_digest(receipt["recipient_thread_id"])
            or record.get("receipt_digest") != receipt_digest(receipt)):
        raise SupervisionError("supervision-pending-mismatch")
    return dict(record)


def gateway_delivery_id(receipt: dict) -> str:
    return "sn-dlv-" + receipt_digest(validate(receipt)).removeprefix("sha256:")


def notice_is_current(record: dict) -> bool:
    """Shared pre-delivery check for native prompt and async carriers."""
    receipt = validate(record.get("receipt"))
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise SupervisionError("supervision-digest-mismatch")
    rows = _rows(Path(receipt["job_registry"]))
    owner = receipt["owner_attempt_id"]
    if (owner not in rows or rows[owner][1].get("parent_sid") != receipt["recipient_thread_id"]
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        return False
    return _pending(rows, receipt["monitored_attempt_ids"], Path(receipt["job_registry"]), receipt["reason"])


def _residue_text(receipt: dict) -> str:
    """Name survivors a finished worker left running; display only, empty when none."""
    try:
        from dispatch_contract import residue_live_pids
        rows = _rows(Path(receipt["job_registry"]))
    except (OSError, ValueError, SupervisionError):
        return ""
    parts = []
    for aid in receipt["monitored_attempt_ids"]:
        pids = residue_live_pids(rows[aid][1]) if aid in rows else ()
        if pids:
            parts.append(f"{aid} finished, but the worker left process pid "
                         f"{', '.join(map(str, pids[:4]))} running")
    if not parts:
        return ""
    return (" " + "; ".join(parts) + ". The row closes by itself once that process exits; "
            "stop it if it is no longer needed. Nothing is signalled automatically.")


def _printed(name: str) -> str:
    from parent_next_directive import entrypoint
    return entrypoint(Path(__file__).resolve().parents[1], f"utilities/{name}")


def _resume_text(receipt: dict) -> str:
    """The route's start command for the notice's owner, or "" when its route cannot be read."""
    try:
        from dispatch_replacement import _route
        from parent_next_directive import resume_command
        rows = _rows(Path(receipt["job_registry"]))
        path, _route_doc = _route(Path(receipt["job_registry"]), receipt["owner_attempt_id"],
                                  rows[receipt["owner_attempt_id"]][1])
        return resume_command(path, receipt["job_registry"], agent_home=Path(__file__).resolve().parents[1])
    except Exception:  # noqa: BLE001 -- the notice still names the owner
        return ""


# Set on a delivered record (not its receipt) when its carrier started the continuation.
CONTINUED_KEY = "parent_continuation"


def continue_for_parent(record: dict, *, session_id: str, recipient_kind: str,
                        environ=None, arm=None) -> dict | None:
    """Start an answered BLOCKED owner's continuation from the carrier that hands its parent the notice.

    Only the route's parent launches it (`route_authority.require_replacement_parent`). The carrier
    that kept its claim on an `answer-awaiting-parent` record for `session_id` runs for that session,
    so it arms the route's `start` once under this session's own identity (`capacity_auto_resume`)
    instead of asking the parent to run it. None when the record is another notice or nothing could
    be armed; the notice then keeps its start command."""
    receipt = record.get("receipt") if isinstance(record, dict) else None
    if (not session_id or not isinstance(receipt, dict) or receipt.get("kind") != KIND
            or receipt.get("reason") != ANSWER_AWAITING_PARENT):
        return None
    try:
        from dispatch_replacement import _route
        from session_identity import session_env
        harness = recipient_kind.split("-", 1)[0]
        jobs = Path(receipt["job_registry"])
        owner = receipt["owner_attempt_id"]
        route_file, route = _route(jobs, owner, _rows(jobs)[owner][1])
        env = {**(os.environ if environ is None else environ),
               session_env()[harness][0]: session_id, "AGENT_DISPATCH_CALLER_HARNESS": harness}
        if arm is None:
            from capacity_auto_resume import arm
        return arm({"reason": ANSWER_AWAITING_PARENT, "route_id": route.get("route_id")},
                   route_file, jobs, environ=env)
    except Exception:  # noqa: BLE001 -- the notice still carries the start command
        return None


def render_text(receipt: dict, *, continued: dict | None = None) -> str:
    receipt = validate(receipt)
    if receipt["reason"] == ANSWER_AWAITING_PARENT:
        if continued:
            return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                    "the answer is kept. This session's runtime started the route's continuation, and its "
                    "new owner receives the answer first; the result arrives here as a notice. Tell the user "
                    "in one line; there is nothing to run and no answer to ask for again.")
        command = _resume_text(receipt)
        return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                "the answer is kept. Only this route's parent session launches its continuation, so "
                "continue the work with the route's start command: the new owner receives the answer "
                "first. Do not ask for the answer again." + (" " + command if command else ""))
    if receipt["reason"] == "owner-input-undelivered":
        command = shlex.join(["python3", _printed("capability-route.py"),
                              "correct", "--jobs", receipt["job_registry"],
                              "--attempt-id", receipt["owner_attempt_id"]])
        return ("A user correction was not delivered, or its delivery is unknown. "
                "The execution result is unchanged. Read the exact input receipts, tell the user "
                "what remains unconfirmed, and do not resend an unknown input automatically. " + command)
    if receipt["reason"] == "workflow-completion-pending":
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} finish --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        return ("The owner result remains PASS, but workflow/route/report closure is pending. "
                "The completion controller owns closure; this is not a running model. "
                "Explain the outstanding closure to the user; this notice authorizes no new execution. "
                "Use inspect instead of finish to read the exact gates, cleanup state and checkpoint. "
                "Existing transaction recovery: " + command)
    if receipt["reason"] == "closure-blocked":
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} inspect --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        return ("The owner result remains PASS, but its own workflow/route closure can never complete from "
                "this exact attempt -- a proven permanent reason (a later attempt already claimed this route "
                "node, or the route's completion marker no longer matches its recorded evidence), not an "
                "ordinary in-flight wait. Retrying this attempt's closure will not help. Explain this to the "
                "user and inspect the exact gate reason before deciding a recovery path (a fresh dispatch of "
                "the same route node, or manual repair of the marker/evidence). Read-only diagnosis: " + command)
    if receipt["reason"] in {"watch-deadline", "receiver-unavailable"}:
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} finish --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        detail = ("The completion watch reached its own deadline (about a day) without the batch settling"
                  if receipt["reason"] == "watch-deadline" else
                  "The completion watch could not reach its delivery gateway, and every monitored attempt "
                  "is already terminal")
        return (f"{detail}, so it recorded this notice and stopped instead of running forever. The owner "
                "result and workflow/route closure state are unchanged -- nothing was retried or discarded. "
                "A human or the next launch must resume the completion watch; this notice alone does not. "
                "Existing transaction recovery: " + command)
    utility = _printed("dispatch-registry.py")
    operation = "resolve-terminal-conflict" if receipt["reason"] == "terminal-evidence-conflict" else "reconcile"
    commands = [f"python3 {shlex.quote(str(utility))} {operation} --jobs "
                f"{shlex.quote(receipt['job_registry'])} --attempt {shlex.quote(aid)}"
                for aid in receipt["monitored_attempt_ids"]]
    disposition = (
        "Compare the committed result with the conflicting evidence and explain the discrepancy. "
        "The read-only command previews the exact conflict. After reviewing and recording the evidence, "
        "use the same command with --review-evidence <report> --expected-row-sha256 <preview-hash> --apply "
        "to retain the committed result and release consumption. A new conflict requires a fresh review. "
        if receipt["reason"] == "terminal-evidence-conflict" else
        "If evidence cannot settle them, ask the user whether to keep waiting or cancel the exact work; "
    )
    return ("Hearting supervision needs attention. This is not workflow completion. "
            f"reason={receipt['reason']} owner={receipt['owner_attempt_id']}. "
            "The controller retains waiting/recovery responsibility. Explain the blockage to the user "
            "and inspect these exact attempts. Do not infer death, erase rows, or retry from this notice. "
            + disposition +
            "an accepted notification does not close the work. Read-only diagnosis: " + " ; ".join(commands)
            + _residue_text(receipt))


def context(receipt: dict, delivery_id: str) -> dict:
    return {"threadId": receipt["recipient_thread_id"], "input": [],
            "clientUserMessageId": delivery_id,
            "additionalContext": {"hearting-supervision": {"kind": "application", "value": render_text(receipt)}}}


def wait_for_batch(*, join: Callable[[set[str]], dict], attempts: set[str],
                   jobs: Path, parent_attempt_id: str = "", emit: Callable[[dict], None] | None = None,
                   on_timeout: Callable[[set[str]], None] | None = None,
                   deadline: float | None = None,
                   stop_check: Callable[[], str] | None = None,
                   replacement_checkpoint: Callable | None = None) -> dict:
    """One shared wait loop. Join deadlines are checkpoints, never death votes.

    Execution boundaries retain their finite budgets. The parent receives one
    durable notice for this exact batch while this controller retains the wait.
    A failed queue write is retried at the next bounded checkpoint, not discarded.

    `deadline` and `stop_check` are both optional and, left `None`, leave this
    loop's behavior exactly as before (every existing caller). A caller that
    itself owns an unfinishable-watch budget -- one process that must not run
    forever -- supplies one or both: `deadline` (a `time.monotonic()` value;
    once reached after a join timeout, this records one `watch-deadline`
    notice and returns `{"state": "watch-expired", ...}` instead of looping
    again) and/or `stop_check` (called once per join timeout; a non-empty
    REASONS token it returns halts the same way, under that reason). Neither
    ever fires mid-join -- only at the same checkpoint an ordinary
    `join-deadline` notice already used.
    """
    ordinal = 0
    lineage = []
    attention = []
    while True:
        observer_error = ""
        try:
            receipt = join(set(attempts))
            if replacement_checkpoint is not None:
                effective, edges, attention = replacement_checkpoint(set(attempts))
                lineage.extend(edge for edge in edges if edge not in lineage)
                if set(effective) != set(attempts):
                    attempts = set(effective)
                    continue
            if receipt.get("state") != "timeout":
                if lineage:
                    receipt = {**receipt, "replacement_lineage": sorted(lineage, key=lambda edge: edge["original_attempt_id"])}
                if attention:
                    receipt = {**receipt, "replacement_attention": attention}
                return receipt
        except Exception as exc:
            # Observation failure is not worker failure. Keep the exact wait
            # and transfer the diagnostic, without fabricating completion.
            observer_error = str(exc)
        ordinal += 1
        halt_reason = ""
        if not observer_error:
            if stop_check is not None:
                try:
                    halt_reason = stop_check() or ""
                except Exception as exc:
                    observer_error = str(exc)
            if not halt_reason and deadline is not None and time.monotonic() >= deadline:
                halt_reason = "watch-deadline"
        if halt_reason:
            try:
                records = materialize(jobs, attempts, reason=halt_reason)
            except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                records = []
            result = {"state": "watch-expired", "reason": halt_reason, "delivery_records": records}
            if lineage:
                result["replacement_lineage"] = sorted(lineage, key=lambda edge: edge["original_attempt_id"])
                result["children"] = [{"attempt_id": aid} for aid in sorted(attempts)]
            if attention:
                result["replacement_attention"] = attention
            return result
        notice_error = ""
        try:
            materialize(jobs, attempts,
                        reason="join-observer-failed" if observer_error else "join-deadline")
        except (OSError, ValueError, pending_delivery.PendingDeliveryError) as exc:
            notice_error = str(exc)
        if on_timeout:
            try:
                on_timeout(set(attempts))
            except Exception as exc:
                observer_error = str(exc)
        if emit:
            emit({"type": "dispatch.supervisor.reparked", "parent_attempt_id": parent_attempt_id,
                  "attempt_count": len(attempts), "repark_ordinal": ordinal,
                  "responsible": "supervision-controller", "notice_error": notice_error,
                  "observer_error": observer_error})
        # A failed observer receives bounded backoff; execution budgets remain
        # the execution boundary's responsibility, never this observer's vote.
        time.sleep(30.0 if observer_error else 0.05)


def wait_for_child_settlement(*, jobs: Path, attempts: set[str],
                              parent_attempt_id: str,
                              join: Callable[[set[str]], dict],
                              reconcile: Callable[[set[str]], object],
                              emit: Callable[[dict], None]) -> None:
    """Retain an unresolved child after notification without another model turn.

    Exact recovery and the shared attempt policy decide when work is settled.
    A ready join receipt alone does not prove cleanup or registry closure.
    The ordinary wait controller retains the obligation and parent handback.
    """
    def observe(monitored: set[str]) -> dict:
        if not _pending(_rows(jobs), sorted(monitored), jobs):
            return {"state": "settled"}
        reconcile(monitored)
        if not _pending(_rows(jobs), sorted(monitored), jobs):
            return {"state": "settled"}
        receipt = join(monitored)
        reconcile(monitored)
        if not _pending(_rows(jobs), sorted(monitored), jobs):
            return {"state": "settled"}
        if receipt.get("state") != "timeout":
            # A terminal-but-unclosed observation may return immediately.
            # Retry evidence collection at a bounded cadence, without tokens.
            time.sleep(1.0)
        return {"state": "timeout"}

    wait_for_batch(join=observe, attempts=attempts, jobs=jobs,
                   parent_attempt_id=parent_attempt_id, emit=emit)
