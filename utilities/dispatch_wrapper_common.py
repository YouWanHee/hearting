#!/usr/bin/env python3
"""Helpers the three adapter dispatch wrappers carried as identical copies.

Each `adapters/<harness>/bin/dispatch-headless.py` had these functions
letter for letter (audit §4 #10): failure lines, the registry lock, process
start ticks, the launch fence's failure record, the artifact and report
bundle roots, the launch heartbeat seed, a route node's leg fields, the
supervised owner's route, and the review output request. None of them reads
anything about a harness. The wrappers keep their old names as aliases, so
callers and tests that patch a wrapper's name are unchanged.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from artifact_producer import ProducerError, prepare_review_output_binding
from model_config import ModelConfigError, resolve_config
from route_authority import scan_anchored_death

ROOT = Path(__file__).resolve().parents[1]


def fail(reason: str, code: int, **fields: str) -> int:
    print("check=failed")
    print(f"reason={reason}")
    for key, value in fields.items():
        print(f"{key}={value}")
    return code


def read_launch_fence_failure(fd: int) -> tuple[dict[str, object] | None, bool]:
    """Read and close the fence's private, close-on-exec failure channel.

    Returns the parsed failure record (or None) alongside whether the fence
    was actually released: `BlockingIOError` means the write end is still
    open (the child has not reached the fence yet, so nothing was released),
    while an EOF read means the write end already closed (the fence was
    released with no failure payload).
    """
    try:
        os.set_blocking(fd, False)
        try:
            raw = os.read(fd, 16384)
        except BlockingIOError:
            return None, False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not raw:
        return None, True
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or not isinstance(record.get("reason"), str)
        or not isinstance(record.get("detail"), str)
    ):
        return None, True
    return record, True


def resolve_artifact_root(worktree: str) -> str:
    result = subprocess.run(
        [str(ROOT / "utilities" / "artifact-root.sh"), worktree],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or not Path(value).is_absolute():
        detail = (result.stderr or result.stdout or "invalid artifact root").strip()
        raise ValueError(detail)
    return value


def is_report_bundle_publish_stage(route_file: str | None, route_node: str | None) -> bool:
    if not route_file or route_node != "publish":
        return False
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {
        "id": "publish", "kind": "capability-owner", "unit": "_kernel/owner",
        "completion_gate": "lab-publish", "dispatch_depth": 1,
    }
    return route.get("capability") == "autopilot-lab" and any(
        all(node.get(key) == value for key, value in expected.items())
        for node in route.get("nodes", []) if isinstance(node, dict)
    )


def resolve_report_bundle_root(route_file: str | None, route_node: str | None) -> Path | None:
    if not is_report_bundle_publish_stage(route_file, route_node):
        return None
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "report-bundle.py"), "root", "--optional"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout or "invalid report bundle root").strip())
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("configured report bundle root is not a safe directory")
    return path


def supervisor_route(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """The route a supervised owner is bound to: the standard+ owner binding, or a
    quick owner's own one-shot tuple, never a partial one."""
    binding = getattr(args, "owner_route_binding", None)
    if binding:
        return binding.route_file, binding.route_id, binding.route_hash
    route = tuple(getattr(args, key, None) for key in ("route_file", "route_id", "route_hash"))
    if all(route) and getattr(args, "route_node", None) == "one-shot":
        return route
    return None


@contextmanager
def jobs_lock(jobs: Path):
    jobs.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def route_node_leg_fields(args):
    """Read the sealed leg_class/auxiliary_check off this wrapper's route node.

    W1c projection source: the fields are stamped by the compiler during
    parallel-group expansion, so the wrapper reads its own sealed node instead
    of trusting a second, independently-produced value. Missing node/fields
    project the explicit absence marker `-`.
    """
    route_file = getattr(args, "route_file", None)
    route_node = getattr(args, "route_node", None)
    if not route_file or not route_node:
        return "-", "-"
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "-", "-"
    for node in route.get("nodes", []):
        if isinstance(node, dict) and node.get("id") == route_node:
            return (
                str(node.get("leg_class") or "-"),
                str(node.get("auxiliary_check") or "-"),
            )
    return "-", "-"


def prepare_review_output_request(args) -> None:
    args.review_output_binding = None
    args.review_governed_lease_nonce = ""
    if not args.review_output:
        return
    if (
        args.dispatch_depth != 1
        or args.worker_type != "review"
        or args.unit != "qa/code-review"
        or args.capability != "autopilot-code"
        or args.execution_surface != "registered-headless"
        or not args.registered_worker
        or args.route_file
        or getattr(args, "owner_route_binding", None)
    ):
        raise ProducerError("review-output-tuple-invalid")
    cycle_id = os.environ.get("AGENT_ARTIFACT_CYCLE_ID", "")
    producer_id = os.environ.get("AGENT_ARTIFACT_PRODUCER_ID", "")
    if not cycle_id or not producer_id:
        raise ProducerError("review-output-cycle-binding-missing")
    args.review_output_binding = prepare_review_output_binding(
        Path(args.artifact_root), cycle_id=cycle_id,
        producer_id=producer_id, attempt_id=args.attempt_id,
        review_output=args.review_output, capability=args.capability,
        unit=args.unit, worktree=args.worktree,
    )
    args.review_governed_lease_nonce = secrets.token_hex(32)


def process_start_ticks(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[21]
    except (OSError, IndexError):
        return ""


def seed_launch_heartbeat(args: argparse.Namespace, jobs: Path, pid: int, start: str) -> str:
    if not (args.attempt_id and args.route_id and args.route_node):
        return "not-route-bound"
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/dispatch-progress.py"), "heartbeat",
         "--attempt-id", args.attempt_id, "--route-id", args.route_id,
         "--route-node", args.route_node, "--jobs", str(jobs),
         "--phase", "launch", "--kind", "registry",
         "--evidence", f"pid={pid};start={start or '-'}"],
        cwd=ROOT, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    return "ok" if result.returncode == 0 else "failed"


def model_config_state(harness: str) -> tuple[str, str]:
    """Which models.conf this launch resolved (`user` or `shipped`) and why --
    on the receipt so a user copy silently replaced by the shipped file is
    visible (top review B2)."""

    try:
        _values, receipt = resolve_config(harness, source_root=ROOT)
    except ModelConfigError as exc:
        return "unavailable", str(exc)[:80]
    return receipt.source, receipt.reason


def initialize_owner_input_when(args: argparse.Namespace, jobs: Path, *, supervised: bool, input_kind: str) -> None:
    """Open correction admission at registration; without it `correct` stays unsupported."""
    if not supervised:
        return
    try:
        from dispatch_owner_input import initialize_owner_input
        initialize_owner_input(jobs, args.attempt_id, input_kind)
    except Exception as exc:
        sys.stderr.write(f"owner-input-init-skipped attempt_id={args.attempt_id} reason={type(exc).__name__}\n")


def watch_early_death(
    proc: subprocess.Popen, log_path: Path, watch_secs: float
) -> tuple[str, str] | None:
    """SD-15: poll a just-launched child for a limit/auth early death.

    Returns (reason, reset) if the child exits within watch_secs and its log tail
    matches a DEATH_PATTERN. SD-59 capacity is the one proactive exception: an
    anchored live capacity line interrupts the exact process group for failover.
    Otherwise returns None. Polls in 0.5s steps.
    """
    if watch_secs <= 0:
        return None
    deadline = time.monotonic() + watch_secs
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            live_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            live_tail = ""
        live_death = scan_anchored_death(live_tail)
        if live_death and live_death[0] == "capacity":
            try:
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            return live_death
        time.sleep(0.5)
    if proc.poll() is None:
        return None  # still alive past the watch window — not an early death
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        tail = ""
    death = scan_anchored_death(tail)
    if death:
        return death
    if proc.returncode:
        return f"launch-exit-{proc.returncode}", ""
    return None


def bind_internal_eligibility_probe(args: argparse.Namespace, harness: str) -> None:
    """SD-66 fix-forward: run the nested-eligibility probe in-wrapper when a
    dispatch-depth-2 ``--start`` carries no explicit evidence, instead of failing
    closed on missing flags a caller never had reason to supply by hand.

    Triggers only when both evidence options are still at their parser
    default (``unknown``/empty) and the parent identity needed to run the
    probe is fully known. Explicit supported/unsupported/unknown/partial
    evidence, dispatch-depth-1, and dry-run/register never reach this function's
    trigger path (callers gate on depth/action before calling it). The probe's
    own JSON status is trusted only when every identity field it echoes back
    matches the request; a malformed/mismatched/erroring probe leaves
    ``nested_eligibility`` at its unknown default so `validate_nested_eligibility`
    still fails closed.
    """
    if args.dispatch_depth < 2 or args.action != "start":
        return
    if getattr(args, "nested_eligibility_explicit", False):
        return
    if args.nested_eligibility != "unknown" or args.eligibility_source:
        return
    if not all((args.parent_harness, args.parent_transport, args.parent_sandbox, args.launch_authority)):
        return
    if "unknown" in (args.parent_harness, args.parent_transport, args.parent_sandbox):
        return
    args.eligibility_probe = "internal"
    probe = ROOT / "utilities" / "nested-dispatch-eligibility.py"
    result = subprocess.run(
        [
            sys.executable, str(probe),
            "--parent-harness", args.parent_harness,
            "--parent-transport", args.parent_transport,
            "--parent-sandbox", args.parent_sandbox,
            "--child-harness", harness,
            "--launch-authority", args.launch_authority,
            "--worktree", args.worktree,
            "--json",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    try:
        row = json.loads(result.stdout)
    except (ValueError, TypeError):
        return
    if (
        row.get("parent_harness") != args.parent_harness
        or row.get("parent_transport") != args.parent_transport
        or row.get("parent_sandbox") != args.parent_sandbox
        or row.get("child_harness") != harness
        or row.get("launch_authority") != args.launch_authority
        or row.get("status") not in ("supported", "unsupported", "unknown")
    ):
        return
    if row["status"] == "supported" and result.returncode != 0:
        # A failed probe process cannot mint launch-eligible evidence, even if
        # its stdout says supported; checked unsupported/unknown results keep
        # their nonzero-rc path and still fail closed downstream.
        return
    args.nested_eligibility = row["status"]
    args.eligibility_source = row.get("probe_source") or ""
    args.eligibility_failure_class = row.get("failure_class") or ""
