#!/usr/bin/env python3
"""Replay the 2026-10-28 owner-pin-handoff case on temp records (U5 plan 0).

Read-only capture lives in ``fixtures/u5-pin-handoff-e4c2.json``.  This module
rebuilds the sealed routes, the R0 ledger, the registry rows, the jobs rows and
the replacement owner's supervisor state under one private temporary root, then
proves the pre-repair refusal chain against unmodified source and the post-repair
expectations (d1-d4) through the public entry points.

Pre-repair proof (source at 2275cf79a):
    U5_EXPECT_PRE_REPAIR=1 python3 utilities/preserved_resource_handoff.test.py \
        PreservedResourceHandoffReplayTest.test_pre_repair_rejection_chain
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import artifact_producer as PRODUCER
import dispatch_completion_join as JOIN
import dispatch_contract as DC
import dispatch_owner_input as INPUT
import dispatch_resource_wait as WAIT
import owner_route_binding as OWNER
import resource_run_registry as RR
import resource_resume as RESUME
import route_authority as AUTH
import route_identity as RI
import route_lineage as RL
import route_parent_close as CLOSE
import workflow_state as WS

import fixture_processes


def _load(name, filename, path=None):
    spec = importlib.util.spec_from_file_location(name, path or (HERE / filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUP = WAIT.supervisor()
ROUTE = _load("u5_replay_route", "capability-route.py")
RUNNER = _load("u5_replay_runner", "resource-runner.py")
V3532 = _load("u5_v3532_wait", None, HERE / "fixtures" / "resource-v3532" / "dispatch_resource_wait.py")

FIXTURE_PATH = HERE / "fixtures" / "u5-pin-handoff-e4c2.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

OLD_OWNER = "att-45b52ff4a004334049fa5d1fcedbec9745c48d5e1a0e70ba"
OLD_OWNER_SID = "0418c5a4-f76c-4fda-9351-96732f72236c"
S71_SID = "53f4ef6b-8767-46eb-8c59-29de682e6dee"
R1_OWNER = "att-9f4aaab1d16e79cb4f9f47f6a36a8856"
REPLACEMENT = "att-u5e9replacement00000000000000000"
E9_SID = "01a1811a-0000-7000-8000-000000000000"
E9_THREAD = "fixture-thread-e9owner"
VAR_RUN = "ami-retrain50102020__var__045454ce1b52"
VAR_REMOTE = "cnn-20261027-060348-ami-rir512-var-scratch50-seed0"
FIX_DIRECT_REMOTE = "cnn-20261028-094907-ami-rir512-fix-resume-cnn0-e05-seed0-direct"
FIX_RUN = "ami-retrain50102020__fix__603f1b434f83"


def _replace(value, pairs):
    if isinstance(value, str):
        for old, new in pairs:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace(item, pairs) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, pairs) for key, item in value.items()}
    return value


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReplayProcess:
    """A private payload/owner process owned by one test."""

    def __init__(self, marker):
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "U5_REPLAY_MARKER": marker})
        self.identity = None
        for _ in range(200):
            self.identity = RR.proc_identity(self.process.pid)
            if self.identity:
                break
            time.sleep(0.01)

    def stop(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait(timeout=10)


class PreservedResourceHandoffReplayTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        runtime = os.environ.get("AGENT_RUNTIME_DIR") or None
        self.base = Path(tempfile.mkdtemp(dir=runtime))
        self.addCleanup(self._remove_base)
        self.dispatch = self.base / "dispatch"
        self.jobs = self.dispatch / "jobs.log"
        self.artifacts = self.base / "artifacts"
        self.worktree = self.base / "worktree"
        self.repo = self.base / "repo"
        self.registry = self.base / "registry" / "resource-runs.json"
        self.cycle_base = self.artifacts / "cycle"
        self.routes_dir = self.artifacts / ".runtime" / "routes"
        self.completion = self.dispatch / "completion"
        self.state_root = self.base / "state"
        for path in (self.dispatch, self.artifacts, self.worktree, self.repo,
                     self.registry.parent, self.cycle_base, self.routes_dir,
                     self.completion, self.state_root):
            path.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text("")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AGENT_")}
        env.update(
            AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(self.jobs),
            AGENT_ARTIFACT_ROOT=str(self.artifacts), AGENT_ARTIFACT_CHECKPOINT="off",
            AGENT_RESOURCE_RUN_INDEX=str(self.base / "resource-index.json"),
            XDG_STATE_HOME=str(self.state_root), HEARTING_GATES="off",
            COMPUTE_HOSTS_CONFIG=str(self.base / "no-compute.yaml"),
            CUDA_VISIBLE_DEVICES="",
        )
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.processes = []
        self.owners_patch = mock.patch.object(AUTH, "owns", return_value=False)
        self.owners_patch.start()
        self.addCleanup(self.owners_patch.stop)
        self.addCleanup(fixture_processes.reap, str(self.base))
        self._materialize()

    # ------------------------------------------------------------------ fixture
    def _paths(self):
        return [
            ("{ARTIFACT_ROOT}", str(self.artifacts)),
            ("{CWD}", str(self.worktree)),
            ("{DISPATCH}", str(self.dispatch)),
            ("{RELEASES}", str(self.base / "releases")),
            ("{COMPUTE_RUNS}", str(self.base / "compute-runs")),
            ("/home/nas/user/Uihyeop/NN_Zoo/SR_CorrNet", str(self.repo)),
        ]

    def _remove_base(self):
        import shutil
        for _ in range(20):
            shutil.rmtree(self.base, ignore_errors=True)
            if not self.base.exists():
                return
            time.sleep(0.1)
        shutil.rmtree(self.base, ignore_errors=True)

    def _reap(self):
        for process in self.processes:
            process.stop()

    def _spawn(self, marker):
        process = ReplayProcess(marker)
        self.processes.append(process)
        return process

    def _route(self, name, source=None):
        route = _replace(copy.deepcopy(FIXTURE["routes"][name]), self._paths())
        route["artifact_root"] = str(self.artifacts)
        route["cwd"] = str(self.worktree)
        lct = route.get("launch_compatibility_tuple")
        if isinstance(lct, dict) and isinstance(lct.get("jobs_path"), dict):
            lct["jobs_path"]["path"] = str(self.jobs)
        if source is not None:
            route["source_route_id"] = source["route_id"]
            route["source_route_hash"] = source["route_hash"]
            for key in ("source_route_supersession",):
                edge = route.get(key)
                if isinstance(edge, dict):
                    edge["from_route_id"] = source["route_id"]
                    edge["from_route_hash"] = source["route_hash"]
            for edge in route.get("supersession_edges") or []:
                edge["from_route_id"] = source["route_id"]
                edge["from_route_hash"] = source["route_hash"]
            for row in route.get("reused_nodes") or []:
                if isinstance(row.get("marker_path"), str):
                    row["marker_path"] = row["marker_path"].replace(
                        FIXTURE["routes"]["R0"]["route_id"], source["route_id"])
        digest = RI.route_hash(route)
        route["route_hash"] = digest
        route["route_id"] = RI.route_id_from_hash(digest)
        return route

    def _write_route(self, route):
        path = self.routes_dir / f"{route['route_id']}.json"
        path.write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
        return path

    def _materialize(self):
        old = {name: FIXTURE["routes"][name]["route_id"] for name in ("R0", "R1", "S1", "S2")}
        self.r0 = self._route("R0")
        self.r0_path = self._write_route(self.r0)
        old_to_new = {old["R0"]: self.r0["route_id"]}
        self.r1 = self._route("R1", source=self.r0)
        self.r1_path = self._write_route(self.r1)
        self.s1 = self._route("S1", source=self.r0)
        self.s1_path = self._write_route(self.s1)
        self.s2 = self._route("S2", source=self.r0)
        self.s2_path = self._write_route(self.s2)
        old_to_new.update({old["R1"]: self.r1["route_id"],
                           old["S1"]: self.s1["route_id"],
                           old["S2"]: self.s2["route_id"]})
        id_pairs = []
        for name in ("R0", "R1", "S1", "S2"):
            id_pairs.append((FIXTURE["routes"][name]["route_id"], old_to_new[old[name]]))
            id_pairs.append((FIXTURE["routes"][name]["route_hash"],
                             {"R0": self.r0, "R1": self.r1, "S1": self.s1,
                              "S2": self.s2}[name]["route_hash"]))
        self.id_pairs = id_pairs
        self.old_to_new = old_to_new

        outcome = _replace(copy.deepcopy(FIXTURE["outcome"]["R0"]), self._paths() + id_pairs)
        outcome["route_file"] = str(self.r0_path)
        outcome["cwd"] = str(self.worktree)
        (self.routes_dir / f"{self.r0['route_id']}.outcome.json").write_text(
            json.dumps(outcome, sort_keys=True), encoding="utf-8")
        self.outcome = outcome

        pins = _replace(copy.deepcopy(FIXTURE["pin_changes"]["R0"]), self._paths() + id_pairs)
        (self.routes_dir / f"{self.r0['route_id']}.pin-changes.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in pins), encoding="utf-8")
        self.pin_changes = pins

        # R0 ledger: the exact close journal, rewritten to the temporary route.
        self.ledger0 = WS.WorkflowLedger(self.r0["route_id"], self.r0["route_hash"], jobs=self.jobs)
        for entry in _replace(copy.deepcopy(FIXTURE["ledger"]["R0"]["journal_close"]),
                              self._paths() + id_pairs):
            self.ledger0._append(entry)
        self.ledger0.state()

        # jobs.log: the two captured rows plus the replacement owner.
        old_row = _replace(FIXTURE["jobs"]["rows"]["old_owner"], self._paths() + id_pairs)
        r1_row = _replace(FIXTURE["jobs"]["rows"]["r1_owner"], self._paths() + id_pairs)
        self.old_owner_row = old_row
        self.r1_owner_row = r1_row
        self.owner_process = self._spawn("u5-replacement-owner")
        self.watcher_process = self._spawn("u5-original-watcher")
        self.payload_process = self._spawn(VAR_RUN)
        self.owner_row = self._replacement_row()
        self.jobs.write_text("\n".join([old_row, r1_row, self.owner_row]) + "\n",
                             encoding="utf-8")

        # registry: the captured rows with live fixture identities.
        rows = _replace(copy.deepcopy(FIXTURE["registry"]["rows"]), self._paths() + id_pairs)
        for run_id, row in rows.items():
            row["route"] = str(self.r0_path)
            row["route_id"] = self.r0["route_id"]
            row["jobs"] = str(self.jobs)
            row["artifact_root"] = str(self.artifacts)
            row["cwd"] = str(self.worktree)
            row["log"] = str(self.artifacts / "logs" / f"{run_id}.log")
            row["sentinel"] = str(self.artifacts / "logs" / f"{run_id}.log.exit")
            row["progress_file"] = str(self.artifacts / "logs" / f"{run_id}.log.progress.json")
            owner = row.get("owner_wait") or {}
            owner["route_id"] = self.r0["route_id"]
            owner["route_hash"] = self.r0["route_hash"]
            owner["jobs"] = str(self.jobs)
            row["owner_wait"] = owner
        var = rows[VAR_RUN]
        var.update({"status": "running", "workflow_state": "RUNNING",
                    "resource_policy": "supervised-owner", "launch_state": "started",
                    "pid": self.payload_process.process.pid,
                    "starttime": self.payload_process.identity["starttime"],
                    "command_hash": self.payload_process.identity["command_hash"],
                    "supervision": RR.proc_identity(self.watcher_process.process.pid),
                    "started_at": time.time() - 60})
        var.pop("process_group", None)
        var.pop("pid_namespace", None)
        for run_id in (FIX_RUN, FIX_RUN + "__a1"):
            rows[run_id]["status"] = "failed"
        self.registry_rows = rows
        self._write_registry()

        # R0 armed watch, bound to the temporary var row.
        armed = _replace(copy.deepcopy(FIXTURE["ledger"]["R0"]["armed_full_run"]),
                         self._paths() + id_pairs)
        armed.update({"route_id": self.r0["route_id"], "route_hash": self.r0["route_hash"],
                      "route_file": str(self.r0_path), "jobs": str(self.jobs),
                      "resource_registry": str(self.registry),
                      "artifact_base": str(self.cycle_base),
                      "successor_cwd": str(self.worktree),
                      "successor_log": str(self.dispatch / "workflow" / self.r0["route_id"]
                                           / "resource" / "verification-start.log")})
        armed["resource_binding"] = WAIT.resource_legacy_body_digest(var)
        armed_path = self.ledger0.root / "armed" / "full-run.json"
        armed_path.parent.mkdir(parents=True, exist_ok=True)
        armed_path.write_text(json.dumps(armed, indent=1, sort_keys=True), encoding="utf-8")
        self.armed = armed
        self.armed_path = armed_path

        # replacement owner input + supervisor state + held lease.
        state_path = self.dispatch / "supervisor-state" / f"{REPLACEMENT}.json"
        JOIN._write_supervisor_state_unlocked(state_path, REPLACEMENT, set(),
                                             phase="running-turn", resource=None)
        self.state_path = state_path
        lease_path = self.dispatch / "supervisor-state" / f"{REPLACEMENT}.lease"
        lease_meta = DC.parse_registry_metadata(self.owner_row.split("\t")[5])
        lease_path.write_bytes(DC._supervisor_lease_payload(lease_meta))
        self.lease_handle = lease_path.open("r+", encoding="utf-8")
        fcntl.flock(self.lease_handle.fileno(), fcntl.LOCK_EX)
        self.addCleanup(self.lease_handle.close)
        meta = DC.parse_registry_metadata(self.owner_row.split("\t")[5])
        target = INPUT._digest({key: meta.get(key, "") for key in
                                ("attempt_id", "harness", "owner_route_id", "owner_route_hash",
                                 "route_id", "route_hash", "parent_sid", "supervisor_lease_nonce")})
        input_path = lease_path.with_suffix(".input.json")
        input_path.write_text(json.dumps({
            "schema_version": 1, "attempt_id": REPLACEMENT, "target": target,
            "thread_id": E9_THREAD, "transport": "session-resume", "accepting": True,
            "generation": "generation-fixture", "requests": [],
        }, sort_keys=True), encoding="utf-8")
        self.input_path = input_path

        # byte snapshots used by d4
        self.snapshot = {
            "registry": _sha(self.registry),
            "armed": _sha(self.armed_path),
            "journal": _sha(self.ledger0.journal_path),
            "state": _sha(self.ledger0.state_path),
        }

    def _replacement_row(self):
        template = DC.parse_registry_metadata(self.r1_owner_row.split("\t")[5])
        meta = dict(template)
        meta.update({
            "attempt_id": REPLACEMENT, "status": "open", "harness": "claude",
            "worker_type": "owner", "unit": "_kernel/owner", "dispatch_depth": "1",
            "attempt_schema_version": "2", "registered_worker": "1",
            "execution_surface": "registered-headless", "transport": "headless",
            "completion_delivery": "session-resume-supervised",
            "supervisor_lease": "flock-v1",
            "supervisor_lease_nonce": "a" * 64,
            "supervisor_lease_file": str(self.dispatch / "supervisor-state" / f"{REPLACEMENT}.lease"),
            "parent_sid": E9_SID, "owner_route_id": self.r1["route_id"],
            "owner_route_hash": self.r1["route_hash"], "owner_route_file": str(self.r1_path),
            "route_id": self.r1["route_id"], "route_hash": self.r1["route_hash"],
            "route_node": "handoff", "capability": "autopilot-lab",
            "capability_mode": "setup", "intensity": "standard",
            "artifact_root": str(self.artifacts),
            "pid": str(self.owner_process.process.pid),
            "pid_start": self.owner_process.identity["starttime"],
            "failure_class": "", "note": "", "parent_close_settled": "",
            "parent_close_route_id": "", "parent_close_route_hash": "",
        })
        for key in ("parent_close_requested", "parent_close_stop_resources"):
            meta.pop(key, None)
        meta.pop("owner_identity_lineage", None)
        fields = ["2026-10-28T02:00:00.000000Z", "open", str(self.repo), str(self.worktree),
                  "framed-ami-ami-v3-ep39-ep46-sr-09d6b888bb-leg1-owner",
                  ",".join(f"{key}={value}" for key, value in meta.items())]
        self.owner_meta = meta
        return "\t".join(fields)

    def _write_registry(self):
        self.registry.write_text(json.dumps({"schema_version": 1, "runs": self.registry_rows},
                                            indent=1, sort_keys=True), encoding="utf-8")
        RR.register_registry(self.registry)

    # ------------------------------------------------------------------ helpers
    def _args(self, **overrides):
        value = dict(route_file=str(self.r1_path), jobs=str(self.jobs),
                     route_id=self.r1["route_id"], route_hash=self.r1["route_hash"],
                     parent_attempt_id=REPLACEMENT, node="full-run")
        value.update(overrides)
        return SimpleNamespace(**value)

    def _control(self, pending=False):
        return SimpleNamespace(thread_id=E9_THREAD, pending=lambda: pending)

    def _cycle(self, route=None):
        route = route or self.r0
        return {"cycle_id": "cyc_u5replay0000000000000000000000",
                "route_id": route["route_id"], "route_hash": route["route_hash"],
                "capability": "autopilot-lab", "intensity": "standard"}

    def _wait(self, *, sleep=None, args=None, control=None, path=None, emitted=None):
        emitted = emitted if emitted is not None else []
        args = args or self._args()
        control = control or self._control()
        path = path or self.state_path
        stop = sleep or self._stop_sleep

        def _sleep(_seconds):
            raise stop
        try:
            value = WAIT.wait(args, path, control, set(), emitted.append, sleep=_sleep)
        except stop:
            value = None
        return value, emitted

    class _StopWait(Exception):
        pass

    _stop_sleep = _StopWait

    def _fake_compute(self, execution, state="quiescent"):
        module = SimpleNamespace(
            ConfigError=type("ConfigError", (Exception,), {}),
            load_config=lambda: {"run_root": str(self.base / "compute-runs"), "hosts": {}},
            execution_record=lambda _config, _run_id: {"state": state, "execution": execution,
                                                       "run_dir": str(self.base / "run")},
            run_snapshot_current=lambda *_args: True,
            validate_compute_outputs=lambda *_args: {"ok": True},
        )
        loader = SimpleNamespace(exec_module=lambda _module: None)
        return [mock.patch("importlib.util.spec_from_file_location",
                           return_value=SimpleNamespace(loader=loader)),
                mock.patch("importlib.util.module_from_spec", return_value=module)]

    def _compute_execution(self, *, route, provenance_route):
        run_id = VAR_REMOTE
        return {
            "kind": "compute-hosts", "run_id": run_id,
            "run_root": str(self.base / "compute-runs"),
            "host": "gpu", "meta_sha256": "m" * 64, "exit_sha256": "e" * 64,
            "receipt_sha256": "r" * 64, "outputs_sha256": "o" * 64,
            "contract_sha256": "c" * 64, "contract": {}, "receipt_state": "finished",
            "identity": {"pid": 9}, "outputs": [], "started_at": 1.0, "ended_at": 2.0,
            "provenance": {
                "run_id": run_id, "registry": str(self.registry),
                "route_id": provenance_route["route_id"],
                "route_hash": provenance_route["route_hash"],
                "route_file": str(self.r0_path if provenance_route is self.r0 else self.r1_path),
                "node": "full-run", "parent_attempt_id": OLD_OWNER,
                "contract": {"run_id": VAR_RUN, "route_hash": provenance_route["route_hash"],
                             "route_id": provenance_route["route_id"]},
            },
        }

    def _inherit(self):
        """Record the inherited watch through the post-repair public wait path."""
        try:
            value, emitted = self._wait()
        except self._StopWait:
            value, emitted = None, []
        self.assertTrue(any(item.get("type") == "dispatch.supervisor.resource-parked"
                            for item in emitted),
                        "wait must park on the inherited watch")
        return self.ledger1(), emitted

    def ledger1(self):
        return WS.WorkflowLedger(self.r1["route_id"], self.r1["route_hash"], jobs=self.jobs)

    # ---------------------------------------------------------------- the tests
    def test_pre_repair_rejection_chain(self):
        if os.environ.get("U5_EXPECT_PRE_REPAIR") != "1":
            self.skipTest("pre-repair chain runs only with U5_EXPECT_PRE_REPAIR=1")

        # ⓪ the cycle refuses the successor because the never-run siblings look like a fork.
        admission = PRODUCER.cycle_route_admission(self.artifacts, self._cycle(), dict(self.r1))
        self.assertFalse(admission.allow)
        self.assertEqual(admission.reason, "cycle-route-binding-mismatch:lineage-fork")

        # ① the replacement owner has no context: the current ledger has no watch.
        self.assertIsNone(WAIT.context(self._args(), self._control()))

        # ② run-verify has no full-run marker; an identity-bearing marker copy is
        #    refused as not-current without the owner admission journal.
        with self.assertRaises(DC.DispatchContractError) as missing:
            DC.completion_marker_gate(str(self.r1_path), "run-verify", "start", ROOT,
                                      self.jobs, attempt_id=REPLACEMENT)
        self.assertEqual(missing.exception.reason, "completion-marker-missing")
        marker_path = self.completion / self.r1["route_id"] / "full-run.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {"schema_version": 2, "node_id": "full-run",
                  "resource_execution_identity": "fixture-identity"}
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        node = next(row for row in self.r1["nodes"] if row["id"] == "full-run")
        currency = DC._resource_stage_marker_current(self.r1, node, marker, marker_path,
                                                     self.jobs)
        self.assertEqual(currency.state, "resource-stage-not-current")

        # ③ the preserved compute run's provenance names R0, so R1 may not adopt it.
        execution = self._compute_execution(route=self.r1, provenance_route=self.r0)
        patches = self._fake_compute(execution)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(SystemExit) as caught:
            RUNNER.adopt_compute_run(self.registry, self._args(), self.r1, self.r1_path,
                                     execution["run_id"])
        refusal = json.loads(caught.exception.resource_message)
        self.assertEqual(refusal["reason"], "resource-compute-route-mismatch")

    def test_d2_pin_handoff_proof_survives_retire_handover_and_cycle_admits_successor(self):
        proof = CLOSE.verified_owner_pin_handoff_close(dict(self.r0), jobs=self.jobs)
        self.assertIsNotNone(proof, "a pin moved by the handed-over seat is handoff authority")
        self.assertEqual(proof["successor"]["route_id"], self.r1["route_id"])
        admission = PRODUCER.cycle_route_admission(self.artifacts, self._cycle(), dict(self.r1))
        self.assertTrue(admission.allow, admission.reason)

    def test_d1_successor_owner_inherits_watch_and_parks(self):
        value, emitted = self._wait()
        parked = [item for item in emitted if item.get("type") == "dispatch.supervisor.resource-parked"]
        self.assertEqual(len(parked), 1, emitted)
        self.assertEqual(parked[0]["resource_count"], 1)
        ledger = self.ledger1()
        inherited = ledger.root / "armed" / "full-run.json"
        self.assertTrue(inherited.is_file())
        self.assertEqual(inherited.read_bytes(), self.armed_path.read_bytes())
        entries = [entry for entry in ledger.journal()
                   if (entry.get("evidence") or {}).get("inherited_watch")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["evidence"]["inherited_watch"]["source_route_id"],
                         self.r0["route_id"])

    def test_d1_finished_run_delivers_receipt_marker_and_run_verify_start(self):
        self._inherit()
        self.payload_process.stop()
        var = self.registry_rows[VAR_RUN]
        Path(var["sentinel"]).parent.mkdir(parents=True, exist_ok=True)
        Path(var["sentinel"]).write_text("0", encoding="utf-8")
        for _ in range(40):
            value, emitted = self._wait()
            if value and "Runtime resource receipt" in value:
                break
            time.sleep(0.05)
        else:
            self.fail("wait did not deliver the resource receipt")
        box = JOIN.read_supervisor_phase_state(self.state_path, REPLACEMENT).resource["outbox"]
        self.assertEqual(box["receipt"]["route_id"], self.r1["route_id"])
        self.assertEqual(box["receipt"]["parent_attempt_id"], REPLACEMENT)
        self.assertEqual(box["receipt"]["state"], "succeeded")
        admitted = SUP.admit_owner_successor_start(
            dict(self.r1), "run-verify", str(self.jobs), REPLACEMENT, E9_THREAD)
        self.assertTrue(admitted)
        marker = self.completion / self.r1["route_id"] / "full-run.json"
        self.assertTrue(marker.is_file())
        stage = self.ledger1().state()["nodes"]["full-run"]
        self.assertEqual(stage["state"], "STAGE_SUCCEEDED")
        self.assertEqual(stage["evidence"]["owner_attempt_id"], REPLACEMENT)
        DC.completion_marker_gate(str(self.r1_path), "run-verify", "start", ROOT,
                                  self.jobs, attempt_id=REPLACEMENT)

    def test_d3_new_start_of_same_payload_is_refused_by_live_inherited_row(self):
        self._inherit()
        var = self.registry_rows[VAR_RUN]
        replacement = {
            "run_id": VAR_RUN + "-new", "cwd": str(self.worktree),
            "log": var["log"], "command": list(var["command"]),
            "route": str(self.r1_path), "node": "full-run", "jobs": str(self.jobs),
            "sentinel": var["sentinel"], "progress_file": var["progress_file"],
            "status": "launching", "parent_attempt_id": REPLACEMENT,
            "owner_wait": dict(self.owner_meta),
            "artifact_root": str(self.artifacts),
        }
        args = SimpleNamespace(jobs=str(self.jobs), node="full-run",
                               run_id=replacement["run_id"])
        blocked = RuntimeError("fixture-launch-blocked")
        with mock.patch.object(RUNNER, "register_registry", side_effect=blocked), \
                mock.patch.object(RUNNER, "start_watch", side_effect=blocked):
            with self.assertRaises(ValueError) as refused:
                RUNNER.start_verified(self.registry, args, self.r1, self.r1_path, replacement)
        self.assertEqual(str(refused.exception), "resource-route-body-conflict")
        self.assertNotIn(replacement["run_id"], json.loads(self.registry.read_text())["runs"])

    def test_d3_route_free_compute_run_is_not_adopted(self):
        execution = self._compute_execution(route=self.r1, provenance_route=self.r1)
        execution["provenance"] = {"run_id": execution["run_id"],
                                   "registry": str(self.registry), "route": None,
                                   "route_id": None, "route_hash": None,
                                   "route_file": None, "node": "full-run",
                                   "attempt_id": None, "parent_attempt_id": None}
        patches = self._fake_compute(execution)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(SystemExit) as caught:
            RUNNER.adopt_compute_run(self.registry, self._args(), self.r1, self.r1_path,
                                     execution["run_id"])
        refusal = json.loads(caught.exception.resource_message)
        self.assertEqual(refusal["reason"], "resource-compute-route-mismatch")
        self.assertEqual(refusal["differences"][0]["field"], "provenance.route_id")

    def test_d3_lineage_compute_run_adopts_only_after_bridge_failed(self):
        # The lineage interpretation binds the preserved run to R1, but only a
        # failed baseline (never the live one) may be adopted.  The full __a1
        # adoption path is covered by resource_runner.test.py (plan 4.5); this
        # replay keeps the decision boundary and the route-free refusal.
        compute_holder = getattr(WAIT, "compute_holder", None)
        self.assertIsNotNone(compute_holder, "dispatch_resource_wait.compute_holder is missing")
        provenance = self._compute_execution(route=self.r1, provenance_route=self.r0)["provenance"]
        holder = compute_holder(provenance, jobs=str(self.jobs))
        self.assertIsNotNone(holder)
        self.assertTrue(holder["inherited"])
        self.assertEqual(holder["route_id"], self.r1["route_id"])
        self.assertFalse(compute_holder(
            {"route_id": None, "route_hash": None, "route_file": None, "node": "full-run",
             "run_id": FIX_DIRECT_REMOTE}, jobs=str(self.jobs))["inherited"])
        candidate = self.registry_rows[VAR_RUN]
        self.assertEqual(RR.classify_identity(candidate)[0], "working")
        failed = {**candidate, "status": "failed", "exit_code": 1,
                  "workflow_state": "FAILED_RETRYABLE", "pid": 999999, "starttime": "1",
                  "command_hash": "0" * 64}
        failed.pop("pid_namespace", None)
        failed.pop("process_group", None)
        self.assertEqual(RR.classify_identity(failed)[0], "exited")

    def test_d4_record_bytes_unchanged_and_v3532_reader_still_binds(self):
        WAIT.context(self._args(), self._control())
        self.assertEqual(_sha(self.registry), self.snapshot["registry"])
        self.assertEqual(_sha(self.armed_path), self.snapshot["armed"])
        self.assertEqual(_sha(self.ledger0.journal_path), self.snapshot["journal"])
        self.assertEqual(_sha(self.ledger0.state_path), self.snapshot["state"])
        legacy = _load("u5_legacy_wait", "dispatch_resource_wait.py",
                       HERE / "fixtures" / "resource-v3532" / "dispatch_resource_wait.py")
        # v3.53.2 read the flat armed record through this exact legacy digest;
        # the old fixture has no boolean helper, so compare the same projection.
        self.assertEqual(legacy.resource_body_digest(self.registry_rows[VAR_RUN]),
                         self.armed["resource_binding"])

    def test_identity_when_lineage_is_not_proven(self):
        holder = getattr(CLOSE, "pin_handoff_holder", None)
        self.assertIsNotNone(holder, "pin_handoff_holder is the one shared interpretation")
        row = self.registry_rows[VAR_RUN]
        proven = holder(row["route"], row["owner_wait"]["route_id"],
                        row["owner_wait"]["route_hash"], row["node"], row["run_id"],
                        kind="resource", jobs=self.jobs)
        self.assertEqual(proven["route_id"], self.r1["route_id"])
        self.assertTrue(proven["inherited"])
        # (a) a sibling continuation that resumes smoke is not a handoff.
        sibling = holder(str(self.s1_path), self.r0["route_id"], self.r0["route_hash"],
                         "full-run", row["run_id"], jobs=self.jobs)
        self.assertFalse(sibling["inherited"])
        # (b) an outcome that no longer preserves the run proves nothing.
        outcome_path = self.routes_dir / f"{self.r0['route_id']}.outcome.json"
        outcome = json.loads(outcome_path.read_text())
        outcome["resources"] = [item for item in outcome["resources"]
                                if item.get("run_id") != row["run_id"]]
        outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
        self.assertFalse(holder(row["route"], row["owner_wait"]["route_id"],
                                row["owner_wait"]["route_hash"], row["node"], row["run_id"],
                                jobs=self.jobs)["inherited"])
        # (c) the old owner row is still open: no settled handoff proof.
        jobs_before = self.jobs.read_text(encoding="utf-8")
        lines = jobs_before.splitlines()
        rewritten = []
        for line in lines:
            fields = line.split("\t")
            if OLD_OWNER in fields[5]:
                fields[1] = "open"
            rewritten.append("\t".join(fields))
        self.jobs.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        try:
            self.assertEqual(holder(row["route"], row["owner_wait"]["route_id"],
                                    row["owner_wait"]["route_hash"], row["node"],
                                    row["run_id"], jobs=self.jobs)["route_id"],
                             self.r0["route_id"])
        finally:
            self.jobs.write_text(jobs_before, encoding="utf-8")
        # (d) the successor does not resume this node: no transfer.
        r1_before = self.r1_path.read_text(encoding="utf-8")
        mutated = copy.deepcopy(self.r1)
        mutated["resume_from_node"] = "smoke"
        self.r1_path.write_text(json.dumps(mutated), encoding="utf-8")
        try:
            self.assertEqual(holder(row["route"], row["owner_wait"]["route_id"],
                                    row["owner_wait"]["route_hash"], row["node"],
                                    row["run_id"], jobs=self.jobs)["route_id"],
                             self.r0["route_id"])
        finally:
            self.r1_path.write_text(r1_before, encoding="utf-8")

    def test_inheritance_is_single_and_replayable(self):
        self._inherit()
        self._inherit()
        inherited = self.ledger1().root / "armed" / "full-run.json"
        self.assertEqual(len(list((self.ledger1().root / "armed").glob("*.json"))), 1)
        entries = [entry for entry in self.ledger1().journal()
                   if (entry.get("evidence") or {}).get("inherited_watch")]
        self.assertEqual(len(entries), 1)
        errors = []

        def worker():
            try:
                self._wait()
            except BaseException as error:  # noqa: BLE001 - replay observation
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(entries), 1)

    def test_two_hop_handoff_keeps_holding(self):
        holder = getattr(CLOSE, "pin_handoff_holder", None)
        self.assertIsNotNone(holder)
        self._inherit()
        r2 = self._route("R1", source=self.r1)
        r2["reason"] = f"owner-pin-handoff:{REPLACEMENT}:codex"
        r2["selection_pins"] = {"contract_version": 1,
                                "owner": {"harness": "codex", "model": None, "effort": None}}
        r2_hash = RI.route_hash(r2)
        r2["route_hash"] = r2_hash
        r2["route_id"] = RI.route_id_from_hash(r2_hash)
        r2_path = self._write_route(r2)
        resources = CLOSE.linked_resources(self.r1, self.r1_path, self.jobs, {REPLACEMENT})
        self.assertIn(VAR_RUN, [item["run_id"] for item in resources])
        self._close_like_capture(r2_path)
        proof = CLOSE.verified_owner_pin_handoff_close(dict(self.r1), jobs=self.jobs)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["successor"]["route_id"], r2["route_id"])
        row = self.registry_rows[VAR_RUN]
        held = holder(row["route"], row["owner_wait"]["route_id"],
                      row["owner_wait"]["route_hash"], row["node"], row["run_id"],
                      jobs=self.jobs)
        self.assertEqual(held["route_id"], r2["route_id"])

    def _close_like_capture(self, successor_path):
        """Close R1 with the captured R0 close shape, rewritten to R1 -> R2."""
        intent_template = CLOSE.ledger_intent(self.r0, self.ledger0)
        result_template = CLOSE.settled_result(self.r0, self.ledger0)
        pairs = [(self.r0["route_id"], self.r1["route_id"]),
                 (self.r0["route_hash"], self.r1["route_hash"]),
                 (OLD_OWNER, REPLACEMENT)]
        intent = _replace(copy.deepcopy(intent_template), pairs)
        result = _replace(copy.deepcopy(result_template), pairs)
        intent.update(route_file=str(self.r1_path),
                      at="2026-10-28T03:00:00.000000Z",
                      resources=[{"kind": "resource", "run_id": VAR_RUN,
                                  "preserved": True, "state": "working"}])
        intent["route"]["route_id"] = self.r1["route_id"]
        intent["route"]["route_hash"] = self.r1["route_hash"]
        result.update(route_file=str(self.r1_path), successor_route=str(successor_path),
                      handoff_pins={"owner": {"harness": "codex", "model": None,
                                              "effort": None}},
                      resources=intent["resources"])
        result["owner_attempt_id"] = REPLACEMENT
        ledger1 = self.ledger1()
        ledger1._append({"at": intent["at"], "route_id": self.r1["route_id"],
                         "route_hash": self.r1["route_hash"], "actor": "parent-close",
                         "evidence": {"parent_close": intent}})
        for node in ("full-run", "run-verify", "handoff"):
            ledger1._append({"at": intent["at"], "route_id": self.r1["route_id"],
                             "route_hash": self.r1["route_hash"], "node": node,
                             "state": "CANCELLED", "workflow_state": None,
                             "evidence": {"reason": "cancelled-by-parent"},
                             "actor": "parent-close", "pid": os.getpid()})
        ledger1._append({"at": intent["at"], "route_id": self.r1["route_id"],
                         "route_hash": self.r1["route_hash"], "node": None, "state": None,
                         "workflow_state": "CANCELLED", "evidence": {}, "actor": "parent-close",
                         "pid": os.getpid()})
        ledger1._append({"at": intent["at"], "route_id": self.r1["route_id"],
                         "route_hash": self.r1["route_hash"], "actor": "parent-close",
                         "evidence": {"parent_close_result": result}})
        (self.routes_dir / f"{self.r1['route_id']}.outcome.json").write_text(
            json.dumps(result), encoding="utf-8")
        (self.routes_dir / f"{self.r1['route_id']}.pin-changes.jsonl").write_text(
            json.dumps({"schema": 1, "route_id": self.r1["route_id"],
                        "route_hash": self.r1["route_hash"], "target": "owner",
                        "previous": {"harness": "claude"},
                        "pin": {"harness": "codex", "model": None, "effort": None},
                        "by": {"harness": "claude", "session_id": E9_SID},
                        "at": "2026-10-28T02:59:00Z"}) + "\n", encoding="utf-8")
        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            fields = line.split("\t")
            metadata = DC.parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == REPLACEMENT:
                metadata.update(status="done", failure_class="cancelled",
                                parent_close_settled="1",
                                parent_close_route_id=self.r1["route_id"],
                                parent_close_route_hash=self.r1["route_hash"],
                                owner_route_id=self.r1["route_id"],
                                owner_route_hash=self.r1["route_hash"],
                                owner_route_file=str(self.r1_path))
                fields[1] = "done"
                fields[5] = ",".join(f"{key}={value}" for key, value in metadata.items())
            rewritten.append("\t".join(fields))
        self.jobs.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

if __name__ == "__main__":
    unittest.main(verbosity=2)
