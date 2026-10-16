"""Runtime-authored resource facts; scientific judgment stays with verification."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from resource_run_registry import strict_json_loads

KEY = "hearting_resource_runs"


def outputs(armed):
    names = []
    for name in armed.get("declared_outputs") or []:
        if not isinstance(name, str) or any(c in name for c in "*?["):
            continue
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("resource-output-path-invalid:" + name)
        if path.suffix and name not in names:
            names.append(name)
    return names


def paths(armed, *, prepare=False):
    base = armed.get("artifact_base")
    if not base:
        return {"expected_outputs": [], "runtime_output": None}
    base = Path(base)
    expected = [base / name for name in outputs(armed)]
    runtime = base / "run.json"
    route_file = armed.get("route_file")
    node_id = armed.get("node")
    if route_file and node_id and Path(route_file).is_file():
        try:
            route = json.loads(Path(route_file).read_text(encoding="utf-8"))
            if (route.get("route_id") != armed.get("route_id")
                    or route.get("route_hash") != armed.get("route_hash")):
                raise ValueError("resource-output-route-binding-mismatch")
            node = next((item for item in route.get("nodes", []) if item.get("id") == node_id), None)
            if node is None:
                raise ValueError("resource-output-node-binding-mismatch")
            from artifact_context import ArtifactContext
            context = ArtifactContext(
                Path(route.get("artifact_root")) if route.get("artifact_root") else None,
                route, node, node_id, None, None, base, None, "resource-run",
            )
            binding = context.resolve_output("run-record")
            runtime = binding.path
            if runtime not in expected:
                raise ValueError("resource-run-output-not-declared")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("resource-output-route-invalid:" + str(exc)) from exc
    elif any(path.name == "run.json" and path.parent == base for path in expected):
        # Older native armed records may not retain a readable route file.
        # Their declared root run.json contract remains sufficient.
        runtime = base / "run.json"
    elif route_file and node_id:
        raise ValueError("resource-output-route-invalid:route file unavailable")
    for path in [runtime, *expected]:
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("resource-output-path-escape:" + str(path))
        if path.is_symlink():
            raise ValueError("resource-output-symlink:" + str(path))
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError("resource-output-not-regular:" + str(path))
        if prepare:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Check an actual create, rather than assuming an access bit grants it.
            fd, probe = tempfile.mkstemp(prefix=".resource-write-", dir=path.parent)
            os.close(fd)
            os.unlink(probe)
    if prepare and runtime.exists():
        read_document(runtime)
    return {"expected_outputs": [str(path) for path in expected], "runtime_output": str(runtime)}


def read_document(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}, None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("resource-run-document-not-regular")
        raw = stream.read()
    document = strict_json_loads(raw)
    if not isinstance(document, dict):
        raise ValueError("resource-run-document-not-object")
    return document, (info.st_dev, info.st_ino, raw)


def runtime_only(document):
    facts = document.get(KEY)
    return (set(document) == {KEY} and isinstance(facts, dict)
            and facts.get("producer") == "hearting" and facts.get("schema_version") == 1
            and isinstance(facts.get("runs"), dict))


def write(armed, row, *, preserve=False):
    receipt = paths(armed)
    if not receipt["runtime_output"]:
        return {**receipt, "runtime_only": False}
    path = Path(receipt["runtime_output"])
    path.parent.mkdir(parents=True, exist_ok=True)
    document, preimage = read_document(path)
    # Existing owner documents remain their scientific artifact, byte for byte.
    # The caller has revalidated their exact execution against the runner registry
    # and sentinel; runtime-created documents carry the facts themselves.
    if preserve:
        if preimage is None:
            raise ValueError("resource-completion-evidence-missing")
        return {**receipt, "runtime_only": runtime_only(document)}
    if preimage is not None and not runtime_only(document):
        return {**receipt, "runtime_only": False}
    namespace = document.setdefault(KEY, {"schema_version": 1, "producer": "hearting", "runs": {}})
    if (not isinstance(namespace, dict) or namespace.get("producer") != "hearting"
            or namespace.get("schema_version") != 1 or not isinstance(namespace.get("runs"), dict)):
        raise ValueError("resource-run-namespace-conflict")
    facts = {key: row.get(key) for key in (
        "run_id", "pid", "starttime", "command_hash", "pid_namespace", "process_group",
        "command", "cwd", "log", "sentinel", "status", "exit_code", "started_at", "ended_at",
        "failure_class", "parent_attempt_id", "config_ref", "config_sha256", "source_commit")}
    facts.update(route_id=armed["route_id"], route_hash=armed.get("route_hash"),
                 node=armed["node"], declared_outputs=receipt["expected_outputs"])
    prior = namespace["runs"].get(row["run_id"])
    if prior is not None and prior != facts:
        raise ValueError("resource-run-execution-conflict")
    namespace["runs"][row["run_id"]] = facts
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if preimage is not None and raw == preimage[2]:
        return {**receipt, "runtime_only": runtime_only(document)}
    fd, temporary = tempfile.mkstemp(prefix=".resource-run-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _, current = read_document(path)
        if current != preimage:
            raise ValueError("resource-run-document-changed")
        if preimage is None:
            # Publication never overwrites a file arriving since the observation.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {**receipt, "runtime_only": runtime_only(document)}
