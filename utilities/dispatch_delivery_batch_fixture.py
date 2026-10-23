#!/usr/bin/env python3
"""Hermetic before/after measurement for depth-1 completion delivery batches.

Each source tree is imported in a fresh Python process. The fixture deliberately
uses the public pending-delivery and sweep APIs plus production-shaped route and
registry records, so the frozen tree reports its actual per-completion behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


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
