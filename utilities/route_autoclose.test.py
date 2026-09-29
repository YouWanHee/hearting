#!/usr/bin/env python3
"""The runtime closes routes nobody works on any more (route_autoclose.py).

Every case drives the public CLI (`compose`, `start`, `status`,
`campaign-status`, `campaign-close`) in an isolated artifact root, route-chain
ledger, dispatch registry, workflow ledger, resource-run index and Claude
session registry.  The R- and N-cases are the PR #53 review reproductions
(rounds 1 and 2), kept as regressions.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer  # noqa: E402

CAP = ROOT / "utilities/capability-route.py"
PRODUCER = ROOT / "utilities/artifact_producer.py"
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_PARENT_SESSION_ID", "AGENT_ROUTE_FILE",
                "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE", "HEARTING_INLINE_FINISH_CRASH_AT",
                "AGENT_WORKFLOW_ROOT", "AGENT_RESOURCE_RUN_INDEX")
SESSION_ENV = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID", "opencode": "OPENCODE_SESSION_ID"}
TWO_HOURS, TWO_DAYS, EIGHT_DAYS = 2 * 3600, 2 * 24 * 3600, 8 * 24 * 3600


# Only these reach the commands under test; everything else is set explicitly,
# so a caller running inside a dispatch worker (AGENT_ARTIFACT_*, AGENT_WORKFLOW_ROOT,
# session ids, XDG state) cannot steer a test write into a real artifact root.
INHERITED = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "USER", "LOGNAME", "SHELL")


def isolated_env(**explicit) -> dict:
    return {**{key: os.environ[key] for key in INHERITED if key in os.environ}, **explicit}


def _proc_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class RouteAutocloseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="route-autoclose-test-")
        self.base = base = Path(self.temp.name)
        self.repo, self.root = base / "repo", base / "artifacts"
        self.repo.mkdir(); self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "commit", "-qm", "base"], check=True)
        self.jobs = base / "state" / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("", encoding="utf-8")
        self.sessions = base / "claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.ledgers = base / "route-chains"
        self.resource_index = base / "resource-runs.index.json"
        self.env = isolated_env(
            AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(self.jobs), AGENT_DISPATCH_DEPTH="0",
            XDG_STATE_HOME=str(base / "xdg"), FLEET_ROUTE_CHAIN_DIR=str(self.ledgers),
            CLAUDE_CONFIG_DIR=str(base / "claude"), AGENT_RESOURCE_RUN_INDEX=str(self.resource_index))
        artifact_producer.activate(self.root, repository_id="repo_" + "a" * 32,
                                   artifact_root_id="root_" + "b" * 32,
                                   w7={"campaign_id": "camp_" + "c" * 32})
        self.prompt = base / "task.md"
        self.prompt.write_text("Tighten the report generator.\n", encoding="utf-8")
        # Two supported headless peers, the fixture artifact_producer.test.py uses:
        # an isolated test host has no runtime to probe.
        probe = {"transport": "headless", "surface": "registered-headless", "status": "supported",
                 "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z"}
        self.headless = base / "headless.json"
        self.headless.write_text(json.dumps({"candidates": [{**probe, "harness": "codex"},
                                                            {**probe, "harness": "claude"}]}))
        self.processes = []
        self.sweeps = 0

    def tearDown(self):
        for proc in self.processes:
            proc.kill(); proc.wait()
        self.temp.cleanup()

    # ---- drivers --------------------------------------------------------
    def run_as(self, harness, sid, *argv, program=CAP):
        env = dict(self.env)
        if harness:
            env[SESSION_ENV[harness]] = sid
        return subprocess.run([sys.executable, str(program), *map(str, argv)], cwd=self.repo, env=env,
                              capture_output=True, text=True)

    def compose(self, slug, harness="codex", sid="session-1", *, intensity="direct", campaign="k1", start=True):
        shape = "direct" if intensity == "direct" else "solo"
        argv = ["compose", "--slug", slug, "--campaign-key", campaign, "--shape", shape,
                "--capability", "autopilot-code", "--capability-mode", "dev", "--intensity", intensity,
                "--cwd", self.repo, "--artifact-root", self.root, "--tracking", "tracked",
                "--prompt-file", self.prompt, "--spec-read", "fixture", "--drift-verdict", "within-spec",
                "--artifact-guard", "fixture"]
        if intensity == "direct":
            argv += ["--owner", harness, "--parent-harness", harness]
        else:
            argv += ["--registered-headless-evidence", self.headless]
        done = self.run_as(harness, sid, *argv)
        self.assertEqual(done.returncode, 0, done.stderr)
        route_file = Path(json.loads(done.stdout)["route_file"])
        if start:
            started = self.run_as(harness, sid, "start", "--route", route_file, "--jobs", self.jobs)
            self.assertEqual(started.returncode, 0, started.stderr)
        self.last_stderr = done.stderr
        return route_file, json.loads(route_file.read_text(encoding="utf-8"))

    def later(self):
        """RECHECK_SECONDS pass: routes and cycles the evidence kept are judged again."""
        state = self.root / ".runtime/route-autoclose/state.json"
        if state.is_file():
            data = json.loads(state.read_text())
            data["kept"] = {}
            state.write_text(json.dumps(data))

    def sweep(self):
        """The next compose from some other session is what triggers a sweep."""
        self.sweeps += 1
        self.compose(f"observer-{self.sweeps}", "codex", "observer", start=False)
        return self.last_stderr

    def status(self):
        done = self.run_as("codex", "observer", "status", "--artifact-root", self.root, "--open-only")
        self.assertEqual(done.returncode, 0, done.stderr)
        return {row["route_id"] for row in json.loads(done.stdout)}

    def campaign(self, verb, campaign, *extra):
        return self.run_as("codex", "observer", verb, "--artifact-root", self.root, "--campaign", campaign,
                           *extra, program=PRODUCER)

    def cycle(self, route):
        return artifact_producer.route_cycle_for(self.root, route)

    def cycle_record(self, cycle_id):
        return artifact_producer.read_cycle_record(self.root, cycle_id)

    def cycle_dir(self, record):
        return artifact_producer.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)

    def write_artifact(self, route, name="report.md", body="the report\n"):
        record = self.cycle(route)
        target = self.cycle_dir(record) / "artifacts" / "documents" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return record

    def outcome(self, route_file):
        path = route_file.with_name(route_file.stem + ".outcome.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def age(self, route_file, harness, sid, seconds=EIGHT_DAYS):
        """`seconds` of nothing: route, cycles and the composer's own ledger."""
        old = time.time() - seconds
        ledger = self.ledgers / harness / f"{sid}.jsonl"
        if ledger.is_file():
            lines = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
            ledger.write_text("".join(json.dumps({**line, "ts": old}) + "\n" for line in lines))
            os.utime(ledger, (old, old))
        for path in [route_file, *self.root.joinpath(".runtime/artifact-producer/v1/cycles").glob("*.json"),
                     *self.root.joinpath("campaigns").rglob("*")]:
            os.utime(path, (old, old))

    def spawn(self, *argv):
        proc = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(300)", *argv])
        self.processes.append(proc)
        for _ in range(100):   # until exec replaced the forked parent's argv
            if b"time.sleep(300)" in Path(f"/proc/{proc.pid}/cmdline").read_bytes():
                break
            time.sleep(0.02)
        return proc

    def claude_record(self, proc, sid):
        (self.sessions / f"{proc.pid}.json").write_text(json.dumps(
            {"pid": proc.pid, "sessionId": sid, "procStart": _proc_start(proc.pid)}))

    def claude_alive(self, sid):
        proc = self.spawn()
        self.claude_record(proc, sid)
        return proc

    def claude_crashed(self, sid):
        """A Claude session that died without removing its registry record."""
        proc = self.spawn()
        self.claude_record(proc, sid)
        proc.kill(); proc.wait()

    def raise_gate(self, route):
        """Journal BLOCKED_HUMAN_GATE the way `workflow-supervisor.py gate --block` does."""
        import workflow_state
        gate = (route.get("human_gate_bindings") or [{}])[0].get("gate") or "plan-approval"
        ledger = workflow_state.WorkflowLedger(route["route_id"], route["route_hash"], jobs=str(self.jobs))
        with ledger.lock():
            for state in ("READY", "RUNNING"):
                ledger.set_workflow_state(state, actor="test")
            ledger.set_workflow_state("BLOCKED_HUMAN_GATE", evidence={"gate": gate, "artifact": "/x/q.md"},
                                      actor="test")
        self.assertEqual(workflow_state.human_gate_resolution(ledger.journal(), gate)["status"], "blocked")

    # ---- closes, claiming no proof -----------------------------------------
    def test_idle_direct_closes_without_claiming_proof_and_abandons_its_cycle(self):
        route_file, route = self.compose("quiet", "opencode", "ses-oc")
        record = self.write_artifact(route)
        self.age(route_file, "opencode", "ses-oc", TWO_DAYS)
        self.sweep()
        self.assertIsNone(self.outcome(route_file))   # a week of quiet first
        self.age(route_file, "opencode", "ses-oc")
        self.assertIn("route_autoclose closed=1", self.sweep())
        outcome = self.outcome(route_file)
        self.assertEqual((outcome["autoclose"]["reason"], outcome["autoclose"]["proof"]), ("idle", "not-claimed"))
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertNotIn("inline_finish_id", outcome)
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))

    def test_idle_direct_without_output_drops_its_empty_cycle(self):
        route_file, route = self.compose("empty", "codex", "session-9")
        record = self.cycle(route)
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIs(self.outcome(route_file)["terminal_gate_proven"], False)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "abandoned")

    def test_crashed_claude_session_closes_after_an_hour_of_quiet(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("orphan", "claude", "claude-crashed")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        self.age(route_file, "claude", "claude-crashed", TWO_HOURS)
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "session-ended")

    def test_quick_route_without_owner_closes_with_its_honest_proof(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("solo", "claude", "claude-crashed", intensity="quick", start=False)
        self.assertEqual(route["effective_intensity"], "quick")
        self.age(route_file, "claude", "claude-crashed", TWO_HOURS)
        self.sweep()
        outcome = self.outcome(route_file)
        self.assertEqual(outcome["autoclose"]["reason"], "session-ended")
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertTrue(outcome["terminal_gates"])

    def test_cycle_left_open_after_its_route_closed_is_sealed(self):
        route_file, route = self.compose("closed-by-hand", "codex", "session-1")
        record = self.write_artifact(route)
        closed = self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.sweep()
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")   # closed just now
        old = time.time() - TWO_HOURS
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        self.sweep()
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))

    def test_r7_interrupted_autoclose_is_finished_by_the_next_sweep(self):
        route_file, route = self.compose("interrupted", "codex", "session-1")
        record = self.write_artifact(route)
        # The sweep died between writing the closure and sealing the cycle.
        script = ("import importlib.util, json, sys; from pathlib import Path; "
                  "spec = importlib.util.spec_from_file_location('cr', sys.argv[1]); "
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                  "p = Path(sys.argv[2]); r = m.verify_route(json.loads(p.read_text()), None, "
                  "allow_stale_registry=True); m.close_route(r, p, None, 'x', allow_unproven=True, "
                  "autoclose={'reason': 'idle', 'trigger': 'compose', 'closed_by': 'runtime', "
                  "'proof': 'not-claimed'})")
        done = subprocess.run([sys.executable, "-c", script, str(CAP), str(route_file)], env=self.env,
                              cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")
        self.assertIn("cycles_sealed=1", self.sweep())   # at once: the closure is the runtime's own
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))
        self.assertNotIn("errors=1", self.sweep())
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / route["route_id"]).exists())
        closed = self.campaign("campaign-close", record["campaign_id"], "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)

    def test_g_a_held_producer_lock_defers_sealing_to_a_later_sweep(self):
        import artifact_admission
        route_file, route = self.compose("locked", "codex", "session-9")
        record = self.write_artifact(route)
        self.age(route_file, "codex", "session-9")
        fd = artifact_admission._acquire_lock(self.root, 1.0)
        try:
            started = time.monotonic()
            self.sweep()
            self.assertLess(time.monotonic() - started, 30)
        finally:
            artifact_admission._release_lock(self.root, fd)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")
        self.sweep()
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "sealed")

    def test_g_a_running_sweep_makes_the_next_one_skip(self):
        route_file, _route = self.compose("busy-sweep", "codex", "session-9")
        self.age(route_file, "codex", "session-9")
        with (self.root / ".runtime/route-autoclose.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.sweep()
            self.assertIsNone(self.outcome(route_file))
        self.sweep()
        self.assertIsNotNone(self.outcome(route_file))

    # ---- never closes live work ---------------------------------------------
    def test_r1_second_campaign_of_a_live_session_leaves_the_first_alone(self):
        self.claude_alive("S")
        first_file, first = self.compose("first", "claude", "S", campaign="k1")
        record = self.write_artifact(first, "draft.md", "half-written draft\n")
        self.compose("second", "claude", "S", campaign="k2")
        self.assertIsNone(self.outcome(first_file))
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")

    def test_r6_sub_agent_sharing_the_session_id_leaves_the_parent_route_alone(self):
        self.claude_alive("parent")
        parent_file, _parent = self.compose("parent-work", "claude", "parent")
        self.compose("subagent-work", "claude", "parent")
        self.assertIsNone(self.outcome(parent_file))

    def test_r2_route_waiting_on_a_human_gate_is_never_closed(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("gated", "claude", "claude-crashed", intensity="quick", start=False)
        self.raise_gate(route)
        self.age(route_file, "claude", "claude-crashed")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_r3_ended_owner_at_a_gate_is_not_closed_by_the_next_compose(self):
        route_file, route = self.compose("gated2", "codex", "session-1", intensity="quick", start=False)
        self.raise_gate(route)
        metadata = f"attempt_id=att-owner-ended,worker_type=owner,owner_route_id={route['route_id']}"
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - TWO_HOURS))
        self.jobs.write_text(f"{old}\tdone\t{self.repo}\t{self.repo}\towner\t{metadata}\n")
        self.age(route_file, "codex", "session-1")
        self.compose("side-question", "codex", "session-1")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_r4_claude_session_this_host_cannot_see_waits_for_the_day_rule(self):
        self.claude_alive("S-live")
        route_file, _route = self.compose("busy", "claude", "S-live")
        self.age(route_file, "claude", "S-live", TWO_HOURS)
        other = self.base / "other-profile"
        (other / "sessions").mkdir(parents=True)
        self.env["CLAUDE_CONFIG_DIR"] = str(other)   # another host / profile
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_attempt_the_registry_still_holds_is_never_closed(self):
        route_file, route = self.compose("owned", "codex", "session-9", intensity="quick", start=False)
        metadata = f"attempt_id=att-live-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))   # judged again only after RECHECK_SECONDS
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def test_n6_pass_owner_with_pending_settlement_is_left_to_the_runtime(self):
        route_file, route = self.compose("owned", "codex", "session-9", intensity="quick", start=False)
        slot = self.root / ".runtime/terminal-commits/v1" / route["route_id"] / "att-owner"
        slot.mkdir(parents=True)
        (slot / "producer-binding.json").write_text("{}")   # written at owner launch
        metadata = (f"attempt_id=att-owner,worker_type=owner,dispatch_depth=1,owner_route_id={route['route_id']},"
                    f"owner_route_file={route_file},owner_route_hash={route['route_hash']},"
                    "workflow_completion=runtime-v1,failure_class=pass")
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        # dispatch_terminal_commit.owner_completion_pending: pending or unknown both keep the route.
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_live_resource_run_bound_to_the_route_is_never_closed(self):
        route_file, route = self.compose("training", "codex", "session-9")
        runner = self.spawn("train.py")
        identity = {"pid": runner.pid, "starttime": _proc_start(runner.pid),
                    "command_hash": hashlib.sha256(Path(f"/proc/{runner.pid}/cmdline").read_bytes()).hexdigest()}
        registry = self.base / "resource-runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {
            "train": {**identity, "route_file": str(route_file), "route_id": route["route_id"],
                      "node": "inline", "status": "running"}}}))
        self.resource_index.write_text(json.dumps({"schema_version": 1, "registries": {
            "r": {"path": str(registry)}}}))
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        runner.kill(); runner.wait()
        self.sweep()   # gone here, but a run on another host is gone here too: no end record, still live
        self.assertIsNone(self.outcome(route_file))
        data = json.loads(registry.read_text())
        data["runs"]["train"].update(status="failed", exit_code=-9)   # the runner records the end
        registry.write_text(json.dumps(data))
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def remote_run(self, route_file, **extra):
        """A run registered from another host: its pid means nothing here."""
        log = self.repo / "runs" / "train.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("epoch 1\n")
        registry = self.base / "runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"r1": {
            "run_id": "r1", "pid": 4000000, "starttime": "123", "command_hash": "ab" * 32,
            "cwd": str(self.repo), "log": str(log), "sentinel": str(log) + ".exit",
            "route": str(route_file), "node": "full-run", "status": "running", **extra}}}))
        self.resource_index.write_text(json.dumps({"schema_version": 1, "registries": {
            "k": {"path": str(registry)}}}))
        return log

    def test_n2_run_on_another_host_counts_as_live_until_it_records_an_end(self):
        route_file, route = self.compose("remote-training", "codex", "session-b")
        log = self.remote_run(route_file)
        self.age(route_file, "codex", "session-b")
        old = time.time() - EIGHT_DAYS
        os.utime(log, (old, old))
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        Path(str(log) + ".exit").write_text("0")   # the wrapper's exit sentinel
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def test_n2_a_bound_runs_fresh_log_counts_as_activity(self):
        route_file, route = self.compose("remote-log", "codex", "session-b")
        self.remote_run(route_file, status="succeeded")   # ended, but it just wrote
        self.age(route_file, "codex", "session-b")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_n7_unreadable_resource_index_closes_nothing(self):
        route_file, route = self.compose("training-local", "codex", "session-b")
        self.resource_index.write_text('{"schema_version": 1, "registries": ')
        self.age(route_file, "codex", "session-b")
        self.assertNotIn("route_autoclose closed", self.sweep())
        self.assertIsNone(self.outcome(route_file))

    def test_n1_week_rule_covers_a_gate_this_host_cannot_see(self):
        route_file, route = self.compose("gated-elsewhere", "codex", "session-b", intensity="quick", start=False)
        self.age(route_file, "codex", "session-b", TWO_DAYS)
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_cycle_a_live_process_writes_into_is_never_closed(self):
        route_file, route = self.compose("logging", "codex", "session-9")
        record = self.write_artifact(route, "train.log", "epoch 1\n")
        log = self.cycle_dir(record) / "artifacts" / "documents" / "train.log"
        writer = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'a'); "
                                   "time.sleep(300)", str(log)])
        self.processes.append(writer)
        time.sleep(0.5)
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        writer.kill(); writer.wait()
        self.later()
        self.sweep()
        self.assertIsNotNone(self.outcome(route_file))

    def test_live_and_resumed_claude_sessions_are_never_closed(self):
        self.claude_alive("claude-live")
        live_file, _live = self.compose("busy", "claude", "claude-live")
        resumed_file, _resumed = self.compose("resumed", "claude", "claude-old")
        proc = self.spawn("--resume", "claude-old")
        self.claude_record(proc, "claude-new")
        self.age(live_file, "claude", "claude-live")
        self.age(resumed_file, "claude", "claude-old")
        self.sweep()
        self.assertIsNone(self.outcome(live_file))
        self.assertIsNone(self.outcome(resumed_file))

    def autoclosed(self):
        route_file, route = self.compose("overnight", "codex", "session-b")
        record = self.write_artifact(route)
        self.age(route_file, "codex", "session-b")
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")
        return route_file, route, record

    def test_n3_finish_on_an_automatically_closed_route_succeeds(self):
        route_file, route, record = self.autoclosed()
        summary = self.base / "summary.md"
        summary.write_text("done\n")
        done = self.run_as("codex", "session-b", "finish", "--route", route_file, "--evidence",
                           self.cycle_dir(record) / "artifacts/documents/report.md", "--summary-file", summary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "already-closed-automatically")
        self.assertIn("already closed automatically", done.stderr)

    def test_n3_start_on_an_automatically_closed_route_hands_back_a_compose(self):
        route_file, route, _record = self.autoclosed()
        done = self.run_as("codex", "session-b", "start", "--route", route_file, "--jobs", self.jobs)
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((receipt["state"], receipt["parent_next"]), ("autoclosed", "compose"))
        again = subprocess.run(receipt["parent_next_command"], shell=True, cwd=self.repo, capture_output=True,
                               text=True, env={**self.env, "CODEX_THREAD_ID": "session-b"})
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotEqual(json.loads(again.stdout.strip().splitlines()[-1])["route_id"], route["route_id"])

    def test_review3_worker_environment_cannot_move_a_test_into_a_real_root(self):
        decoy = self.base / "decoy-artifact-root"
        decoy.mkdir()
        worker = {"AGENT_ARTIFACT_ROOT": str(decoy), "AGENT_ARTIFACT_OUTPUT_DIR": str(decoy / "out"),
                  "AGENT_ARTIFACT_CYCLE_ID": "cyc_" + "d" * 32, "AGENT_ARTIFACT_CAMPAIGN_KEY": "k1",
                  "AGENT_WORKFLOW_ROOT": str(decoy / "workflow")}
        with mock.patch.dict(os.environ, worker):
            self.assertFalse(set(worker) & set(isolated_env()))
            route_file, route, _record = self.autoclosed()
            done = self.run_as("codex", "session-b", "start", "--route", route_file, "--jobs", self.jobs)
        command = json.loads(done.stdout.strip().splitlines()[-1])["parent_next_command"]
        # The leak's mechanism: the compose command a returning session runs, under a worker env.
        again = subprocess.run(command, shell=True, cwd=self.repo, capture_output=True, text=True,
                               env={**self.env, **worker, "CODEX_THREAD_ID": "session-b"})
        self.assertEqual(again.returncode, 0, again.stderr)
        receipt = json.loads(again.stdout.strip().splitlines()[-1])
        self.assertTrue(receipt["route_file"].startswith(str(self.root)), receipt["route_file"])
        self.assertEqual(sorted(decoy.rglob("*")), [])

    def test_review3_write_into_an_automatically_closed_cycle_names_the_way_forward(self):
        route_file, route, record = self.autoclosed()
        target = self.cycle_dir(record) / "artifacts" / "documents" / "late.md"
        done = self.run_as("codex", "session-b", "check-write", "--artifact-root", self.root,
                           "--file", target, program=PRODUCER)
        verdict = json.loads(done.stdout)
        self.assertEqual(verdict["verdict"], "deny")
        self.assertIn("compose the work again", verdict["hint"])

    def test_review3_a_cycle_that_cannot_seal_is_not_retried_until_its_evidence_changes(self):
        route_file, route = self.compose("stuck", "codex", "session-1")
        record = self.write_artifact(route)
        self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        old = time.time() - TWO_HOURS
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        cycle_file = self.root / ".runtime/artifact-producer/v1/cycles" / (record["cycle_id"] + ".json")
        stuck = json.loads(cycle_file.read_text())
        stuck["parent_cycle_id"] = "cyc_" + "e" * 32   # a parent that never seals
        cycle_file.write_text(json.dumps(stuck))
        self.assertIn("cycles_left_open=1", self.sweep())
        memory = json.loads((self.root / ".runtime/route-autoclose/state.json").read_text())
        self.assertIn(record["cycle_id"], memory["unsealable"])
        self.assertNotIn("cycles_left_open", self.sweep())   # remembered: no second attempt
        os.utime(cycle_file, None)                          # new evidence
        self.assertIn("cycles_left_open=1", self.sweep())

    def test_review3_a_kept_route_is_judged_again_only_after_the_recheck_interval(self):
        import route_autoclose
        route_file, route = self.compose("owned-later", "codex", "session-9", intensity="quick", start=False)
        metadata = f"attempt_id=att-live-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        state = json.loads((self.root / ".runtime/route-autoclose/state.json").read_text())
        row = state["kept"]["route:" + route["route_id"]]
        self.assertEqual(row["reason"], "owner-live")
        self.assertAlmostEqual(row["until"] - time.time(), route_autoclose.RECHECK_SECONDS, delta=120)

    def test_non_automatic_closure_keeps_its_existing_finish_refusal(self):
        route_file, route = self.compose("hand-closed", "codex", "session-1")
        record = self.write_artifact(route)
        self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        summary = self.base / "summary.md"
        summary.write_text("done\n")
        done = self.run_as("codex", "session-1", "finish", "--route", route_file, "--evidence",
                           self.cycle_dir(record) / "artifacts/documents/report.md", "--summary-file", summary)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("finish-route-already-closed", done.stderr + done.stdout)

    def test_the_sweeping_session_keeps_its_own_routes(self):
        route_file, _route = self.compose("mine", "codex", "session-1")
        self.age(route_file, "codex", "session-1")
        self.compose("next", "codex", "session-1")
        self.assertIsNone(self.outcome(route_file))

    def test_e_an_oversized_cycle_counts_as_active(self):
        route_file, route = self.compose("big", "codex", "session-9")
        record = self.write_artifact(route)
        bulk = self.cycle_dir(record) / "artifacts" / "bulk"
        bulk.mkdir()
        for index in range(2100):
            (bulk / f"{index}.txt").write_text("x")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_f_status_stays_read_only(self):
        route_file, route = self.compose("listed", "codex", "session-9")
        self.age(route_file, "codex", "session-9")
        self.assertIn(route["route_id"], self.status())
        self.assertIsNone(self.outcome(route_file))

    # ---- campaign close -------------------------------------------------------
    def test_e_campaign_close_by_its_worker_is_not_refused_by_its_open_direct(self):
        route_file, route = self.compose("member", "codex", "session-1", campaign="k2")
        campaign = self.write_artifact(route)["campaign_id"]
        status = json.loads(self.campaign("campaign-status", campaign).stdout)
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-not-sealed")
        closed = self.run_as("codex", "session-1", "campaign-close", "--artifact-root", self.root,
                             "--campaign", campaign, "--reason", "report shipped", program=PRODUCER)
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")
        self.assertEqual(json.loads(self.campaign("campaign-status", campaign).stdout)["state"], "satisfied")

    def test_e_campaign_close_takes_an_hour_quiet_member_but_not_fresh_work(self):
        route_file, route = self.compose("member", "opencode", "ses-oc", campaign="k3")
        campaign = self.write_artifact(route)["campaign_id"]
        fresh = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertNotEqual(fresh.returncode, 0)
        self.assertIsNone(self.outcome(route_file))
        self.age(route_file, "opencode", "ses-oc", TWO_HOURS)
        closed = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")

    def test_review4_campaign_close_judges_its_members_despite_the_recheck_interval(self):
        route_file, route = self.compose("member-owned", "codex", "session-9", campaign="k4")
        campaign = self.write_artifact(route)["campaign_id"]
        metadata = f"attempt_id=att-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9", TWO_HOURS)
        refused = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertNotEqual(refused.returncode, 0)   # the owner still runs
        self.assertIsNone(self.outcome(route_file))
        # The owner just ended; the user closes the campaign within RECHECK_SECONDS.
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        closed = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")

    # ---- the checkout and everything outside the artifact root ----------------
    def test_r5_checkout_bytes_and_index_are_untouched_and_git_is_only_read(self):
        shim_dir = self.base / "shim"
        shim_dir.mkdir()
        log = self.base / "git.log"
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        (shim_dir / "git").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(log)
                                      + "\nexec " + real_git + " \"$@\"\n")
        (shim_dir / "git").chmod(0o755)
        route_file, route = self.compose("edited", "codex", "session-9")
        campaign = self.write_artifact(route)["campaign_id"]
        (self.repo / "README.md").write_text("user edit in progress\n", encoding="utf-8")
        os.utime(self.repo / "README.md", (time.time() + 5, time.time() + 5))   # stat-stale index
        self.age(route_file, "codex", "session-9")
        index = self.repo / ".git" / "index"
        before = {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        index_before = (index.stat().st_mtime_ns, index.read_bytes())
        self.env["PATH"] = str(shim_dir) + os.pathsep + self.env["PATH"]
        closed = self.campaign("campaign-status", campaign)
        self.assertIn("route_autoclose closed=1", closed.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}, before)
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), index_before)
        calls = log.read_text().splitlines() if log.is_file() else []
        self.assertTrue(all("rev-parse" in call.split() for call in calls), calls)

    def test_r8_a_sweep_writes_nothing_outside_the_artifact_root(self):
        route_file, route = self.compose("outside", "codex", "session-9")
        campaign = self.write_artifact(route)["campaign_id"]
        self.age(route_file, "codex", "session-9")

        def snapshot():
            return {str(path): path.stat().st_mtime_ns for path in self.base.rglob("*")
                    if path.is_file() and not path.is_relative_to(self.root) and not path.is_relative_to(self.repo)}

        before = snapshot()
        done = self.campaign("campaign-status", campaign)
        self.assertIn("route_autoclose closed=1", done.stderr)
        after = snapshot()
        self.assertEqual(sorted(key for key in after if before.get(key) != after[key]), [])


if __name__ == "__main__":
    unittest.main()
