#!/usr/bin/env python3
"""Isolated production-entry CPU and invalidation fixture for runtime observers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pstats
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
JOBS_BYTES = 23_800_000
INDEX_BYTES = 91_000_000
MANIFEST_BYTES = 1_400_000
DIAGNOSTIC_PROFILE = os.environ.get("HEARTING_CPU_DIAGNOSTIC") == "profile"
BASELINE_MODE = os.environ.get("HEARTING_CPU_BASELINE") == "1"
OBSERVER_COUNT = 1 if DIAGNOSTIC_PROFILE else 8
WATCHER_COUNT = 1 if DIAGNOSTIC_PROFILE else 11
RUNNER_COUNT = 1
IDLE_SECONDS = 3 if DIAGNOSTIC_PROFILE else 20
BURST_SECONDS = 3 if DIAGNOSTIC_PROFILE else 20
COLD_SECONDS = 5 if DIAGNOSTIC_PROFILE else 10


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
if argv[:1] == ["--session"]:
    argv = argv[2:]
verb = argv[1] if len(argv) > 1 else ""
target = argv[2] if len(argv) > 2 else "fixture-target"
log = os.environ["FIXTURE_HERDR_LOG"]
with open(log, "a", encoding="utf-8") as out:
    out.write(f"{os.getpid()} {verb} {target}\n")
if verb == "wait":
    with open(os.environ["FIXTURE_HERDR_FIFO"], "rb") as source:
        source.read(1)
payload = {"id": "fixture-herdr", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": "sid-fixture"},
    "agent_status": "idle", "name": target, "pane_id": "w1:p9"
}, "type": "agent_info"}}
print(json.dumps(payload))
'''


class FakeObservationEndpoint:
    """Small newline JSON server implementing the two observed protocol methods."""

    def __init__(self, path: Path, panes: list[dict]):
        self.path = path
        self.panes = panes
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(64)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.subscriptions: list[socket.socket] = []
        self.requests = {"events.subscribe": 0, "session.snapshot": 0}
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    @staticmethod
    def _read_line(connection):
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return None
            data.extend(chunk)
        return json.loads(bytes(data).split(b"\n", 1)[0])

    @staticmethod
    def _send(connection, payload):
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())

    def _serve(self, connection):
        try:
            request = self._read_line(connection)
            if not isinstance(request, dict):
                return
            method = request.get("method")
            with self.lock:
                self.requests[method] = self.requests.get(method, 0) + 1
            response = {"id": request.get("id")}
            if method == "events.subscribe":
                response["result"] = {"subscribed": True}
                self._send(connection, response)
                connection.settimeout(0.2)
                with self.lock:
                    self.subscriptions.append(connection)
                while not self.stop.is_set():
                    try:
                        if not connection.recv(1):
                            break
                    except socket.timeout:
                        continue
            elif method == "session.snapshot":
                response["result"] = {"type": "session_snapshot", "snapshot": {
                    "protocol": 22, "panes": self.panes, "agents": []}}
                self._send(connection, response)
            else:
                response["error"] = {"code": "unknown_method"}
                self._send(connection, response)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            with self.lock:
                if connection in self.subscriptions:
                    self.subscriptions.remove(connection)
            with contextlib.suppress(OSError):
                connection.close()

    def _accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def publish_unrelated(self):
        frame = (json.dumps({"event": "pane.updated", "data": {
            "pane_id": "w99:p999", "pane": {"pane_id": "w99:p999", "agent": "claude"}
        }}, separators=(",", ":")) + "\n").encode()
        failed = []
        with self.lock:
            for connection in self.subscriptions:
                try:
                    connection.sendall(frame)
                except OSError:
                    failed.append(connection)
            for connection in failed:
                self.subscriptions.remove(connection)

    def close(self):
        self.stop.set()
        with contextlib.suppress(OSError):
            self.listener.close()
        with self.lock:
            connections = list(self.subscriptions)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        self.thread.join(timeout=1)


def _import_from(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _proc_stat(pid: int):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if not tail or tail[0] in {"Z", "X"}:
            return None
        return {
            "pid": pid, "ppid": int(tail[1]), "pgrp": int(tail[2]),
            "utime": int(tail[11]), "stime": int(tail[12]),
            "start": tail[19], "rss_pages": int(tail[21]),
        }
    except (OSError, ValueError, IndexError):
        return None


def _process_tree_snapshot(seed_pids: set[int]):
    rows = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        item = _proc_stat(int(name))
        if item is not None:
            rows[item["pid"]] = item
    groups = {rows[pid]["pgrp"] for pid in seed_pids if pid in rows}
    selected = {pid for pid, item in rows.items()
                if pid in seed_pids or item["pgrp"] in groups}
    ancestors = set(seed_pids)
    changed = True
    while changed:
        changed = False
        for pid, item in rows.items():
            if item["ppid"] in ancestors and pid not in selected:
                selected.add(pid)
                ancestors.add(pid)
                changed = True
    result = {}
    for pid in selected:
        item = rows.get(pid)
        if item is None:
            continue
        key = (pid, item["start"])
        try:
            io = Path(f"/proc/{pid}/io").read_text().splitlines()
            rchar = int(next(line.split()[1] for line in io if line.startswith("rchar:")))
        except (OSError, ValueError, StopIteration):
            rchar = 0
        item["rchar"] = rchar
        result[key] = item
    return result


def _sample_window(seconds: float, seed_pids: set[int], seed_provider=None):
    start = time.monotonic()
    start_epoch = time.time()
    sampler_start = _proc_stat(os.getpid())
    refresh_roots_at = start
    previous = _process_tree_snapshot(seed_pids)
    ticks = 0
    read_chars = 0
    max_rss = 0
    peak_processes = len(previous)
    identities = set(previous)
    while time.monotonic() - start < seconds:
        time.sleep(0.05)
        now = time.monotonic()
        if seed_provider is not None and now >= refresh_roots_at:
            seed_pids.update(seed_provider())
            refresh_roots_at = now + 1.0
        current = _process_tree_snapshot(seed_pids)
        peak_processes = max(peak_processes, len(current))
        identities.update(current)
        for key, item in current.items():
            before = previous.get(key)
            if before is None:
                ticks += item["utime"] + item["stime"]
                read_chars += item["rchar"]
            else:
                ticks += max(0, item["utime"] + item["stime"]
                             - before["utime"] - before["stime"])
                read_chars += max(0, item["rchar"] - before["rchar"])
            max_rss = max(max_rss, item["rss_pages"] * os.sysconf("SC_PAGE_SIZE"))
        previous = current
    elapsed = time.monotonic() - start
    end_epoch = time.time()
    hz = os.sysconf("SC_CLK_TCK")
    sampler_end = _proc_stat(os.getpid())
    sampler_ticks = 0
    if sampler_start is not None and sampler_end is not None:
        sampler_ticks = max(0, sampler_end["utime"] + sampler_end["stime"]
                            - sampler_start["utime"] - sampler_start["stime"])
    return {
        "elapsed_seconds": elapsed, "start_epoch": start_epoch,
        "end_epoch": end_epoch, "cpu_ticks": ticks,
        "cpu_percent_one_core": 100.0 * ticks / (hz * elapsed),
        "sampler_cpu_ticks": sampler_ticks,
        "sampler_cpu_percent_one_core": 100.0 * sampler_ticks / (hz * elapsed),
        "rchar_bytes": read_chars, "max_sampled_rss_bytes": max_rss,
        "peak_processes": peak_processes,
        "pid_start_identities": [list(item) for item in sorted(identities)],
    }


def _make_source_copy(destination: Path):
    shutil.copytree(
        SOURCE_ROOT, destination,
        ignore=shutil.ignore_patterns(
            ".git", ".agent_reports", ".claude_reports", ".dispatch",
            "__pycache__", "node_modules", "dist", "coverage"),
        symlinks=True,
    )
    if not (destination / "RELEASE_VERSION").is_file():
        (destination / "RELEASE_VERSION").write_text("fixture\n", encoding="utf-8")
    if not (destination / "harness-manifest.json").is_file():
        (destination / "harness-manifest.json").write_text("{}\n", encoding="utf-8")


def _valid_project(project_root: Path, *, large_manifest: bool,
                   large_index: bool, modules: dict):
    admission = modules["admission"]
    identity = admission.ensure_root_identity(project_root)
    fixture = modules["admission_fixture"]
    allocator = modules["identity"].IdAllocator()
    doc, content = fixture._make_valid_document(allocator, identity, content=b"payload\n")
    manifest = modules["manifest"]
    target_rows = 3200 if large_manifest else 8
    digest = manifest.digest_bytes(content)
    provenance = {
        "source_manifest_id": doc["manifest_id"],
        "source_revision_id": doc["manifest_revision_id"],
        "producer_route_id": "fixture-route",
        "algorithm_version": "fixture-v1",
        "schema_version": 1,
        "source_digest": "sha256:" + "2" * 64,
    }
    for index in range(1, target_rows):
        artifact_id = allocator.allocate("artifact")
        revision_id = allocator.allocate("artifact_revision")
        doc["artifacts"].append({
            "artifact_id": artifact_id, "cycle_id": doc["cycle"]["cycle_id"],
            "role": "supporting", "type": "doc", "capability": "fixture",
            "title": f"payload-{index}",
        })
        doc["artifact_revisions"].append({
            "artifact_revision_id": revision_id, "artifact_id": artifact_id,
            "revision_sequence": 1, "content_digest": digest,
            "byte_size": len(content), "media_type": "text/plain",
            "locator": {"kind": "cycle-relative", "path": f"artifacts/payload-{index:05d}.txt"},
            "provenance": dict(provenance),
        })
    report = manifest.validate(doc)
    if not report.ok:
        raise AssertionError(f"generated manifest is invalid: {report.to_payload()}")
    raw_manifest = manifest.canonical_bytes(doc)
    if large_manifest and len(raw_manifest) < MANIFEST_BYTES:
        raise AssertionError(f"generated manifest is only {len(raw_manifest)} bytes")
    directory = project_root / "campaigns" / doc["campaign"]["campaign_id"] \
        / "cycles" / doc["cycle"]["cycle_id"]
    payload_dir = directory / "artifacts"
    payload_dir.mkdir(parents=True)
    for index in range(1, target_rows):
        (payload_dir / f"payload-{index:05d}.txt").write_bytes(content)
    (directory / "plan.md").write_bytes(content)
    (directory / "manifest.json").write_bytes(raw_manifest)

    cycle_path = os.path.relpath(directory, project_root)
    index_payload = {
        "schema_version": 1, "artifact_root_id": identity.artifact_root_id,
        "stable_ids": {}, "routes": {}, "event_ids": {}, "streams": {},
        "manifests": {doc["cycle"]["cycle_id"]: {
            "manifest_id": doc["manifest_id"],
            "manifest_revision_id": doc["manifest_revision_id"],
            "cycle_id": doc["cycle"]["cycle_id"],
            "manifest_digest": manifest.manifest_digest(doc),
            "idempotency_key": doc["cycle"]["cycle_id"],
        }},
        "cycles": {doc["cycle"]["cycle_id"]: {
            "campaign_id": doc["campaign"]["campaign_id"],
            "cycle_path": cycle_path,
            "manifest_digest": manifest.manifest_digest(doc),
        }},
    }
    for row in doc["artifacts"]:
        index_payload["stable_ids"][row["artifact_id"]] = {
            "kind": "artifact", "cycle_id": doc["cycle"]["cycle_id"],
            "manifest_id": doc["manifest_id"],
        }
    if large_index:
        cycle_id = doc["cycle"]["cycle_id"]
        manifest_id = doc["manifest_id"]
        count = 560_000
        for number in range(count):
            key = f"artifact_{number:032x}"
            index_payload["stable_ids"].setdefault(key, {
                "kind": "artifact", "cycle_id": cycle_id,
                "manifest_id": manifest_id,
            })
    admission_dir = project_root / ".runtime" / "artifact-admission" / "v1"
    admission_dir.mkdir(parents=True, exist_ok=True)
    raw_index = json.dumps(index_payload, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    if large_index and len(raw_index) < INDEX_BYTES:
        raise AssertionError(f"generated index is only {len(raw_index)} bytes")
    (admission_dir / "index.json").write_bytes(raw_index + b"\n")
    loaded = admission.load_index(project_root)
    if len(loaded.stable_ids) < target_rows:
        raise AssertionError("generated index did not retain its stable-id rows")
    return {
        "root": str(project_root), "artifact_root_id": identity.artifact_root_id,
        "campaign_id": doc["campaign"]["campaign_id"],
        "cycle_id": doc["cycle"]["cycle_id"], "manifest_id": doc["manifest_id"],
        "manifest_bytes": len(raw_manifest), "index_bytes": len(raw_index) + 1,
        "manifest_validation": "passed", "index_rows": len(loaded.stable_ids),
    }


def _valid_sealed_project(project_root: Path, temp_root: Path, modules: dict):
    """Create a real completed producer cycle and verify its sealed history."""
    producer_fixture = modules["producer_fixture"]
    producer = modules["producer"]
    admission = modules["admission"]
    artifact_index = modules["artifact_index"]
    project_root.mkdir(parents=True, exist_ok=True)
    producer.activate(
        project_root, repository_id=producer_fixture.REPO_ID,
        artifact_root_id=producer_fixture.ROOT_ID,
        w7={"campaign_id": "camp_" + "c" * 32},
    )
    route = producer_fixture.compile_for(
        "direct", project_root, slug="runtime-observation-cpu-fixture")
    binding = producer_fixture.L.admit_runtime_route(project_root, route)
    route_file = Path(binding.route_file)
    begun = producer.begin(
        project_root, route_file=route_file, capability=route["capability"],
        intensity=route["effective_intensity"],
    )
    cycle_dir = Path(begun["cycle_dir"])
    payload_dir = cycle_dir / "artifacts"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload = b"runtime observation fixture payload\n"
    for index in range(3200):
        (payload_dir / f"fixture-payload-{index:05d}.txt").write_bytes(payload)

    evidence = temp_root / "terminal-evidence.md"
    evidence.write_text("isolated completed-cycle fixture evidence\n", encoding="utf-8")
    for node in route["nodes"]:
        if node.get("terminal") is not True:
            continue
        if node.get("dispatch_depth", 0) == 0:
            producer_fixture.R.write_completion_marker(route, node, node["id"], evidence)
        else:
            metadata = {
                "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": 1, "fallback_hop": "same-harness-headless",
            }
            producer_fixture.R.write_completion_marker(
                route, node, node["id"], evidence,
                attempt_id=f"att-cpu-fixture-{node['id']}", attempt_metadata=metadata)
    outcome, _generation = producer_fixture.R.close_route(
        route, route_file, commit="a" * 40, summary="isolated CPU fixture")
    if not outcome.get("terminal_gate_proven"):
        raise AssertionError(f"fixture route did not close: {outcome}")
    sealed = producer.finalize(project_root, cycle_id=begun["cycle_id"])
    if sealed.get("status") not in {"sealed", "already-sealed"}:
        raise AssertionError(f"fixture cycle did not seal: {sealed}")

    record = producer.read_cycle_record(project_root, begun["cycle_id"])
    manifest_path = producer._record_cycle_manifest_path(project_root, record)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_bytes = manifest_path.stat().st_size
    if manifest_bytes < MANIFEST_BYTES:
        raise AssertionError(f"producer manifest is only {manifest_bytes} bytes")

    index_doc = admission.load_index(project_root)
    payload_doc = artifact_index.to_payload(index_doc)
    cycle_id = begun["cycle_id"]
    manifest_id = index_doc.manifests[cycle_id]["manifest_id"]
    stable_ids = payload_doc["stable_ids"]
    stable_row = {"kind": "artifact", "cycle_id": cycle_id,
                  "manifest_id": manifest_id}
    for number in range(560_000):
        stable_ids.setdefault(f"artifact_{number:032x}", stable_row)
    raw_index = json.dumps(payload_doc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    if len(raw_index) < INDEX_BYTES:
        raise AssertionError(f"inflated schema-valid index is only {len(raw_index)} bytes")
    index_path = admission._index_path(project_root)
    index_path.write_bytes(raw_index + b"\n")

    context = getattr(producer, "_HISTORICAL_OBSERVATIONS", None)
    if context is not None:
        context.clear()
        before = context.metrics_snapshot()
    expected = {"route_id": route["route_id"], "route_hash": route["route_hash"]}
    verified = producer._verify_sealed_cycle_locked(
        project_root, record, expected_binding=expected, historical=True)
    if verified.get("status") != "already-sealed":
        raise AssertionError(f"historical sealed-cycle verification failed: {verified}")
    if context is not None:
        producer._verify_sealed_cycle_locked(
            project_root, record, expected_binding=expected, historical=True)
        after = context.metrics_snapshot()
        verification_metrics = {key: after[key] - before[key] for key in after}
    else:
        verification_metrics = {"legacy_historical_verify_calls": 1}

    return {
        "root": str(project_root),
        "artifact_root_id": admission.ensure_root_identity(project_root).artifact_root_id,
        "campaign_id": document["campaign"]["campaign_id"],
        "cycle_id": cycle_id, "manifest_id": manifest_id,
        "manifest_revision_id": document["manifest_revision_id"],
        "manifest_path": str(manifest_path),
        "manifest_bytes": manifest_bytes,
        "index_bytes": index_path.stat().st_size,
        "manifest_validation": "producer-sealed-and-historically-verified",
        "index_rows": len(stable_ids),
        "historical_verification": verified,
        "historical_verification_metrics_delta": verification_metrics,
    }


def _row_text(index: int, jobs_path: Path, project_root: Path,
              worker_identities: list[dict[str, str]]):
    if index < OBSERVER_COUNT:
        attempt_id = f"att-observer-{index:02d}"
        parent_id = f"att-parent-{index:02d}"
        status = "running"
    else:
        attempt_id = f"att-history-{index:08d}"
        parent_id = "att-history-parent"
        status = "done"
    fields = [
        "2026-10-18T00:00:00Z", status, str(project_root), str(project_root),
        f"history-{index:08d}",
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,"
        f"attempt_id={attempt_id},parent_attempt_id={parent_id},harness=codex,",
    ]
    if index < OBSERVER_COUNT:
        fields[-1] += ",".join(f"{key}={value}" for key, value in worker_identities[index].items()) + ","
    fields[-1] += "note="
    prefix = "\t".join(fields)
    desired = 400
    pad = max(0, desired - len(prefix.encode("utf-8")) - 1)
    line = prefix + ("x" * pad) + "\n"
    return line


def _write_jobs(jobs_path: Path, project_root: Path,
                worker_identities: list[dict[str, str]]):
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    rows = (JOBS_BYTES // 400) + 1024
    with jobs_path.open("w", encoding="utf-8", newline="") as handle:
        for index in range(rows):
            handle.write(_row_text(index, jobs_path, project_root, worker_identities))
    while jobs_path.stat().st_size < JOBS_BYTES:
        with jobs_path.open("a", encoding="utf-8", newline="") as handle:
            for index in range(rows, rows + 1024):
                handle.write(_row_text(index, jobs_path, project_root, worker_identities))
        rows += 1024


def _wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def _active_observer_processes(jobs_path: Path):
    active = {}
    expected_jobs = str(jobs_path)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                errors="replace").split("\0")
        except OSError:
            continue
        if ("--resume-obligation" not in argv or "--jobs" not in argv
                or "codex-managed-completion.py" not in " ".join(argv)):
            continue
        jobs_index = argv.index("--jobs") + 1
        duty_index = argv.index("--resume-obligation") + 1
        if jobs_index >= len(argv) or duty_index >= len(argv) or argv[jobs_index] != expected_jobs:
            continue
        stat_row = _proc_stat(pid)
        if stat_row is not None:
            active.setdefault(argv[duty_index], []).append(
                {"pid": pid, "pid_start": stat_row["start"]})
    return active


class RuntimeObservationCpuFixture(unittest.TestCase):
    def test_isolated_production_observer_workload(self):
        with tempfile.TemporaryDirectory(prefix="hearting-runtime-observation-cpu-") as temporary:
            temp_root = Path(temporary)
            data_root = temp_root / "harness-data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)
            source = releases / "fixture"
            _make_source_copy(source)
            current = data_root / "current"
            current.symlink_to(source, target_is_directory=True)
            state_root = temp_root / "peer-state"
            home = temp_root / "home"
            fake_bin = temp_root / "bin"
            profile_dir = temp_root / "profiles"
            profile_hook = temp_root / "profile-hook"
            fake_bin.mkdir()
            fake_herdr = fake_bin / "herdr"
            fake_herdr.write_text(_FAKE_HERDR, encoding="utf-8")
            fake_herdr.chmod(0o755)
            fifo = temp_root / "herdr-wait.fifo"
            os.mkfifo(fifo)
            fifo_keeper = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            call_log = temp_root / "herdr-calls.log"
            jobs_path = temp_root / "dispatch" / "jobs.log"
            home.mkdir()
            for path in (home / ".config", home / ".local" / "share",
                         home / ".local" / "state", home / ".cache"):
                path.mkdir(parents=True, exist_ok=True)

            old_env = dict(os.environ)
            source_commit = old_env.get("HEARTING_SOURCE_COMMIT", "")
            source_diff_digest = old_env.get("HEARTING_SOURCE_DIFF_SHA256", "")
            if not source_commit:
                try:
                    source_commit = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT,
                        env=old_env, stderr=subprocess.DEVNULL, text=True).strip()
                except (OSError, subprocess.CalledProcessError):
                    source_commit = "source-export"
            if not source_diff_digest:
                try:
                    source_diff = subprocess.check_output(
                        ["git", "diff", "--binary", "HEAD"], cwd=SOURCE_ROOT,
                        env=old_env, stderr=subprocess.DEVNULL)
                    source_diff_digest = hashlib.sha256(source_diff).hexdigest()
                except (OSError, subprocess.CalledProcessError):
                    source_diff_digest = "unavailable"
            isolated = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "HARNESS_DATA_ROOT": str(data_root),
                "HEARTING_GATES": "on",
                "AGENT_HOME": str(source),
                "AGENT_DISPATCH_JOBS": str(jobs_path),
                "AGENT_PEER_LEDGER_ROOT": str(state_root),
                "HERDR_SOCKET_PATH": str(temp_root / "herdr-observation.sock"),
                "AGENT_HERDR_SESSION": "default",
                "FIXTURE_HERDR_FIFO": str(fifo),
                "FIXTURE_HERDR_LOG": str(call_log),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "PYTHONPATH": str(source / "utilities") + os.pathsep
                              + str(source / "tools"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            if DIAGNOSTIC_PROFILE:
                profile_dir.mkdir()
                profile_hook.mkdir()
                (profile_hook / "sitecustomize.py").write_text(
                    "import atexit, cProfile, os, signal, sys\n"
                    "profile_target = ('codex-managed-completion.py' in sys.argv[0] or "
                    "'dispatch-registry.py' in sys.argv[0] or "
                    "('peer-steward.py' in sys.argv[0] and "
                    "('__watch-run' in sys.argv or '__obligation-runner' in sys.argv)))\n"
                    "if profile_target:\n"
                    "    profiler = cProfile.Profile()\n"
                    "    profiler.enable()\n"
                    "    saved = [False]\n"
                    "    def save_profile():\n"
                    "        if saved[0]: return\n"
                    "        saved[0] = True\n"
                    "        profiler.disable()\n"
                    "        profiler.dump_stats(os.path.join(os.environ['FIXTURE_PROFILE_DIR'], "
                    "str(os.getpid()) + '.prof'))\n"
                    "    atexit.register(save_profile)\n",
                    encoding="utf-8")
                with (profile_hook / "sitecustomize.py").open("a", encoding="utf-8") as hook:
                    hook.write(
                        "    original_exit = os._exit\n"
                        "    def profiled_exit(code):\n"
                        "        save_profile()\n"
                        "        original_exit(code)\n"
                    "    os._exit = profiled_exit\n"
                    "    def profiled_signal(signum, frame):\n"
                    "        save_profile()\n"
                    "        raise SystemExit(128 + signum)\n"
                    "    signal.signal(signal.SIGTERM, profiled_signal)\n")
                isolated["FIXTURE_PROFILE_DIR"] = str(profile_dir)
                isolated["PYTHONPATH"] = str(profile_hook) + os.pathsep + isolated["PYTHONPATH"]
            for key in list(os.environ):
                if (key.startswith(("AGENT_DISPATCH_", "AGENT_PEER_", "HERDR_", "AGENT_HERDR_",
                                    "AGENT_SESSION_", "CLAUDE_", "CODEX_", "OPENCODE_"))
                        or key in {"AGENT_HOME", "HARNESS_DATA_ROOT", "CODEX_HOME",
                                   "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                   "XDG_STATE_HOME", "XDG_CACHE_HOME"}):
                    os.environ.pop(key, None)
            os.environ.update(isolated)

            runner_process = None
            watch_processes = []
            worker_processes = []
            observation_server = None
            open_successor_locks = contextlib.ExitStack()
            try:
                sys.path.insert(0, str(source / "utilities"))
                sys.path.insert(0, str(source / "tools" / "install"))
                admission = __import__("artifact_admission")
                artifact_index = __import__("artifact_index")
                artifact_identity = __import__("artifact_identity")
                artifact_manifest = __import__("artifact_manifest")
                artifact_producer = __import__("artifact_producer")
                dispatch_contract = __import__("dispatch_contract")
                batch = __import__("dispatch_batch_obligations")
                peer_obligations = __import__("peer_obligations")
                admission_fixture = _import_from(
                    source / "utilities" / "artifact_admission.test.py",
                    "runtime_cpu_admission_fixture")
                producer_fixture = _import_from(
                    source / "utilities" / "artifact_producer.test.py",
                    "runtime_cpu_producer_fixture")
                managed = _import_from(source / "utilities" / "codex-managed-completion.py",
                                       "runtime_cpu_managed_completion")
                steward = _import_from(source / "utilities" / "peer-steward.py",
                                       "runtime_cpu_peer_steward")
                session_tidy = __import__("session_tidy")

                jobs_path.parent.mkdir(parents=True, exist_ok=True)
                worker_identities = []
                for _index in range(OBSERVER_COUNT):
                    worker = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(600)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        cwd=str(temp_root), env=dict(os.environ))
                    worker_processes.append(worker)
                    identity = dispatch_contract.process_launch_identity(worker.pid)
                    if not identity.get("pid_start"):
                        raise AssertionError("fixture worker has no exact PID/start identity")
                    worker_identities.append(identity)
                _write_jobs(jobs_path, source, worker_identities)
                jobs_size = jobs_path.stat().st_size
                if jobs_size < JOBS_BYTES:
                    raise AssertionError(f"jobs registry is only {jobs_size} bytes")

                projects = [_valid_sealed_project(
                    temp_root / "projects" / "project-0", temp_root,
                    {"admission": admission, "identity": artifact_identity,
                     "manifest": artifact_manifest, "artifact_index": artifact_index,
                     "producer": artifact_producer,
                     "producer_fixture": producer_fixture})]
                for index in (1, 2):
                    project = temp_root / "projects" / f"project-{index}"
                    project.mkdir(parents=True)
                    projects.append(_valid_project(
                        project, large_manifest=False, large_index=False,
                        modules={"admission": admission, "identity": artifact_identity,
                                 "manifest": artifact_manifest,
                                 "admission_fixture": admission_fixture}))
                if projects[0]["manifest_bytes"] < MANIFEST_BYTES:
                    raise AssertionError("large manifest threshold was not met")
                if projects[0]["index_bytes"] < INDEX_BYTES:
                    raise AssertionError("large admission index threshold was not met")

                historical_metrics = projects[0].get(
                    "historical_verification_metrics_delta", {})
                if (not BASELINE_MODE and
                        (historical_metrics.get("loads") != 2
                         or historical_metrics.get("hits") != 2)):
                    raise AssertionError(
                        f"sealed verifier did not reuse immutable generations: {historical_metrics}")

                panes = [{"pane_id": "w1:p9", "agent": "claude",
                          "agent_session": {"value": "sid-fixture"}}]
                observation_server = FakeObservationEndpoint(
                    Path(isolated["HERDR_SOCKET_PATH"]), panes)
                binding = peer_obligations.managed_service_binding(source)
                if binding != str(current):
                    raise AssertionError(f"isolated managed binding unavailable: {binding!r}")

                # Keep real session-tidy seat and successor locks in the isolated state root.
                for key in ("fixture-seat-a", "fixture-seat-b",
                            "peer-successor-fixture-a", "peer-successor-fixture-b"):
                    open_successor_locks.enter_context(session_tidy.seat_lock(key))

                watch_root = steward._watch_root()
                watch_root.mkdir(parents=True, exist_ok=True)
                for index in range(WATCHER_COUNT):
                    watch_id = f"{index + 1:016x}"
                    paths = steward._watch_paths(watch_id, watch_root)
                    arm = {
                        "schema_version": steward._WATCH_SCHEMA,
                        "watch_id": watch_id, "target": f"fixture-target-{index}",
                        "until": ["idle"], "timeout": 300_000,
                        "deadline": steward._watch_deadline(300_000),
                        "server": "default", "armed_ts": steward._utc_now(),
                        "agent": {"harness": "claude", "session_id": "sid-fixture",
                                  "name": f"fixture-target-{index}", "pane": "w1:p9"},
                        "steward": {"harness": "codex", "session_id": "sid-steward",
                                    "project": str(source)},
                        "observer_protocol": 2, "observer_generation": 1,
                        "rearm_count": 0, "watcher": {},
                    }
                    steward._write_json_atomic(paths.arm, arm)
                    lock_fd = steward._open_lock(paths.lock)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    argv = [sys.executable, str(source / "utilities" / "peer-steward.py"),
                            *steward._watch_observer_duty_argv(
                                watch_id, arm, rearm_generation=1, lock_fd=lock_fd,
                                service_current=binding)]
                    log_fd = os.open(paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    try:
                        process = subprocess.Popen(
                            argv, stdin=subprocess.DEVNULL, stdout=log_fd,
                            stderr=subprocess.STDOUT, close_fds=True, pass_fds=(lock_fd,),
                            start_new_session=True, cwd=str(watch_root), env=dict(os.environ))
                    finally:
                        os.close(log_fd)
                        os.close(lock_fd)
                    arm["watcher"] = {"pid": process.pid,
                                      "pid_start": steward.process_start_ticks(process.pid) or ""}
                    steward._write_json_atomic(paths.arm, arm)
                    watch_processes.append((watch_id, process))

                # Create eight real exact-attempt completion duties. The managed
                # runner below launches their actual --resume-obligation entrypoints.
                duty_ids = []
                for index in range(OBSERVER_COUNT):
                    args = managed.parser().parse_args([
                        "--jobs", str(jobs_path), "--control-socket",
                        str(temp_root / "control.sock"), "--parent-attempt-id",
                        f"att-parent-{index:02d}", "--sealed-batch-id",
                        f"sealed-fixture-{index:02d}", "--attempt-id",
                        f"att-observer-{index:02d}", "--interval", "1",
                        "--timeout", "600",
                    ])
                    duty_id, _record = batch.create(args)
                    duty_ids.append(duty_id)

                if not peer_obligations.ensure_normal_runner(
                        state_root, lifecycle_root=source, reconciliation_required=True):
                    raise AssertionError("managed obligation runner did not start")
                runner_process = peer_obligations._RUNNERS[-1]
                if runner_process.poll() is not None:
                    raise AssertionError("managed obligation runner exited during startup")

                def observer_snapshot():
                    records = [batch.read(jobs_path, duty_id) for duty_id in duty_ids]
                    if any(not record for record in records):
                        return None
                    owners = [record.get("observer") or {} for record in records]
                    if any(not owner.get("pid") or not owner.get("pid_start") for owner in owners):
                        return None
                    for duty_id, owner in zip(duty_ids, owners):
                        pid = int(owner["pid"])
                        proc = _proc_stat(pid)
                        if proc is None or proc["start"] != owner["pid_start"]:
                            return None
                        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                            errors="replace").split("\0")
                        if "--resume-obligation" not in argv:
                            return None
                        fd_number = argv[argv.index("--obligation-lock-fd") + 1]
                        lease = batch._root(jobs_path) / (duty_id + ".lease")
                        lock_held = peer_obligations._process_holds_flock(
                            pid, lease, fd_number)
                        if not lock_held and not BASELINE_MODE:
                            return None
                        owner["lease_held"] = lock_held
                    return owners

                owners = _wait_for(observer_snapshot, 20, "eight exact managed observers")
                try:
                    _wait_for(lambda: len(observation_server.subscriptions) >= WATCHER_COUNT,
                              20, "eleven actual watch subscriptions")
                except AssertionError as exc:
                    diagnostics = []
                    for watch_id, process in watch_processes:
                        paths = steward._watch_paths(watch_id, watch_root)
                        diagnostics.append({
                            "watch_id": watch_id, "pid": process.pid,
                            "returncode": process.poll(),
                            "observation": (json.loads(paths.observation.read_text())
                                            if paths.observation.exists() else None),
                            "receipt": (json.loads(paths.receipt.read_text())
                                        if paths.receipt.exists() else None),
                            "log": paths.log.read_text(errors="replace")[-2000:]
                                    if paths.log.exists() else "",
                        })
                    raise AssertionError(f"{exc}; watcher diagnostics={diagnostics}") from exc
                watcher_pids = {process.pid for _watch_id, process in watch_processes}
                runner_pids = {runner_process.pid}
                owner_pids = {int(owner["pid"]) for owner in owners}
                worker_pids = {process.pid for process in worker_processes}
                seed_pids = watcher_pids | runner_pids | owner_pids | worker_pids

                def current_seed_pids():
                    current = set(watcher_pids | runner_pids | worker_pids)
                    current.update(int(owner["pid"])
                                   for group in _active_observer_processes(jobs_path).values()
                                   for owner in group)
                    return current

                cold_start = _sample_window(COLD_SECONDS, seed_pids,
                                            seed_provider=current_seed_pids)
                if observer_snapshot() is None:
                    raise AssertionError("observer PID/start or lease changed during cold start")
                warm = _sample_window(IDLE_SECONDS, seed_pids,
                                      seed_provider=current_seed_pids)
                stable_owners = observer_snapshot()
                if stable_owners is None or (not BASELINE_MODE and stable_owners != owners):
                    raise AssertionError("observer PID/start or lease changed during warm idle")
                warm_active = _active_observer_processes(jobs_path)
                for _ in range(3):
                    before = [dict(owner) for owner in stable_owners]
                    batch.ensure_observers(jobs_path, service_current=binding)
                    after = observer_snapshot()
                    if not BASELINE_MODE and after != before:
                        raise AssertionError("an ensure pass duplicated or replaced a healthy observer")
                observation_server.publish_unrelated()
                burst = _sample_window(BURST_SECONDS, seed_pids,
                                       seed_provider=current_seed_pids)
                after_burst = observer_snapshot()
                if not BASELINE_MODE and after_burst != stable_owners:
                    raise AssertionError("unrelated event changed managed observer ownership")
                burst_active = _active_observer_processes(jobs_path)

                if DIAGNOSTIC_PROFILE:
                    for _watch_id, process in watch_processes:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                    _wait_for(lambda: all(process.poll() is not None
                                          for _watch_id, process in watch_processes),
                              5, "profiled watcher shutdown")

                # Replacing one observer row is an ownership loss. That process
                # must exit without restoring itself; the managed runner then
                # retries the same duty with one current owner.
                replacement_result = {"mode": "baseline-not-asserted"} if BASELINE_MODE else None
                if not BASELINE_MODE:
                    first_duty = duty_ids[0]
                    first_owner = stable_owners[0]
                    replacement = {"pid": os.getpid(),
                                   "pid_start": str(_proc_stat(os.getpid())["start"])}
                    batch.update(jobs_path, first_duty, state="unknown", observer=replacement,
                                 reason="fixture-observer-record-replaced")
                    _wait_for(lambda: _proc_stat(int(first_owner["pid"])) is None,
                              12, "lost observer to exit")

                    def retry_owner():
                        record = batch.read(jobs_path, first_duty)
                        owner = (record or {}).get("observer") or {}
                        proc = _proc_stat(int(owner["pid"])) if str(owner.get("pid", "")).isdigit() else None
                        if (proc is None or owner.get("pid_start") != proc["start"]
                                or int(owner.get("pid", 0)) == os.getpid()):
                            return None
                        argv = Path(f"/proc/{owner['pid']}/cmdline").read_bytes().decode(
                            errors="replace").split("\0")
                        return owner if "--resume-obligation" in argv else None

                    replacement_owner = _wait_for(retry_owner, 15, "automatic same-duty observer retry")
                    for _ in range(2):
                        batch.ensure_observers(jobs_path, service_current=binding)
                    if retry_owner() != replacement_owner:
                        raise AssertionError("repeated ensure pass replaced the healthy retry owner")
                    replacement_result = {
                        "lost_pid": int(first_owner["pid"]),
                        "new_owner": replacement_owner,
                        "unknown_state_preserved": batch.read(jobs_path, first_duty)["state"] == "unknown",
                    }

                profile_summaries = []
                if DIAGNOSTIC_PROFILE and profile_dir.is_dir():
                    for profile_path in sorted(profile_dir.glob("*.prof")):
                        stats = pstats.Stats(str(profile_path))
                        top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:30]
                        profile_summaries.append({
                            "pid": int(profile_path.stem),
                            "functions_by_cumulative_time": [
                                {"file": key[0], "line": key[1], "function": key[2],
                                 "calls": value[1], "self_seconds": value[2],
                                 "cumulative_seconds": value[3]}
                                for key, value in top],
                        })

                output = {
                    "source_root": str(SOURCE_ROOT),
                    "fixture_root": str(temp_root),
                    "jobs_path": str(jobs_path),
                    "harness_pid": os.getpid(),
                    "source_commit": source_commit,
                    "source_diff_sha256": source_diff_digest,
                    "baseline_mode": BASELINE_MODE,
                    "jobs_bytes": jobs_size,
                    "projects": projects,
                    "observers": OBSERVER_COUNT, "watchers": WATCHER_COUNT,
                    "managed_runner": RUNNER_COUNT,
                    "observer_pid_start": [
                        {"pid": int(item["pid"]), "pid_start": item["pid_start"],
                         "lease_held": bool(item.get("lease_held"))}
                        for item in stable_owners],
                    "warm_active_observer_processes": warm_active,
                    "burst_active_observer_processes": burst_active,
                    "watcher_pid_start": [
                        {"pid": proc.pid,
                         "pid_start": (steward._read_json(
                             steward._watch_paths(watch_id, watch_root).arm) or {}
                             ).get("watcher", {}).get("pid_start", "")}
                        for watch_id, proc in watch_processes],
                    "runner_pid_start": {"pid": runner_process.pid,
                                         "pid_start": _proc_stat(runner_process.pid)["start"]},
                    "worker_pid_start": [
                        {"pid": process.pid, "pid_start": _proc_stat(process.pid)["start"]}
                        for process in worker_processes],
                    "diagnostic_profile": DIAGNOSTIC_PROFILE,
                    "warm_idle_seconds": IDLE_SECONDS,
                    "unrelated_event_seconds": BURST_SECONDS,
                    "cold_start_seconds": COLD_SECONDS,
                    "cold_start": cold_start,
                    "warm_idle": warm,
                    "unrelated_event": burst,
                    "profile_summaries": profile_summaries,
                    "herdr_requests": dict(observation_server.requests),
                    "unrelated_events_sent": WATCHER_COUNT,
                    "historical_index_projection_metrics_delta": historical_metrics,
                    "observer_replacement_retry": replacement_result,
                    "limitations": [
                        "target-file syscall bytes and per-function hot-branch profiles require a separate strace/profile run",
                        "proc sampling reports every observed PID/start identity; sub-50ms exited children may fall between samples",
                    ],
                }
                print(json.dumps(output, indent=2, sort_keys=True))
                evidence_path = os.environ.get("HEARTING_RUNTIME_OBSERVATION_EVIDENCE")
                if evidence_path:
                    path = Path(evidence_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
            finally:
                # Every process belongs to this temporary fixture. Stop exact
                # process groups while all state roots and sockets still exist.
                groups = set()
                for process in [runner_process, *(p for _wid, p in watch_processes),
                                *worker_processes]:
                    if process is not None:
                        groups.add(process.pid)
                for duty_id in locals().get("duty_ids", []):
                    record = batch.read(jobs_path, duty_id) if "batch" in locals() else None
                    observer = (record or {}).get("observer") or {}
                    if str(observer.get("pid", "")).isdigit():
                        groups.add(int(observer["pid"]))
                temp_prefix = str(temp_root) + os.sep
                def fixture_process_groups():
                    found = set()
                    for name in os.listdir("/proc"):
                        if not name.isdigit():
                            continue
                        pid = int(name)
                        try:
                            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                                errors="replace").split("\0")
                        except OSError:
                            continue
                        command = " ".join(argv)
                        if (temp_prefix in command and any(
                                marker in command for marker in (
                                    "codex-managed-completion.py", "peer-steward.py", "/bin/herdr"))):
                            identity = _proc_stat(pid)
                            if identity is not None:
                                found.add(identity["pgrp"])
                    return found

                groups.update(fixture_process_groups())
                for pgid in groups:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    discovered = fixture_process_groups()
                    if not discovered:
                        break
                    for pgid in discovered:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.05)
                # An observer can start its one same-duty retry while its old
                # lease holder exits. Re-scan after signaling so a child that
                # appeared during teardown is also stopped before temp_root is
                # removed.
                for _ in range(20):
                    discovered = fixture_process_groups()
                    if not discovered:
                        break
                    for pgid in discovered:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGKILL)
                    time.sleep(0.05)
                for pgid in groups:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGKILL)
                for process in [runner_process, *(p for _wid, p in watch_processes),
                                *worker_processes]:
                    if process is not None:
                        with contextlib.suppress(Exception):
                            process.wait(timeout=2)
                if observation_server is not None:
                    observation_server.close()
                open_successor_locks.close()
                with contextlib.suppress(OSError):
                    os.close(fifo_keeper)
                os.environ.clear()
                os.environ.update(old_env)
                for entry in (str(source / "utilities"), str(source / "tools" / "install")):
                    with contextlib.suppress(ValueError):
                        sys.path.remove(entry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
