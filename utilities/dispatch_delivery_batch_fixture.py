#!/usr/bin/env python3
"""Hermetic before/after measurement for depth-1 completion delivery batches.

Each source tree is imported in a fresh Python process. The fixture deliberately
uses the public pending-delivery and sweep APIs plus production-shaped route and
registry records, so the frozen tree reports its actual per-completion behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock


SEED = 20261023
SIBLINGS = 24
FAILURE_INDICES = (7, 18)
PARENT = "att-measurement-parent"
PARENT_SID = "ses-measurement-parent"


def _child(source_root: Path, output: Path) -> int:
    source_root = source_root.resolve()
    sys.path.insert(0, str(source_root / "utilities"))
    sys.path.insert(0, str(source_root / "hooks"))
    import dispatch_pending_delivery as pending
    import dispatch_session_sweep as sweep
    import dispatch_notice_state as notice_state
    from route_identity import route_hash

    rows = []
    real_monotonic_ns = time.monotonic_ns
    fake_clock = [real_monotonic_ns()]
    time.monotonic_ns = lambda: fake_clock[0]
    with tempfile.TemporaryDirectory(prefix="delivery-batch-fixture-") as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        jobs = state / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        histories = {"all_success": [], "two_failures": []}

        for variant in histories:
            scenario = state / variant
            scenario.mkdir()
            dispatch_root = scenario
            registry = scenario / "jobs.log"
            registry.write_text("", encoding="utf-8")
            records: dict[str, dict] = {}
            route_files = []
            attempts = []
            for index in range(SIBLINGS):
                attempt_id = f"att-measure-{index:032x}"
                route_id = f"rt-measure-{variant}-{index:02d}"
                route = {"route_id": route_id,
                         "parent_cycle_id": f"cycle-measure-{variant}",
                         "nodes": [{"id": "execute"}]}
                route["route_hash"] = route_hash(route)
                route_file = scenario / f"{route_id}.json"
                route_file.write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
                route_files.append((route_id, route_file, route["route_hash"]))
                attempts.append(attempt_id)
                histories[variant].append({"attempt_id": attempt_id, "route_id": route_id,
                                           "route_file": str(route_file),
                                           "route_hash": route["route_hash"]})

            # A parent has 24 registered depth-1 siblings from the outset. Rows
            # become terminal one at a time; later completions are not materialized
            # until their position in this deterministic timeline.
            registry.write_text("".join(
                "2026-10-23T00:00:00Z\trunning"
                + f"\t{scenario}\t{scenario}\tframe\tattempt_id={attempts[index]},"
                + f"dispatch_depth=1,registered_worker=1,worker_type=execute,parent_sid={PARENT_SID},"
                + f"parent_attempt_id={PARENT},parent_harness=claude,route_file={route_files[index][1]},"
                + f"route_id={route_files[index][0]},route_hash={route_files[index][2]},route_node=execute,"
                + f"delivery_intent=1,delivery_intent_at_ns={fake_clock[0]},"
                + f"failure_class=pass,note=completed-marker,"
                + f"delivery_id=delivery-measure-{variant}-{index:02d}\n"
                for index in range(SIBLINGS)
            ), encoding="utf-8")

            # Registry state is rewritten as each sibling finishes. This keeps the
            # route membership real while the fixed fake clock advances by index.
            raw_rows = registry.read_text(encoding="utf-8").splitlines()
            timeline = []
            recipient_kind = "claude-parent-runtime"
            previous_fixed = 0
            for index in range(SIBLINGS):
                fake_clock[0] += 1_000_000_000
                failed = variant == "two_failures" and index in FAILURE_INDICES
                fields = raw_rows[index].split("\t")
                fields[1] = "done"
                fields[0] = f"2026-10-23T00:00:{index:02d}Z"
                metadata = fields[5].split(",")
                metadata = [part for part in metadata if not part.startswith(("failure_class=", "note="))]
                metadata.extend(("failure_class=fail", "note=dead-worker-fail") if failed else
                                ("failure_class=pass", "note=completed-marker"))
                fields[5] = ",".join(metadata)
                raw_rows[index] = "\t".join(fields)
                registry.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")

                attempt_id = attempts[index]
                route_id, _route_file, _route_digest = route_files[index]
                delivery_id = f"delivery-measure-{variant}-{index:02d}"
                classification = "attention" if failed else "success"
                child = {"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                         "reason": "terminal-failure-or-unclosed" if failed else "terminal-complete",
                         "required_action": "inspect-done-failure" if failed else "advance-completed",
                         "harness": "claude", "delivery_classification": classification}
                receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": PARENT,
                           "job_registry": str(registry), "children": [child],
                           "delivery_classification": classification}
                record = pending.create(
                    dispatch_root, recipient_kind=recipient_kind, recipient_key=PARENT_SID,
                    delivery_id=delivery_id, session_generation="",
                    session_generation_supported="0", attempt_ids=[attempt_id],
                    parent_attempt_id=PARENT, route_id=route_id, route_node="execute",
                    receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
                    row_revisions={attempt_id: f"revision-{index:02d}"})
                records[delivery_id] = record

                resolver = getattr(notice_state, "resolve_delivery_batch", None)
                decision = (resolver(registry, record, recipient_key=PARENT_SID,
                                     delivery_root=dispatch_root) if resolver else None)

                delivered, _count = sweep.sweep_deliver(
                    dispatch_root, recipient_kind, PARENT_SID)
                body = sweep.delivery_context([(dispatch_root, delivered)]) if delivered else ""
                if delivered:
                    # The sweep's exact prompt-context acknowledgement is the
                    # native receipt boundary for this carrier.
                    sweep.ack_delivered(dispatch_root, PARENT_SID, delivered,
                                        acked_by="fixture-parent-turn")
                transitions = []
                for record_id, before in records.items():
                    after = pending.read(dispatch_root, PARENT_SID, record_id)
                    if before.get("state") != after.get("state"):
                        transitions.append({"delivery_id": record_id,
                                            "from": before.get("state"), "to": after.get("state")})
                    records[record_id] = after
                timeline.append({"completion": index + 1, "attempt_id": attempt_id,
                                 "failed": failed, "wakes": 1 if delivered else 0,
                                 "emissions": 1 if delivered else 0,
                                 "injected_characters": len(body),
                                 "fixed_instruction_characters": _fixed_instruction_length(body),
                                 "ack_transitions": transitions,
                                 "delivered_ids": [item["delivery_id"] for item in delivered],
                                 "batch_decision": ({"held": decision.get("held"),
                                     "delivery_ids": list(decision.get("delivery_ids", ())),
                                     "outstanding_attempt_ids": list(decision.get(
                                         "outstanding_attempt_ids", ())) } if decision else None)})

            unresolved = [did for did in records
                          if pending.read(dispatch_root, PARENT_SID, did)["state"] != "acked"]
            total = {
                "wakes": sum(item["wakes"] for item in timeline),
                "emissions": sum(item["emissions"] for item in timeline),
                "injected_characters": sum(item["injected_characters"] for item in timeline),
                "fixed_instruction_characters": sum(item["fixed_instruction_characters"] for item in timeline),
                "ack_transition_count_by_record": {
                    did: sum(1 for item in timeline for transition in item["ack_transitions"]
                             if transition["delivery_id"] == did and transition["to"] == "acked")
                    for did in records
                },
                "unsettled_record_ids": unresolved,
                "failure_delivery_completion": {
                    item["attempt_id"]: item["completion"] for item in timeline if item["failed"]
                    and item["wakes"]
                },
                "timeline": timeline,
            }
            rows.append({"carrier": "shared_sweep", "variant": variant,
                         "observed": total, "status": "measured"})

            rows.append(_measure_rewake(source_root, variant))
            rows.append(_measure_codex_queue(source_root, variant))

    output.parent.mkdir(parents=True, exist_ok=True)
    time.monotonic_ns = real_monotonic_ns
    output.write_text(json.dumps({"schema_version": 1, "seed": SEED,
                                  "source_root": str(source_root), "measurements": rows},
                                 indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(rows, sort_keys=True))
    return 0


def _fixed_instruction_length(body: str) -> int:
    if not body:
        return 0
    # Count only the fixed shared completion header and deduplicated follow-up,
    # excluding per-record receipt lines and markers.
    import dispatch_session_sweep as sweep
    return len(sweep.COMPLETION_DELIVERY_HEADER) + sum(
        len(line) for line in body.splitlines()
        if line.startswith("Original request/format apply.")
        or line.startswith("Required action:")
    )


def _carrier_fixed_instruction_length(carrier: str, body: str) -> int:
    """Count shared instruction text, excluding per-delivery receipt content."""
    lines = body.splitlines()
    if carrier == "claude_async_rewake":
        # The test's classified receipt is a per-attempt receipt line; the
        # remaining text is only the per-record native receipt marker.
        return sum(len(line) for line in lines
                   if not line.startswith(("RECEIPT ", "HEARTING_RECEIPT_V1 ")))
    if carrier == "codex_native_queue":
        return sum(len(line) for line in lines
                   if line.startswith(("Completion bookkeeping for the listed attempts:",
                                       "The commands use the shared, runtime-neutral")))
    raise ValueError(f"unknown delivery carrier: {carrier}")


def _measurement_row(carrier: str, variant: str, timeline: list[dict], records: dict,
                     pending, root: Path, recipient: str) -> dict:
    counts = {delivery_id: sum(
        1 for item in timeline for transition in item["ack_transitions"]
        if transition["delivery_id"] == delivery_id and transition["to"] == "acked")
        for delivery_id in records}
    unresolved = [delivery_id for delivery_id in records
                  if pending.read(root, recipient, delivery_id)["state"] != "acked"]
    return {"carrier": carrier, "variant": variant, "status": "measured", "observed": {
        "wakes": sum(item["wakes"] for item in timeline),
        "emissions": sum(item["emissions"] for item in timeline),
        "injected_characters": sum(item["injected_characters"] for item in timeline),
        "fixed_instruction_characters": sum(item["fixed_instruction_characters"] for item in timeline),
        "ack_transition_count_by_record": counts,
        "unsettled_record_ids": unresolved,
        "failure_delivery_completion": {
            item["attempt_id"]: item["completion"] for item in timeline
            if item["failed"] and item["wakes"]},
        "timeline": timeline,
    }}


def _write_sibling_route(root: Path, variant: str, index: int) -> str:
    from route_identity import route_hash
    route_id = f"rt-measure-{variant}-{index:02d}"
    route = {"route_id": route_id, "parent_cycle_id": f"cycle-measure-{variant}",
             "nodes": [{"id": "execute"}]}
    route["route_hash"] = route_hash(route)
    (root / f"{route_id}.json").write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
    return route_id


def _measure_rewake(source_root: Path, variant: str) -> dict:
    import dispatch_pending_delivery as pending
    import dispatch_notice_state

    spec = importlib.util.spec_from_file_location(
        "dispatch_owner_rewake_fixture", source_root / "hooks" / "dispatch-owner-rewake.py")
    rewake = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rewake
    spec.loader.exec_module(rewake)
    recipient = "ses-measurement-parent"
    with tempfile.TemporaryDirectory(prefix="delivery-rewake-fixture-") as temporary:
        root = Path(temporary)
        jobs = root / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        records: dict[str, dict] = {}
        members = []
        success_total = SIBLINGS - (len(FAILURE_INDICES) if variant == "two_failures" else 0)
        for index in range(SIBLINGS):
            attempt = f"att-rewake-{index:032x}"
            delivery_id = f"delivery-rewake-{variant}-{index:02d}"
            route_id = _write_sibling_route(root, variant, index)
            failed = variant == "two_failures" and index in FAILURE_INDICES
            classification = "attention" if failed else "success"
            receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": PARENT,
                       "job_registry": str(jobs), "parent_cycle_id": f"cycle-measure-{variant}",
                       "delivery_classification": classification,
                       "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                     "reason": "terminal-failure-or-unclosed" if failed else "terminal-complete",
                                     "required_action": "inspect-done-failure" if failed else "advance-completed",
                                     "harness": "claude", "delivery_classification": classification}]}
            record = pending.create(
                root, recipient_kind="claude-parent-runtime", recipient_key=recipient,
                delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                attempt_ids=[attempt], parent_attempt_id=PARENT,
                route_id=route_id, route_node="execute", receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt),
                row_revisions={attempt: f"revision-{index:02d}"})
            records[delivery_id] = record
            members.append((attempt, delivery_id, failed))

        timeline, held, completed_ids = [], [], set()
        known = {delivery_id: failed for _attempt, delivery_id, failed in members}
        for index, (attempt, delivery_id, failed) in enumerate(members):
            completed_ids.add(delivery_id)
            candidates = [delivery_id] + list(held)
            held = []
            before = {key: pending.read(root, recipient, key)["state"] for key in records}
            completion_texts = []
            wakes = chars = fixed = 0
            failed_carrier = failed
            for candidate_id in candidates:
                candidate_attempt = next(a for a, d, _f in members if d == candidate_id)
                candidate_launch = rewake.Launch(attempt_id=candidate_attempt, jobs=jobs,
                                                  session_id=recipient, armed="registry")
                metadata = {"delivery_id": candidate_id, "parent_sid": recipient}
                ready_successes = [d for d, is_failure in known.items()
                                   if not is_failure
                                   and pending.read(root, recipient, d)["state"]
                                   not in {"acked", "rejected", "expired"}]
                completed_successes = sum(1 for _a, d, is_failure in members
                                          if d in completed_ids and not is_failure)
                held_now = (not known[candidate_id] and completed_successes < success_total
                            and "members" in getattr(rewake.ClaimWin, "__dataclass_fields__", {}))
                def resolve(_jobs, record, **_kwargs):
                    if record.get("receipt", {}).get("delivery_classification") != "success":
                        return None
                    return {"delivery_ids": tuple(ready_successes), "held": held_now}
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True), \
                     mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=resolve,
                                create=True), \
                     mock.patch.object(rewake, "classified_receipt", side_effect=lambda l, st, r, _root:
                                       ("attention" if known[candidate_id] else "success",
                                        f"RECEIPT {l.attempt_id} state={'attention' if known[candidate_id] else 'success'}")), \
                     mock.patch.object(rewake, "_gate_notices", return_value=[]), \
                     mock.patch.object(rewake, "settle_arm", return_value=True), \
                     mock.patch.object(rewake, "_ended", side_effect=lambda _claim, code, **_kw: code), \
                     mock.patch.object(rewake.sys, "stdout", stdout), \
                     mock.patch.object(rewake.sys, "stderr", stderr):
                    win = rewake._carrier_one_claim(candidate_launch, metadata)
                    if isinstance(win, getattr(rewake, "ClaimHeld", ())):
                        held.append(candidate_id)
                        continue
                    if win is None:
                        continue
                    message = f"RECEIPT {candidate_attempt} state={'attention' if known[candidate_id] else 'success'}"
                    if getattr(win, "members", ()) and hasattr(rewake, "_batch_message"):
                        message = rewake._batch_message(candidate_launch, win, message)
                    else:
                        from dispatch_recipient_receipt import receipt_marker
                        claimed_record = pending.read(root, recipient, win.delivery_id)
                        message += "\n" + receipt_marker(claimed_record, recipient)
                    code = rewake._emit_with_gates(candidate_launch, mock.sentinel.arm,
                                                  "attention" if known[candidate_id] else "success",
                                                  message, block=False, owner_closed=True)
                    output = stderr.getvalue() or stdout.getvalue()
                    if code == 2:
                        completion_texts.append(output)
                        wakes += 1
                        chars += len(output)
                        fixed += _carrier_fixed_instruction_length("claude_async_rewake", output)
                        for member_id in (item_id for item_id, _record in win.members) \
                                if getattr(win, "members", ()) else (win.delivery_id,):
                            try:
                                pending.mark_sent_ambiguous(root, recipient, member_id,
                                                            claim_owner=win.claim_owner)
                                # The captured native receipt marker is consumed exactly once.
                                pending.ack(root, recipient, member_id,
                                            acked_by="fixture-claude-exact-marker")
                            except pending.PendingDeliveryError:
                                pass
            after = {key: pending.read(root, recipient, key)["state"] for key in records}
            transitions = [{"delivery_id": key, "from": before[key], "to": value}
                           for key, value in after.items() if before[key] != value]
            body = "\n".join(completion_texts)
            timeline.append({"completion": index + 1, "attempt_id": attempt, "failed": failed_carrier,
                             "wakes": wakes, "emissions": wakes, "injected_characters": chars,
                             "fixed_instruction_characters": fixed,
                             "ack_transitions": transitions,
                             "delivered_ids": [t["delivery_id"] for t in transitions if t["to"] == "acked"],
                             "captured_text": body})
        return _measurement_row("claude_async_rewake", variant, timeline, records,
                                pending, root, recipient)


def _measure_codex_queue(source_root: Path, variant: str) -> dict:
    import dispatch_pending_delivery as pending
    import dispatch_notice_state

    spec = importlib.util.spec_from_file_location(
        "codex_managed_completion_fixture", source_root / "utilities" / "codex-managed-completion.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    recipient = "ses-measurement-parent"
    with tempfile.TemporaryDirectory(prefix="delivery-codex-queue-fixture-") as temporary:
        root = Path(temporary)
        jobs = root / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        records, members, rows_by_attempt = {}, [], {}
        success_total = SIBLINGS - (len(FAILURE_INDICES) if variant == "two_failures" else 0)
        for index in range(SIBLINGS):
            attempt = f"att-codex-{index:032x}"
            delivery_id = f"delivery-codex-{variant}-{index:02d}"
            route_id = _write_sibling_route(root, variant, index)
            failed = variant == "two_failures" and index in FAILURE_INDICES
            classification = "attention" if failed else "success"
            receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": PARENT,
                       "job_registry": str(jobs), "parent_cycle_id": f"cycle-measure-{variant}",
                       "delivery_classification": classification,
                       "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                     "reason": "terminal-failure-or-unclosed" if failed else "terminal-complete",
                                     "required_action": "inspect-done-failure" if failed else "advance-completed",
                                     "delivery_classification": classification}]}
            record = pending.create(
                root, recipient_kind="codex-native-queue", recipient_key=recipient,
                delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                attempt_ids=[attempt], parent_attempt_id=PARENT, route_id=route_id,
                route_node="execute", receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt),
                row_revisions={attempt: f"revision-{index:02d}"})
            records[delivery_id] = record
            row = SimpleNamespace(attempt_id=attempt, metadata={
                "delivery_id": delivery_id, "delivery_recipient_kind": "codex-native-queue",
                "delivery_row_revision": f"revision-{index:02d}",
                "delivery_receipt_digest": record["receipt_digest"]})
            rows_by_attempt[attempt] = row
            members.append((attempt, delivery_id, receipt, failed))

        sent, timeline, held = [], [], []
        success_ids = {delivery_id for _attempt, delivery_id, _receipt, failed in members if not failed}
        def resolve(_jobs, record, **_kwargs):
            if record.get("receipt", {}).get("delivery_classification") != "success":
                return None
            open_ids = tuple(sorted(d for d in success_ids if pending.read(root, recipient, d)["state"]
                                    not in {"acked", "rejected", "expired"}))
            completed_successes = sum(1 for _a, d, _r, failed in members[:len(done_indices)]
                                      if not failed and d in records)
            return {"delivery_ids": open_ids, "held": completed_successes < success_total}

        done_indices = []
        def send(_path, **kwargs):
            sent.append(kwargs)
            return {"status": "consumed", "turn_id": f"turn-{len(sent)}", "turn_status": "completed"}
        def exact_evidence(**kwargs):
            return {"session_id": kwargs.get("session_id"),
                    "client_message_id": kwargs.get("client_message_id"),
                    "confirmed_client_message_id": kwargs.get("client_message_id"),
                    "expected_message": kwargs.get("expected_message")}
        def settle_exact(storage_root, session_id, harness, native_thread, record, evidence, *, acked_by):
            if evidence.get("expected_message") not in [item.get("message") for item in sent]:
                return {"received": False, "settled": False}
            pending.ack(storage_root, session_id, record["delivery_id"], acked_by=acked_by,
                        expected_states=("pending", "claimed", "sent-ambiguous"))
            return {"received": True, "settled": True}

        for index, (attempt, delivery_id, receipt, failed) in enumerate(members):
            done_indices.append(index + 1)
            before = {key: pending.read(root, recipient, key)["state"] for key in records}
            args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=recipient,
                                   jobs=jobs, timeout=2.0, delivery_retry_interval=0.01)
            candidates = [index] + list(held)
            held = []
            sends_before = len(sent)
            for candidate_index in candidates:
                candidate_attempt, candidate_id, candidate_receipt, _failed = members[candidate_index]
                with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                        "thread": {"id": recipient, "cwd": "/tmp/repo"}}), \
                     mock.patch.object(module, "current_session_children",
                                       side_effect=lambda _jobs, _thread, attempts, _kind:
                                       [rows_by_attempt[a] for a in sorted(attempts)]), \
                     mock.patch.object(module, "_queue_target_from_rows", return_value=recipient), \
                     mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=resolve,
                                create=True), \
                     mock.patch("dispatch_notice_state.notice_is_current", return_value=True), \
                     mock.patch.object(module.pending_delivery, "_replacement_disposition", return_value=""), \
                     mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send), \
                     mock.patch("peer_obligations.retain_registered_completion", return_value=True), \
                     mock.patch("dispatch_recipient_receipt.codex_consumed_evidence", side_effect=exact_evidence), \
                     mock.patch("dispatch_recipient_receipt.settle_native_receipt", side_effect=settle_exact):
                    try:
                        result = module._native_queue_delivery_once(
                            args, candidate_receipt, {candidate_attempt})
                    except pending.PendingDeliveryError as exc:
                        if exc.detail == "batch-hold":
                            held.append(candidate_index)
                            continue
                        raise
                if result.get("status") == "sent-ambiguous" and result.get("reason") == "native-queue-batch-held":
                    held.append(candidate_index)
            after = {key: pending.read(root, recipient, key)["state"] for key in records}
            transitions = [{"delivery_id": key, "from": before[key], "to": value}
                           for key, value in after.items() if before[key] != value]
            new_sends = sent[sends_before:]
            relevant = "\n".join(item["message"] for item in new_sends)
            timeline.append({"completion": index + 1, "attempt_id": attempt, "failed": failed,
                             "wakes": len(new_sends), "emissions": len(new_sends),
                             "injected_characters": len(relevant),
                             "fixed_instruction_characters": _carrier_fixed_instruction_length(
                                 "codex_native_queue", relevant),
                             "ack_transitions": transitions,
                             "delivered_ids": [t["delivery_id"] for t in transitions if t["to"] == "acked"],
                             "captured_text": relevant})
        return _measurement_row("codex_native_queue", variant, timeline, records,
                                pending, root, recipient)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source_root is None or args.output is None:
        parser.error("--source-root and --output are required")
    if args.child:
        return _child(args.source_root, args.output)

    source_root = args.source_root.resolve()
    with tempfile.TemporaryDirectory(prefix="delivery-batch-runtime-") as temporary:
        temp = Path(temporary)
        env = os.environ.copy()
        for name in ("HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TAB_ID",
                     "HERDR_WORKSPACE_ID", "HERDR_ENV"):
            env.pop(name, None)
        env.update({"HOME": str(temp / "home"), "XDG_CONFIG_HOME": str(temp / "xdg-config"),
                    "XDG_STATE_HOME": str(temp / "xdg-state"), "PYTHONDONTWRITEBYTECODE": "1"})
        Path(env["HOME"]).mkdir()
        Path(env["XDG_CONFIG_HOME"]).mkdir()
        Path(env["XDG_STATE_HOME"]).mkdir()
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--child", "--source-root", str(source_root),
                                 "--output", str(args.output.resolve())],
                                cwd=source_root, env=env, text=True, capture_output=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
