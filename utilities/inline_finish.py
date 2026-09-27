"""Retryable direct inline route finish transaction."""
from __future__ import annotations
import fcntl, hashlib, json, os, stat, subprocess, sys, tempfile
import fnmatch
from pathlib import Path
from typing import Any, Mapping
import artifact_producer, artifact_lifecycle, dispatch_terminal_commit

class InlineFinishError(ValueError):
    pass

def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    fd, tmp = tempfile.mkstemp(prefix=".finish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def _read(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise InlineFinishError("finish-intent-unsafe")
        row = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise InlineFinishError("finish-intent-corrupt") from exc
    if not isinstance(row, dict) or row.get("schema") != "inline_finish_v1":
        raise InlineFinishError("finish-intent-corrupt")
    return row

def _fault(point: str) -> None:
    if os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == point:
        raise InlineFinishError("fault-injected-" + point)

def finish(args, route: Mapping[str, Any], route_file: Path, api) -> dict[str, Any]:
    root = Path(route["artifact_root"]).resolve(strict=True)
    route_file = Path(route_file)
    if route_file.is_symlink():
        raise InlineFinishError("finish-route-unsafe")
    route_file = route_file.resolve(strict=True)
    if route.get("effective_intensity") != "direct":
        raise InlineFinishError("finish-route-not-direct")
    nodes = route.get("nodes") or []
    if (len(nodes) != 1 or nodes[0].get("id") != "inline"
            or nodes[0].get("kind") != "capability-owner"
            or nodes[0].get("dispatch_depth") != 0
            or nodes[0].get("execution_surface") != "inline"
            or nodes[0].get("registered_worker") is not False
            or nodes[0].get("terminal") is not True):
        raise InlineFinishError("finish-inline-owner-sentinel-required")
    # The interactive caller can be hosted by any supported runtime.  Keep the
    # same ambiguity rule used by dispatch admission rather than guessing one
    # session variable when a shell happens to inherit several.
    from dispatch_parent_completion import interactive_parent_identity
    from dispatch_contract import DispatchContractError
    try:
        _harness, sid = interactive_parent_identity(os.environ)
    except DispatchContractError as exc:
        if str(exc) == "caller-harness-ambiguous":
            raise InlineFinishError("finish-caller-harness-ambiguous") from exc
        raise InlineFinishError("finish-caller-harness-invalid") from exc
    if not sid:
        raise InlineFinishError("finish-current-session-missing")
    if not sid or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") != "0":
        raise InlineFinishError("finish-registered-caller-ineligible")
    if dispatch_terminal_commit.require_current_cleanup("inline-finish", target=route_file) is not None:
        raise InlineFinishError("finish-foreign-cleanup-scope")
    # Require the session marker at the canonical Codex state path, sealed to this route.
    from dispatch_contract import route_grounding_state_dir
    marker_root = route_grounding_state_dir(Path(os.environ.get("AGENT_HOME", "")), os.environ)
    session_key = hashlib.sha256(b"material-route-session-v1\0" + sid.encode()).hexdigest()
    marker_path = marker_root / (session_key + ".json")
    try:
        if marker_path.is_symlink() or not stat.S_ISREG(marker_path.lstat().st_mode):
            raise InlineFinishError("finish-current-session-route-missing")
        marker = json.loads(marker_path.read_text())
    except (OSError, ValueError) as exc:
        raise InlineFinishError("finish-current-session-route-missing") from exc
    if (marker.get("session_key") != session_key or marker.get("route_id") != route.get("route_id")
            or marker.get("route_hash") != route.get("route_hash")
            or Path(marker.get("route_file", "")).resolve() != route_file
            or Path(marker.get("cwd", "")).resolve() != Path(route["cwd"]).resolve()):
        raise InlineFinishError("finish-current-session-route-mismatch")
    jobs = os.environ.get("AGENT_DISPATCH_JOBS", "")
    if not jobs or not Path(jobs).is_file():
        raise InlineFinishError("finish-registry-unavailable")
    try:
        for line in Path(jobs).read_text().splitlines():
            fields = line.split("\t")
            from dispatch_contract import parse_registry_metadata
            metadata = parse_registry_metadata(fields[5]) if len(fields) > 5 else {}
            if route["route_id"] in {metadata.get("route_id"), metadata.get("owner_route_id")}:
                raise InlineFinishError("finish-registered-route-ineligible")
    except InlineFinishError: raise
    except Exception as exc: raise InlineFinishError("finish-registry-unreadable") from exc
    node = nodes[-1]
    if not isinstance(node, dict) or not node.get("id"):
        raise InlineFinishError("finish-terminal-node-missing")
    base = root / ".runtime" / "inline-finish" / "v1" / route["route_id"]
    state_path, lock_path = base / "finish.json", base / "finish.lock"
    prior_state = _read(state_path)
    if not prior_state and api.outcome_path(route_file).exists():
        raise InlineFinishError("finish-route-already-closed")
    record = artifact_producer.route_cycle_for(root, route)
    if record is None and prior_state:
        record = artifact_producer.read_cycle_record(root, prior_state.get("intent", {}).get("cycle_id", ""))
    if not record:
        raise InlineFinishError("finish-route-cycle-missing")
    if record.get("state") != "open" and not prior_state:
        raise InlineFinishError("finish-route-cycle-missing")
    admitted = artifact_producer.cycle_route_admission(
        root, record, route, finalize=True, validation_only=record.get("state") == "sealed")
    if not admitted.allow:
        raise InlineFinishError("finish-route-cycle-" + admitted.reason)
    campaign = artifact_producer.read_campaign(root, record["campaign_id"])
    if not campaign or campaign.get("key") != route.get("campaign_key") or not route.get("campaign_key"):
        raise InlineFinishError("finish-campaign-key-mismatch")
    if artifact_producer._live_review_lease(root, record["cycle_id"]) is not None:
        raise InlineFinishError("finish-active-review-lease")
    output = artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    evidence = Path(args.evidence).absolute()
    try:
        if evidence.is_symlink() or not stat.S_ISREG(evidence.lstat().st_mode):
            raise InlineFinishError("finish-evidence-not-regular")
        evidence.resolve(strict=True).relative_to(output.resolve(strict=True))
        evidence_raw = evidence.read_bytes()
    except InlineFinishError: raise
    except (OSError, ValueError) as exc: raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable") from exc
    if not evidence_raw: raise InlineFinishError("finish-evidence-empty")
    try: summary_raw = Path(args.summary_file).read_bytes()
    except OSError as exc: raise InlineFinishError("finish-summary-unreadable") from exc
    if not summary_raw or b"\0" in summary_raw: raise InlineFinishError("finish-summary-invalid")
    try: summary_text = summary_raw.decode("utf-8").strip()
    except UnicodeError as exc: raise InlineFinishError("finish-summary-invalid") from exc
    if not summary_text: raise InlineFinishError("finish-summary-invalid")
    source = str(route.get("source_commit") or "")
    commit = str(args.commit or api._head_commit(route["cwd"]) or "").lower()
    if not api._COMMIT_SHA.fullmatch(source) or not api._COMMIT_SHA.fullmatch(commit):
        raise InlineFinishError("finish-commit-invalid")
    ancestor = subprocess.run(["git", "-C", route["cwd"], "merge-base", "--is-ancestor", source, commit], capture_output=True)
    head = subprocess.run(["git", "-C", route["cwd"], "rev-parse", "HEAD"], capture_output=True, text=True)
    commit_reachable = subprocess.run(["git", "-C", route["cwd"], "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True)
    if ancestor.returncode or head.returncode or commit_reachable.returncode:
        raise InlineFinishError("finish-commit-not-source-descendant")
    # Tracked edits inside this direct node's declared scope are not admissible.
    scope = (node.get("write_scope") or [])
    dirty = subprocess.run(["git", "-C", route["cwd"], "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True)
    if dirty.returncode:
        raise InlineFinishError("finish-git-status-unavailable")
    for line in dirty.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if any(item == "source-scoped" or fnmatch.fnmatchcase(path, item.removesuffix("/**") + "/*")
               or path == item.removesuffix("/**") for item in scope):
            raise InlineFinishError("finish-scoped-tracked-dirt")
    identity = artifact_lifecycle.read_root_identity(root)
    if not identity: raise InlineFinishError("finish-artifact-root-identity-missing")
    evidence_rel = evidence.resolve().relative_to(output.resolve()).as_posix()
    intent = {"route_id":route["route_id"], "route_hash":route["route_hash"],
              "artifact_root_id":identity.artifact_root_id, "campaign_key":campaign.get("key"),
              "campaign_id":record["campaign_id"], "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
              "terminal_node":node["id"], "evidence_path":evidence_rel, "evidence_sha256":_digest(evidence_raw),
              "summary_sha256":_digest(summary_raw), "commit":commit,
              "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record)}
    intent_id = _digest(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())
    base.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-prove every mutable admission fact while holding the route
            # finish fence.  In particular lstat before reading so a raced
            # symlink replacement cannot redirect evidence bytes.
            try:
                evidence_mode = evidence.lstat().st_mode
                if not stat.S_ISREG(evidence_mode) or evidence.is_symlink():
                    raise InlineFinishError("finish-evidence-not-regular")
                locked_evidence = evidence.read_bytes()
                locked_summary = Path(args.summary_file).read_bytes()
            except OSError as exc:
                raise InlineFinishError("finish-evidence-drift") from exc
            if locked_evidence != evidence_raw or locked_summary != summary_raw:
                raise InlineFinishError("finish-evidence-drift")
            try:
                if marker_path.is_symlink() or not stat.S_ISREG(marker_path.lstat().st_mode):
                    raise InlineFinishError("finish-current-session-route-missing")
                locked_marker = json.loads(marker_path.read_text())
            except (OSError, ValueError) as exc:
                raise InlineFinishError("finish-current-session-route-missing") from exc
            if (locked_marker.get("session_key") != session_key
                    or locked_marker.get("route_id") != route.get("route_id")
                    or locked_marker.get("route_hash") != route.get("route_hash")
                    or Path(locked_marker.get("route_file", "")).resolve() != route_file
                    or Path(locked_marker.get("cwd", "")).resolve() != Path(route["cwd"]).resolve()):
                raise InlineFinishError("finish-current-session-route-mismatch")
            try:
                for line in Path(jobs).read_text().splitlines():
                    fields = line.split("\t")
                    from dispatch_contract import parse_registry_metadata
                    metadata = parse_registry_metadata(fields[5]) if len(fields) > 5 else {}
                    if route["route_id"] in {metadata.get("route_id"), metadata.get("owner_route_id")}:
                        raise InlineFinishError("finish-registered-route-ineligible")
            except InlineFinishError:
                raise
            except Exception as exc:
                raise InlineFinishError("finish-registry-unreadable") from exc
            locked_dirty = subprocess.run(
                ["git", "-C", route["cwd"], "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True)
            if locked_dirty.returncode:
                raise InlineFinishError("finish-git-status-unavailable")
            for line in locked_dirty.stdout.splitlines():
                path = line[3:].strip().strip('"')
                if any(item == "source-scoped" or fnmatch.fnmatchcase(path, item.removesuffix("/**") + "/*")
                       or path == item.removesuffix("/**") for item in scope):
                    raise InlineFinishError("finish-scoped-tracked-dirt")
            latest = artifact_producer.read_cycle_record(root, record["cycle_id"])
            if (not latest or latest.get("state") not in {"open", "sealed"}
                    or dispatch_terminal_commit.cycle_identity_digest(latest) != intent["cycle_record_digest"]):
                raise InlineFinishError("finish-cycle-drift")
            current_route = json.loads(route_file.read_text(encoding="utf-8"))
            if (current_route.get("route_id") != intent["route_id"]
                    or current_route.get("route_hash") != intent["route_hash"]
                    or api.route_hash(current_route) != intent["route_hash"]):
                raise InlineFinishError("finish-route-drift")
            current_head = api._head_commit(route["cwd"])
            if not current_head or subprocess.run(
                    ["git", "-C", route["cwd"], "merge-base", "--is-ancestor", intent["commit"], current_head],
                    capture_output=True).returncode:
                raise InlineFinishError("finish-commit-drift")
            if artifact_producer._live_review_lease(root, record["cycle_id"]) is not None:
                raise InlineFinishError("finish-active-review-lease")
            state = _read(state_path)
            if state and state.get("inline_finish_id") != intent_id:
                prior_intent = state.get("intent") or {}
                if (prior_intent.get("evidence_path") == evidence_rel
                        and prior_intent.get("evidence_sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-evidence-drift")
                raise InlineFinishError("finish-intent-conflict")
            replayed_at_entry = state is not None
            if not state:
                _fault("before-claim")
                state = {"schema":"inline_finish_v1", "inline_finish_id":intent_id, "intent":intent,
                         "claimant_session":sid, "state":"claimed"}
                _atomic(state_path, state)
                _fault("after-claim")
            marker_file = api.completion_dir(route["route_id"]) / (node["id"] + ".json")
            if state["state"] == "claimed":
                _fault("before-marker")
                if not marker_file.is_file():
                    cmd = [sys.executable, str(api.ROOT/"utilities/capability-route.py"), "complete",
                           "--route", str(route_file), "--node", node["id"], "--evidence", str(evidence)]
                    child_env=os.environ.copy(); child_env["AGENT_INLINE_FINISH_ID"]=intent_id
                    subprocess.run(cmd, cwd=route["cwd"], env=child_env, check=True, capture_output=True, text=True)
                marker_raw = marker_file.read_bytes()
                marker = json.loads(marker_raw)
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                _fault("after-marker-write")
                state.update(state="node-completed", terminal_marker_digest=marker_digest); _atomic(state_path, state)
                _fault("after-marker")
            else:
                marker_raw = marker_file.read_bytes()
                try:
                    marker = json.loads(marker_raw)
                except (ValueError, UnicodeError) as exc:
                    raise InlineFinishError("finish-marker-corrupt") from exc
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                if marker_digest != state.get("terminal_marker_digest"): raise InlineFinishError("finish-marker-drift")
            outcome_path = api.outcome_path(route_file)
            binding = {"kind":"inline_producer_binding_v1", "artifact_root_id":identity.artifact_root_id,
                       "campaign_key":campaign.get("key"), "campaign_id":record["campaign_id"],
                       "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
                       "route_id":route["route_id"], "route_hash":route["route_hash"],
                       "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record),
                       "terminal_marker_digest":marker_digest, "evidence_sha256":intent["evidence_sha256"],
                       "inline_finish_id":intent_id}
            if state["state"] == "node-completed":
                _fault("before-close")
                if not outcome_path.is_file():
                    outcome, _ = api.close_route(route, route_file, commit, summary_text,
                        allow_unproven=False, expected_terminal_marker_digest=marker_digest,
                        inline_finish_id=intent_id, inline_commit=commit,
                        expected_summary_digest=intent["summary_sha256"],
                        expected_producer_binding_digest=_digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode()))
                else:
                    outcome = json.loads(outcome_path.read_text())
                    if (outcome.get("route_hash") != route["route_hash"]
                            or outcome.get("terminal_marker_digest") != marker_digest
                            or outcome.get("inline_finish_id") != intent_id
                            or outcome.get("summary_digest") != intent["summary_sha256"]
                            or outcome.get("producer_binding_digest") != _digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode())
                            or outcome.get("head_commit") != commit):
                        raise InlineFinishError("finish-outcome-conflict")
                _fault("after-close-write")
                state.update(state="route-closed", outcome_digest=_digest(json.dumps(outcome,sort_keys=True).encode()))
                _atomic(state_path, state)
                _fault("after-close")
            if binding["terminal_marker_digest"] != marker_digest:
                raise InlineFinishError("finish-marker-drift")
            if state["state"] in {"route-closed", "producer-sealed", "finished"}:
                try:
                    applied_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                if _digest(json.dumps(applied_outcome, sort_keys=True).encode()) != state.get("outcome_digest"):
                    raise InlineFinishError("finish-outcome-drift")
            if state["state"] == "route-closed":
                _fault("before-finalize")
                crash_manifest = os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == "after-manifest"
                artifact_producer.finalize_exact_cycle(root, cycle_id=record["cycle_id"],
                    expected_binding=binding, crash_after_manifest=crash_manifest)
                _fault("after-index")
                verified = artifact_producer.verify_finalized_cycle(root, cycle_id=record["cycle_id"], expected_binding=binding)
                state.update(state="producer-sealed", manifest_digest=verified["manifest_digest"]); _atomic(state_path, state)
                _fault("after-finalize")
            if state["state"] == "producer-sealed":
                _fault("before-receipt")
                verified = artifact_producer.verify_finalized_cycle(root, cycle_id=record["cycle_id"], expected_binding=binding)
                if verified.get("manifest_digest") != state.get("manifest_digest"): raise InlineFinishError("finish-seal-drift")
                receipt = {"schema":"finish_receipt_v1", "inline_finish_id":intent_id, "route_id":route["route_id"],
                           "route_hash":route["route_hash"], "cycle_id":record["cycle_id"],
                           "terminal_marker_digest":marker_digest, "manifest_digest":verified["manifest_digest"],
                           "commit":commit, "replay":replayed_at_entry}
                state.update(state="finished", receipt=receipt); _atomic(state_path, state)
                _fault("after-receipt")
            if state.get("state") != "finished": raise InlineFinishError("finish-pending")
            # A durable finished label is only a replay hint. Re-prove the
            # exact public marker, close outcome, and producer seal before
            # returning a successful receipt to any caller.
            if state.get("state") == "finished":
                try:
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                expected_binding_digest = _digest(json.dumps(binding, sort_keys=True,
                    separators=(",", ":")).encode())
                if (outcome.get("route_id") != route["route_id"]
                        or outcome.get("route_hash") != route["route_hash"]
                        or outcome.get("inline_finish_id") != intent_id
                        or outcome.get("terminal_marker_digest") != marker_digest
                        or outcome.get("summary_digest") != intent["summary_sha256"]
                        or outcome.get("producer_binding_digest") != expected_binding_digest
                        or outcome.get("head_commit") != commit
                        or outcome.get("terminal_gate_proven") is not True):
                    raise InlineFinishError("finish-outcome-conflict")
                verified = artifact_producer.verify_finalized_cycle(
                    root, cycle_id=record["cycle_id"], expected_binding=binding)
                if (verified.get("manifest_digest") != state.get("manifest_digest")
                        or state.get("receipt", {}).get("terminal_marker_digest") != marker_digest):
                    raise InlineFinishError("finish-seal-drift")
            result = dict(state["receipt"]); result["replay"] = replayed_at_entry
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def pending_state(root: Path, route_id: str):
    return _read(Path(root)/".runtime"/"inline-finish"/"v1"/route_id/"finish.json")

def pending_for_cycle(root: Path, cycle_id: str):
    """Find a cycle's exact finish slot, including a continuation route slot.

    The cycle record retains its begin route, so looking up only that route
    would miss a finish claimed by a verified continuation.
    """
    home = Path(root)/".runtime"/"inline-finish"/"v1"
    if not home.is_dir():
        return None
    matches = []
    for route_home in home.iterdir():
        if route_home.is_symlink() or not route_home.is_dir():
            raise InlineFinishError("finish-intent-unsafe")
        state = _read(route_home/"finish.json")
        if state and (state.get("intent") or {}).get("cycle_id") == cycle_id:
            matches.append(state)
    if len(matches) > 1:
        raise InlineFinishError("finish-cycle-ambiguous")
    return matches[0] if matches else None

def evidence_matches(path: Path, digest: str) -> bool:
    from dispatch_contract import evidence_digest
    return evidence_digest(path) == digest

def assert_not_pending(root: Path, route_id: str) -> None:
    state = pending_state(root, route_id)
    if state and state.get("state") != "finished": raise InlineFinishError("finish-in-progress")
