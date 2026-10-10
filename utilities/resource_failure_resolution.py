"""Resolve workflow consumption of an unchanged failed run, never its exit."""
from __future__ import annotations

import hashlib
from collections import deque
import json
import os
from pathlib import Path
import re
import shlex

import dispatch_resource_wait as OWNER_RESOURCE
import resource_run_registry as RR
import resource_resume
import workflow_state as WS

SHA = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


class UnresolvedResource(WS.WorkflowStateError):
    code = "resource-failure-unresolved"

    def __init__(self, node, reason, next_step):
        super().__init__(f"{node}:{reason}")
        self.next_step = next_step


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        before = stream_stat(stream)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
        if before != stream_stat(stream):
            raise ValueError("artifact-changed-during-read")
    if before != file_stat(path):
        raise ValueError("artifact-replaced-during-read")
    return value.hexdigest()


def stream_stat(stream):
    row = os.fstat(stream.fileno())
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


def file_stat(path):
    row = Path(path).stat()
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


class Snapshot:
    """Parse only the bytes whose hash was checked; reject changes before append."""
    def __init__(self):
        self.files = {}
        self.digests = {}

    def read(self, path, expected=None):
        path = Path(path)
        before = file_stat(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if before != file_stat(path) or (expected is not None and sha != expected):
            raise ValueError("bound-artifact-changed:" + str(path))
        old = self.files.setdefault(path, before)
        if old != before:
            raise ValueError("artifact-changed-during-resolution:" + str(path))
        self.digests[path] = sha
        return raw, sha

    def document(self, path, expected=None):
        raw, sha = self.read(path, expected)
        if Path(path).suffix == ".jsonl":
            value = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            value = json.loads(raw)
        return value, sha

    def check_digest(self, path, expected):
        path = Path(path)
        before = file_stat(path)
        if digest(path) != expected or file_stat(path) != before:
            raise ValueError("bound-artifact-changed:" + str(path))
        if self.files.setdefault(path, before) != before:
            raise ValueError("artifact-changed-during-resolution:" + str(path))
        self.digests[path] = expected

    def verify(self):
        if any(file_stat(path) != identity for path, identity in self.files.items()):
            raise ValueError("artifact-changed-before-resolution")


def hashes(value):
    """Read structured SHA fields, excluding prose and unrelated identifiers."""
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(hashes(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key.endswith("sha256") or
                    (key in {"expected", "actual"} and value.get("expected") == value.get("actual"))):
                match = SHA.fullmatch(item)
                if match:
                    result.add(match[1])
            if isinstance(item, dict) and ("hash" in key or key == "artifact_sha256"):
                result.update(match[1] for text in item.values() if isinstance(text, str)
                              and (match := SHA.fullmatch(text)))
            result.update(hashes(item))
    return result


def references(value):
    """Existing path/hash manifests can carry an independent admission transitively."""
    result = []
    if isinstance(value, list):
        for item in value:
            result.extend(references(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and ("hash" in key or key == "artifact_sha256"):
                result.extend((name, SHA.fullmatch(sha)[1]) for name, sha in item.items()
                              if isinstance(sha, str) and SHA.fullmatch(sha))
            result.extend(references(item))
        path = value.get("path", value.get("evidence_path", value.get("source")))
        sha = value.get("sha256", value.get("input_sha256", value.get("source_sha256")))
        if sha is None and value.get("expected") == value.get("actual"):
            sha = value.get("actual")
        if isinstance(path, str) and isinstance(sha, str) and SHA.fullmatch(sha):
            result.append((path.split("#", 1)[0], SHA.fullmatch(sha)[1]))
    return result


def admitted_hashes(base, document, snapshot, targets=()):
    pending = deque([document])
    seen = set()
    bindings = set()
    while pending:
        value = pending.popleft()
        bindings.update(hashes(value))
        if set(targets) & bindings:
            return bindings
        for name, sha in references(value):
            path = Path(name)
            path = (path if path.is_absolute() else base / path).resolve()
            if not path.is_relative_to(base) or path.suffix not in {".json", ".jsonl"}:
                continue
            if (path, sha) in seen:
                continue
            seen.add((path, sha))
            child, _ = snapshot.document(path, sha)
            pending.append(child)
    return bindings


def contained(base, name):
    path = Path(name)
    path = path if path.is_absolute() else base / path
    path = path.resolve(strict=True)
    if not path.is_relative_to(base):
        raise ValueError("artifact-outside-resource-cycle")
    return path


def descendants(route, predecessor):
    result = []
    reached = {predecessor}
    pending = list(route["nodes"])
    while pending:
        next_pending = []
        for node in pending:
            if node["id"] in reached:
                continue
            if any(dep in reached for dep in node.get("depends_on", [])):
                reached.add(node["id"])
                result.append(node)
            else:
                next_pending.append(node)
        if len(next_pending) == len(pending):
            break
        pending = next_pending
    return result


def retry_step(route, armed, jobs, reason):
    """The existing compose surface opens a new leg; no caller input file needed."""
    nodes = [armed["node"], *[node["id"] for node in descendants(route, armed["node"])]]
    run_id = armed["predecessor_id"]
    base = re.sub(r"__a[1-9][0-9]*$", "", run_id)
    try:
        runs = json.loads(Path(armed.get("resource_registry") or "").read_text())["runs"]
    except (OSError, ValueError, KeyError, TypeError):
        runs = {}  # The new cycle uses a separate registry and output directory.
    if not isinstance(runs, dict):
        runs = {}
    ordinal = 1
    while f"{base}__a{ordinal}" in runs:
        ordinal += 1
    retry = f"{base}__a{ordinal}"
    task = (f"Continue the approved task from {route['route_id']}. Resource {run_id} remains failed: {reason}. "
            f"Retain its registry, sentinel, outputs and all historical PASS/FAIL records unchanged. "
            f"Reuse its checked configuration and smoke inputs, run a distinct resource attempt {retry} "
            "in this new cycle's output directory, and revalidate every selected downstream stage against "
            "the new output bytes. Historical PASS markers do not complete this leg. Original task:\n"
            + str((route.get("work_request") or {}).get("text") or route.get("slug", "")))
    argv = ["hearting", "run", "capability-route", "compose", "--start", "--shape", "staged",
            "--capability", route["capability"], "--capability-mode", route["capability_mode"],
            "--intensity", route["effective_intensity"], "--graph", ",".join(nodes),
            # A pipe can be read only once. Supplying the existing slug option
            # keeps compose's slug inference from consuming the task first.
            "--slug", f"{route['route_id']}-{armed['node']}-retry-a{ordinal}",
            "--cwd", route["cwd"], "--artifact-root", route["artifact_root"],
            "--jobs", str(jobs), "--prompt-file", "/dev/stdin"]
    if route.get("campaign_key"):
        argv += ["--campaign-key", route["campaign_key"]]
    else:
        argv += ["--unassigned"]
    return {"command": shlex.join(["printf", "%s", task]) + " | " + shlex.join(argv),
            "run_id": retry, "revalidate_nodes": nodes[1:],
            "meaning": "원본 실패를 보존하고 새 실행의 산출물로 후속 단계를 다시 검증합니다."}


def prove(route, ledger, armed, failure, gates, jobs, module, *, snapshots=None):
    snapshot = Snapshot()
    observed_armed, _ = snapshot.document(ledger.root / "armed" / f"{armed['node']}.json")
    if observed_armed != armed:
        raise ValueError("resource-watch-changed")
    node_id = armed["node"]
    node = WS.route_node(route, node_id)
    if (node is None or node.get("kind") != "resource-runner"
            or armed.get("route_id") != route["route_id"]
            or armed.get("route_hash") != route["route_hash"]
            or armed.get("jobs") != str(Path(jobs).resolve())):
        raise ValueError("resource-watch-binding-mismatch")
    registry, _ = snapshot.document(armed["resource_registry"])
    row = registry["runs"][armed["predecessor_id"]]
    sentinel, _ = snapshot.read(row["sentinel"])
    identity = f"{row['run_id']}:{row.get('pid')}:{row.get('starttime')}:{row.get('exit_code')}"
    if (row.get("route") != armed.get("route_file") or row.get("node") != node_id
            or row.get("jobs") != armed.get("jobs") or row.get("status") != "failed"
            or type(row.get("exit_code")) is not int or row["exit_code"] == 0
            or row.get("cancel_requested") or row.get("parent_close_requested")
            or RR.classify_identity(row)[0] != "exited"
            or sentinel.decode().strip() != str(row["exit_code"])
            or failure.get("identity") != identity
            or failure.get("resource_sha256") != resource_resume.row_digest(row)
            or failure.get("exit_code") != row["exit_code"]
            or armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(row)):
        raise ValueError("failed-execution-changed")
    chain = descendants(route, node_id)
    if not any(n.get("terminal") and gates.get(n["id"], {}).get("passed") is True for n in chain):
        raise ValueError("no-passed-downstream-terminal")
    directory = module.completion_dir(route["route_id"], jobs=jobs)
    markers = {}
    proofs = {}
    independent = []
    for part in [node, *chain]:
        key = part["id"]
        raw, marker_sha = snapshot.read(directory / f"{key}.json")
        marker = json.loads(raw)
        proof = module._marker_identity_row(route, part, key,
            part.get("terminal_gate") if part.get("terminal") else part.get("completion_gate"), jobs=jobs,
            exact_terminal=part.get("kind") != "resource-runner")
        if proof.get("passed") is not True:
            raise ValueError(f"downstream-marker-unproven:{key}:{proof.get('reason')}")
        markers[key] = marker
        proofs[key] = {"marker_sha256": marker_sha,
                       "evidence": marker["evidence"]}
        if key != node_id and marker.get("review_independence") == "independent":
            independent.append(key)
    if not independent:
        raise ValueError("independent-pass-absent")
    base = Path(armed["artifact_base"]).resolve(strict=True)
    documents = {}
    for key, marker in markers.items():
        evidence = marker["evidence"]
        if Path(evidence['path']).suffix in {'.json', '.jsonl'}:
            documents[key], _ = snapshot.document(evidence['path'], evidence['sha256'])
        else:
            snapshot.read(evidence['path'], evidence['sha256'])
            documents[key] = {}
    judgment = documents[node_id]
    if (judgment.get("gate_evidence_verdict", judgment.get("verdict")) != "PASS"
            or judgment.get("route_id") != route["route_id"]
            or judgment.get("node", judgment.get("node_id")) != node_id):
        raise ValueError("resource-judgment-not-pass")
    receipt_path = contained(base, "run.json")
    receipt, receipt_sha = snapshot.document(receipt_path)
    if (receipt.get("route_id") != route["route_id"] or receipt.get("exit") != row["exit_code"]
            or receipt.get("config_sha256") != row.get("config_sha256")
            or not receipt.get("outputs")):
        raise ValueError("failed-output-receipt-unbound")
    produced = {contained(base, item["path"]): item["sha256"] for item in receipt["outputs"]}
    consumed = {}
    admitted = set()
    consumers = {markers[p['id']]['evidence']['sha256'] for p in chain
                 if node_id in p.get('depends_on', [])}
    for key in independent:
        if documents[key].get("verdict") != "PASS":
            raise ValueError("independent-verdict-not-pass")
        admitted.update(admitted_hashes(base, documents[key], snapshot, consumers))
    for part in chain:
        if node_id not in part.get("depends_on", []):
            continue
        evidence_path = contained(base, markers[part["id"]]["evidence"]["path"])
        if markers[part["id"]]["evidence"]["sha256"] not in admitted:
            continue
        document = documents[part["id"]]
        bindings = hashes(document)
        # A marker-bound summary may bind a declared JSON/JSONL sidecar.
        for name in part.get("outputs", []):
            if any(c in name for c in "*?[") or Path(name).suffix not in {".json", ".jsonl"}:
                continue
            path = contained(base, name)
            if path == evidence_path:
                continue
            sidecar_sha = digest(path)
            if sidecar_sha in bindings:
                sidecar, _ = snapshot.document(path, sidecar_sha)
                bindings.update(hashes(sidecar))
        if receipt_sha not in bindings:
            continue
        consumed.update({path: expected for path, expected in produced.items() if expected in bindings})
    if not consumed:
        raise ValueError("same-byte-consumer-binding-absent")
    # The accepted correction also names preserved artifacts. Do not substitute
    # corrected bytes for any originally produced bytes, even when PASS is kept.
    for name, expected in judgment.get("artifact_sha256", {}).items():
        path = contained(base, name)
        if path in produced and produced[path] != expected:
            raise ValueError("correction-output-binding-mismatch")
        snapshot.check_digest(path, expected)
    for path, expected in consumed.items():
        snapshot.check_digest(path, expected)
    snapshot.verify()
    if snapshots is not None:
        snapshots.append(snapshot)
    return {"basis": "downstream-independent-verification", "identity": identity,
            "resource_sha256": failure["resource_sha256"], "original_failure": failure,
            "run": {"path": str(receipt_path), "sha256": receipt_sha},
            "consumed_artifacts": [{"path": str(path), "sha256": sha} for path, sha in sorted(consumed.items())],
            "checked_artifacts": [{"path": str(path), "sha256": sha}
                                  for path, sha in sorted(snapshot.digests.items())],
            "stage_chain": proofs, "independent_nodes": independent}


def reconcile(route, ledger, gates, jobs, module):
    """Validate all candidates first; append only legal, restartable transitions."""
    current = ledger.state()
    if current["workflow_state"] not in {"CREATED", "READY", "RUNNING", "FAILED_RETRYABLE"}:
        return []
    if not gates or any(row.get("passed") is not True for row in gates.values()):
        return []
    planned = {}
    retries = {}
    snapshots = []
    for node_id, stage in current["nodes"].items():
        prior = (stage.get("evidence") or {}).get("resolved_resource_failure")
        if stage["state"] != "FAILED_RETRYABLE" and not prior:
            continue
        failure = prior["original_failure"] if prior else stage.get("evidence") or {}
        if failure.get("exit_code") in {None, 0}:
            continue  # Existing successful-exit/missing-output recovery owns this.
        node = WS.route_node(route, node_id)
        if not node or node.get('kind') != 'resource-runner':
            continue
        path = ledger.root / "armed" / f"{node_id}.json"
        retry_armed = {'node': node_id, 'predecessor_id':
                 str(failure.get('identity') or f"{route['route_id']}__{node_id}").split(':')[0]}
        try:
            armed = json.loads(path.read_text())
            if not isinstance(armed, dict):
                raise ValueError('resource-watch-unreadable')
            if isinstance(armed.get('resource_registry'), str):
                retry_armed['resource_registry'] = armed['resource_registry']
            if armed.get("predecessor_kind") != "resource":
                raise ValueError('resource-watch-binding-mismatch')
            planned[node_id] = prove(route, ledger, armed, failure, gates, jobs, module, snapshots=snapshots)
            retries[node_id] = retry_armed
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UnresolvedResource(node_id, str(exc), retry_step(route, retry_armed, jobs, str(exc))) from exc
    if any(stage["state"] in WS.vocabulary(ledger.registry_path)["failure_states"]
           and node not in planned for node, stage in current["nodes"].items()):
        return []
    for node, snapshot in zip(planned, snapshots):
        try:
            snapshot.verify()
        except (OSError, ValueError) as exc:
            raise UnresolvedResource(node, str(exc), retry_step(route, retries[node], jobs, str(exc))) from exc
    for node, proof in planned.items():
        evidence = {"resolved_resource_failure": proof}
        state = ledger.state()["nodes"][node]["state"]
        if state == "FAILED_RETRYABLE":
            ledger.record(node, "READY", evidence=evidence, actor="resource-failure-resolution")
            state = "READY"
        for step in WS.completion_transition_path(state, "STAGE_SUCCEEDED", ledger.registry_path):
            ledger.record(node, step, evidence=evidence, actor="resource-failure-resolution")
    workflow_failure = next((row for row in reversed(ledger.journal()) if row.get("workflow_state")), {})
    if (planned and ledger.state()["workflow_state"] == "FAILED_RETRYABLE"
            and (workflow_failure.get("evidence") or {}).get("node") in planned):
        ledger.set_workflow_state("READY", evidence={"resolved_resource_failures": list(planned)},
                                  actor="resource-failure-resolution")
    return list(planned)
