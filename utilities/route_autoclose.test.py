#!/usr/bin/env python3
"""The runtime closes routes nobody works on any more (route_autoclose.py).

Every case drives the public CLI (`compose`, `start`, `status`,
`campaign-status`, `campaign-close`) in an isolated artifact root, route-chain
ledger, dispatch registry and Claude session registry.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer  # noqa: E402

CAP = ROOT / "utilities/capability-route.py"
PRODUCER = ROOT / "utilities/artifact_producer.py"
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_PARENT_SESSION_ID", "AGENT_ROUTE_FILE",
                "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE", "HEARTING_INLINE_FINISH_CRASH_AT")
SESSION_ENV = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID", "opencode": "OPENCODE_SESSION_ID"}
TWO_DAYS = 2 * 24 * 3600


def _proc_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class RouteAutocloseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="route-autoclose-test-")
        base = Path(self.temp.name)
        self.repo, self.root = base / "repo", base / "artifacts"
        self.repo.mkdir(); self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "commit", "-qm", "base"], check=True)
        self.jobs = base / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.sessions = base / "claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.ledgers = base / "route-chains"
        self.env = {key: value for key, value in os.environ.items() if key not in SESSION_VARS}
        self.env.update({"AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
                         "AGENT_DISPATCH_DEPTH": "0", "XDG_STATE_HOME": str(base / "state"),
                         "FLEET_ROUTE_CHAIN_DIR": str(self.ledgers),
                         "CLAUDE_CONFIG_DIR": str(base / "claude")})
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
        self.sleepers = []

    def tearDown(self):
        for proc in self.sleepers:
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

    def status(self, harness="codex", sid="observer"):
        done = self.run_as(harness, sid, "status", "--artifact-root", self.root, "--open-only")
        self.assertEqual(done.returncode, 0, done.stderr)
        return {row["route_id"] for row in json.loads(done.stdout)}

    def cycle(self, route):
        return artifact_producer.route_cycle_for(self.root, route)

    def cycle_record(self, cycle_id):
        return artifact_producer.read_cycle_record(self.root, cycle_id)

    def write_artifact(self, route, name="report.md", body="the report\n"):
        record = self.cycle(route)
        target = artifact_producer.cycle_dir(self.root, record["campaign_id"], record["cycle_id"],
                                             record) / "artifacts" / "documents" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return record

    def outcome(self, route_file):
        path = route_file.with_name(route_file.stem + ".outcome.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def age(self, route_file, harness, sid):
        """Two days of nothing: route, cycle, and the composer's own ledger."""
        old = time.time() - TWO_DAYS
        ledger = self.ledgers / harness / f"{sid}.jsonl"
        if ledger.is_file():
            lines = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
            ledger.write_text("".join(json.dumps({**line, "ts": old}) + "\n" for line in lines))
            os.utime(ledger, (old, old))
        for path in [route_file, *self.root.joinpath(".runtime/artifact-producer/v1/cycles").glob("*.json"),
                     *self.root.joinpath("campaigns").rglob("*")]:
            os.utime(path, (old, old))

    def claude_alive(self, sid):
        proc = subprocess.Popen(["sleep", "300"])
        self.sleepers.append(proc)
        (self.sessions / f"{proc.pid}.json").write_text(json.dumps(
            {"pid": proc.pid, "sessionId": sid, "procStart": _proc_start(proc.pid)}))
        return proc

    # ---- (a) superseded -------------------------------------------------
    def test_a_same_session_compose_finishes_previous_direct_with_its_artifact(self):
        first_file, first = self.compose("first")
        record = self.write_artifact(first)
        second_file, _ = self.compose("second")
        self.assertIn("route_autoclose closed=1", self.last_stderr)
        outcome = self.outcome(first_file)
        self.assertEqual(outcome["autoclose"]["reason"], "superseded")
        self.assertIs(outcome["terminal_gate_proven"], True)
        self.assertTrue(outcome["inline_finish_id"])
        self.assertEqual(outcome["summary"], "Tighten the report generator.")
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "completed"))
        self.assertIsNone(self.outcome(second_file))

    def test_a_previous_direct_without_artifact_closes_unproven_and_drops_empty_cycle(self):
        first_file, first = self.compose("first")
        record = self.cycle(first)
        self.compose("second")
        outcome = self.outcome(first_file)
        self.assertEqual((outcome["autoclose"]["reason"], outcome["autoclose"]["evidence"]),
                         ("superseded", "none"))
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "abandoned")

    # ---- (b) session ended ------------------------------------------------
    def test_b_ended_claude_session_direct_closes_on_next_status(self):
        route_file, route = self.compose("orphan", "claude", "claude-gone")
        self.write_artifact(route)
        self.assertIn(route["route_id"], self.status())   # an hour of quiet first
        self.age(route_file, "claude", "claude-gone")
        self.assertNotIn(route["route_id"], self.status())
        outcome = self.outcome(route_file)
        self.assertEqual(outcome["autoclose"]["reason"], "session-ended")
        self.assertEqual(outcome["autoclose"]["trigger"], "status")
        self.assertIs(outcome["terminal_gate_proven"], True)

    def test_b_undecidable_session_closes_only_after_two_idle_days(self):
        route_file, route = self.compose("quiet", "opencode", "ses-oc")
        self.assertIn(route["route_id"], self.status())
        self.age(route_file, "opencode", "ses-oc")
        self.assertNotIn(route["route_id"], self.status())
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    # ---- (c) no owner --------------------------------------------------------
    def test_c_quick_route_without_owner_closes_with_honest_proof(self):
        route_file, route = self.compose("solo", "claude", "claude-gone", intensity="quick", start=False)
        self.assertEqual(route["effective_intensity"], "quick")
        self.age(route_file, "claude", "claude-gone")
        self.assertNotIn(route["route_id"], self.status())
        outcome = self.outcome(route_file)
        self.assertEqual(outcome["autoclose"]["reason"], "session-ended")
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertTrue(outcome["terminal_gates"])

    # ---- (d) never close live work -----------------------------------------
    def test_d_attempt_the_registry_still_holds_is_never_closed(self):
        route_file, route = self.compose("owned", "claude", "claude-gone", intensity="quick", start=False)
        metadata = f"attempt_id=att-live-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "claude", "claude-gone")
        self.assertIn(route["route_id"], self.status())
        self.assertIsNone(self.outcome(route_file))
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.assertNotIn(route["route_id"], self.status())

    def test_d_live_claude_session_is_never_closed(self):
        sleeper = self.claude_alive("claude-live")
        route_file, route = self.compose("busy", "claude", "claude-live")
        self.age(route_file, "claude", "claude-live")
        self.assertIn(route["route_id"], self.status())
        sleeper.kill(); sleeper.wait()
        self.assertNotIn(route["route_id"], self.status())
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "session-ended")

    def test_d_resumed_claude_session_keeps_its_old_routes(self):
        route_file, route = self.compose("resumed", "claude", "claude-old")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)",
                                 "--resume", "claude-old"])
        self.sleepers.append(proc)
        (self.sessions / f"{proc.pid}.json").write_text(json.dumps(
            {"pid": proc.pid, "sessionId": "claude-new", "procStart": _proc_start(proc.pid)}))
        self.age(route_file, "claude", "claude-old")
        self.assertIn(route["route_id"], self.status())

    def test_d_cycle_a_live_process_writes_into_is_never_closed(self):
        first_file, first = self.compose("training", "claude", "claude-gone")
        record = self.write_artifact(first, "train.log", "epoch 1\n")
        log = artifact_producer.cycle_dir(self.root, record["campaign_id"], record["cycle_id"],
                                          record) / "artifacts" / "documents" / "train.log"
        writer = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'a'); time.sleep(300)",
                                   str(log)])
        self.sleepers.append(writer)
        time.sleep(0.5)
        self.age(first_file, "claude", "claude-gone")
        self.assertIn(first["route_id"], self.status())
        second_file, _ = self.compose("next", "claude", "claude-gone")
        self.assertIsNone(self.outcome(first_file))
        writer.kill(); writer.wait()
        self.age(first_file, "claude", "claude-gone")
        self.assertNotIn(first["route_id"], self.status())

    def test_d_status_from_the_composing_session_keeps_its_routes(self):
        route_file, route = self.compose("mine", "codex", "session-1")
        self.age(route_file, "codex", "session-1")
        self.assertIn(route["route_id"], self.status("codex", "session-1"))

    def test_d_supersede_leaves_a_non_direct_route_that_never_ran(self):
        waiting_file, waiting = self.compose("waiting", intensity="quick", start=False)
        self.compose("next")
        self.assertIsNone(self.outcome(waiting_file))

    def test_b_cycle_left_open_after_its_route_closed_is_sealed(self):
        route_file, route = self.compose("closed-by-hand", "codex", "session-1")
        record = self.write_artifact(route)
        closed = self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")
        self.status()
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")   # closed just now
        old = time.time() - 2 * 3600
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        self.status()
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))

    # ---- (e) campaign close -------------------------------------------------
    def campaign(self, verb, campaign, sid="observer", *extra):
        return self.run_as("codex", sid, verb, "--artifact-root", self.root, "--campaign", campaign,
                           *extra, program=PRODUCER)

    def test_e_campaign_close_by_its_worker_is_not_refused_by_its_open_direct(self):
        route_file, route = self.compose("member", campaign="k2")
        campaign = self.write_artifact(route)["campaign_id"]
        status = json.loads(self.campaign("campaign-status", campaign).stdout)
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-not-sealed")
        closed = self.campaign("campaign-close", campaign, "session-1", "--reason", "report shipped")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")
        self.assertEqual(json.loads(self.campaign("campaign-status", campaign).stdout)["state"], "satisfied")

    def test_e_campaign_close_takes_a_quiet_direct_but_not_fresh_work(self):
        route_file, route = self.compose("member", "opencode", "ses-oc", campaign="k3")
        campaign = self.write_artifact(route)["campaign_id"]
        fresh = self.campaign("campaign-close", campaign, "observer", "--reason", "done")
        self.assertNotEqual(fresh.returncode, 0)
        self.assertIsNone(self.outcome(route_file))
        self.age(route_file, "opencode", "ses-oc")
        closed = self.campaign("campaign-close", campaign, "observer", "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")

    # ---- (f) source tree ----------------------------------------------------
    def _tree(self):
        return {str(path.relative_to(self.repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.repo.rglob("*")) if path.is_file() and ".git" not in path.parts}

    def test_f_autoclose_leaves_the_checkout_untouched_and_records_tracked_dirt(self):
        first_file, first = self.compose("first")
        self.write_artifact(first)
        (self.repo / "README.md").write_text("user edit in progress\n", encoding="utf-8")
        before = self._tree()
        self.compose("second")
        self.assertEqual(self._tree(), before)
        outcome = self.outcome(first_file)
        self.assertIs(outcome["terminal_gate_proven"], True)
        self.assertEqual(outcome["autoclose"]["tracked_dirt"], ["README.md"])
        self.assertEqual((self.repo / "README.md").read_text(), "user edit in progress\n")


if __name__ == "__main__":
    unittest.main()
