#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from route_identity import route_hash


ROOT = Path(__file__).resolve().parents[1]
WATCH = ROOT / "utilities" / "dispatch-orphan-watch.py"


class OrphanWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.jobs = self.base / "jobs.log"
        self.route_id = "rt-watch"
        self.route = self.base / "route.json"
        route = {
            "route_id": self.route_id,
            "nodes": [
                {"id": "plan", "depends_on": []},
                {"id": "execute", "depends_on": ["plan"]},
            ],
        }
        route["route_hash"] = route_hash(route)
        self.route.write_text(json.dumps(route))
        marker_dir = self.home / ".dispatch" / "completion" / self.route_id
        marker_dir.mkdir(parents=True)
        (marker_dir / "plan.json").write_text(json.dumps({"node_id": "plan"}))
        self.supervisor_state = (
            self.jobs.parent / "supervisor-state" / "att-watch.json"
        )
        self.supervisor_state.parent.mkdir(parents=True)
        self.supervisor_state.write_text("{}", encoding="utf-8")
        self.supervisor_lease = (
            self.jobs.parent / "supervisor-state" / "att-watch.lease"
        )
        self.supervisor_lease.parent.mkdir(parents=True, exist_ok=True)
        self.supervisor_lease.write_text("stale", encoding="utf-8")
        self.owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.owner_start = self.proc_start(self.owner.pid)
        self.observer_namespace = (Path("/proc/self/ns/pid").readlink())
        self.children = []

    def tearDown(self):
        if self.owner.poll() is None:
            self.owner.kill()
        self.owner.wait()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
        self.temp.cleanup()

    @staticmethod
    def proc_start(pid):
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        return raw[raw.rfind(")") + 2:].split()[19]

    def write_rows(self, completed_owner=False, child=None, supervised=True):
        status = "done" if completed_owner else "open"
        child_pid = child.pid if child is not None else 99999999
        child_start = self.proc_start(child.pid) if child is not None else self.owner_start
        child_group = f",pgid={child_pid},pid_observer_ns={self.observer_namespace},pid_ns={self.observer_namespace}"
        current = (
            "attempt_schema_version=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless"
        )
        supervised_metadata = (
            ",harness=codex,completion_delivery=app-server-supervised,"
            "supervisor_lease=flock-v1,"
            f"supervisor_lease_file={self.supervisor_lease}"
            if supervised
            else ""
        )
        self.jobs.write_text(
            f"2026-07-19T00:00:00Z\t{status}\t/r\t/w\towner\t"
            f"{current},dispatch_depth=1,worker_type=owner,attempt_id=att-watch,"
            "parent_sid=parent-test,parent_completion_delivery=claude-parent-runtime,"
            f"pid={self.owner.pid},pid_start={self.owner_start},pgid={self.owner.pid},"
            f"pid_observer_ns={self.observer_namespace},"
            f"pid_ns={self.observer_namespace}"
            f"{supervised_metadata}\n"
            "2026-07-19T00:00:01Z\topen\t/r\t/w\tchild\t"
            f"{current},dispatch_depth=2,route_id={self.route_id},"
            f"route_file={self.route},route_hash={json.loads(self.route.read_text())['route_hash']},"
            "route_node=execute,attempt_id=att-child,"
            f"parent=owner,parent_attempt_id=att-watch,pid={child_pid},"
            f"pid_start={child_start}{child_group}\n"
        )

    def watcher(self):
        process = subprocess.Popen([
            sys.executable, str(WATCH),
            "--jobs", str(self.jobs),
            "--agent-home", str(self.home),
            "--attempt-id", "att-watch",
            "--pid", str(self.owner.pid),
            "--pid-start", self.owner_start,
            "--interval", "0.02",
        ])
        def reap():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(reap)
        return process

    def test_owner_exit_reaps_exact_child_group_and_closes_both_rows(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.children.append(child)
        self.write_rows(child=child)
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("\tdone\t/r\t/w\towner\t", text)
        self.assertIn("note=dead-parent-orphaned", text)
        self.assertIn("\tdone\t/r\t/w\tchild\t", text)
        self.assertIn("note=dead-parent-terminated", text)
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.supervisor_state.exists())
        self.assertFalse(self.supervisor_lease.exists())

    def test_owner_exit_before_any_child_closes_the_open_row(self):
        self.write_rows(supervised=False)
        lines = self.jobs.read_text().splitlines()
        self.jobs.write_text(lines[0] + "\n")
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("\tdone\t/r\t/w\towner\t", text)
        self.assertIn("note=dead-exact-pid", text)
        self.assertFalse(self.supervisor_state.exists())
        self.assertTrue(self.supervisor_lease.exists())

    def test_terminal_owner_waits_then_finishes_child_cleanup(self):
        self.write_rows(completed_owner=True)
        self.route.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route_id,
            "route_hash": json.loads(self.route.read_text())["route_hash"],
            "terminal_gate_proven": True,
        }))
        before = self.jobs.read_text()
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        self.assertTrue(self.supervisor_state.exists())
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        after = self.jobs.read_text()
        self.assertEqual(after.splitlines()[0], before.splitlines()[0])
        self.assertIn("\tdone\t/r\t/w\tchild\t", after)
        self.assertFalse(self.supervisor_state.exists())
        self.assertFalse(self.supervisor_lease.exists())

    def test_fleet_kill_waits_then_closes_exact_child_and_protects_foreign_sibling(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.children.extend((child, sibling))
        self.write_rows(completed_owner=True, child=child)
        current = (
            "attempt_schema_version=2,transport=headless,execution_surface=registered-headless,"
            "registered_worker=1,fallback_hop=same-harness-headless"
        )
        self.jobs.write_text(self.jobs.read_text() +
            "2026-07-19T00:00:02Z\topen\t/r\t/w\tforeign\t"
            f"{current},dispatch_depth=2,route_id={self.route_id},route_node=test,"
            f"attempt_id=att-foreign,parent=other,parent_attempt_id=att-other,"
            f"pid={sibling.pid},pid_start={self.proc_start(sibling.pid)},"
            "pid_scope=namespace-local,launch_lifecycle=foreground-scoped\n")
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        self.assertIsNone(child.poll())
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("att-child", text)
        child_line = next(line for line in text.splitlines() if "att-child" in line)
        sibling_line = next(line for line in text.splitlines() if "att-foreign" in line)
        self.assertIn("note=dead-parent-terminated", child_line)
        self.assertIn("\topen\t", sibling_line)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(sibling.poll())

    def test_killed_owner_also_waits_for_extinction_before_namespace_close(self):
        self.write_rows(completed_owner=True)
        text = self.jobs.read_text()
        text = text.replace("\tdone\t/r\t/w\towner\t", "\tkilled\t/r\t/w\towner\t")
        text = text.replace(
            f"pid_start={self.owner_start},pgid={self.owner.pid},",
            f"pid_start={self.owner_start},pgid={self.owner.pid},pid_scope=namespace-local,",
            1,
        )
        text = text.replace(
            "pid_start=1\n",
            "pid_start=1,pid_scope=namespace-local,"
            "launch_lifecycle=foreground-scoped\n",
        )
        self.jobs.write_text(text)
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        child_line = next(line for line in self.jobs.read_text().splitlines()
                          if "att-child" in line)
        self.assertIn("\topen\t", child_line)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 70)
        self.assertTrue(self.supervisor_state.exists())
        notices = list((self.jobs.parent / "pending-delivery").glob("**/delivery-*.json"))
        self.assertEqual(len(notices), 1)
        self.assertEqual(json.loads(notices[0].read_text())["receipt"]["reason"], "supervisor-exited")
        child_line = next(line for line in self.jobs.read_text().splitlines()
                          if "att-child" in line)
        self.assertIn("note=dead-parent-exited", child_line)

    def test_non_orphan_429_exit_is_reconciled_as_capacity(self):
        log = self.base / "owner.claude.jsonl"
        log.write_text(json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've reached your Fable 5 limit",
        }) + "\n", encoding="utf-8")
        current = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,"
            "harness=claude"
        )
        self.jobs.write_text(
            "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
            f"{current},attempt_id=att-watch,pid={self.owner.pid},"
            f"pid_start={self.owner_start},pgid={self.owner.pid},log_file={log}\n"
        )
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/r\t/w\towner\t", registry)
        self.assertIn("note=dead-capacity", registry)
        self.assertIn("failure_class=capacity", registry)
        self.assertTrue(self.supervisor_lease.exists())

    def test_non_orphan_signal_exit_without_envelope_is_still_closed(self):
        current = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,harness=codex"
        )
        self.jobs.write_text(
            "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
            f"{current},attempt_id=att-watch,pid={self.owner.pid},"
            f"pid_start={self.owner_start},pgid={self.owner.pid}\n"
        )
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/r\t/w\towner\t", registry)
        self.assertIn("note=dead-protocol", registry)
        self.assertTrue(self.supervisor_lease.exists())


def load_watch_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_orphan_watch_unit", WATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdleOwnerWatchTest(unittest.TestCase):
    """A live owner is watched by PID; the registry is judged only on change."""

    ROW = ("2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
           "attempt_schema_version=2,dispatch_depth=1,worker_type=owner,attempt_id=att-idle{extra}\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.jobs.write_text("other\trow\n" * 50 + self.ROW.format(extra=""), encoding="utf-8")
        self.watch = load_watch_module()
        self.now = [100.0]
        real_record = self.watch.attempt_record
        self.records = mock.patch.object(self.watch, "attempt_record", side_effect=real_record)
        self.reads = self.records.start()
        self.addCleanup(self.records.stop)
        self.close = mock.patch("route_parent_close.row_requested",
                                side_effect=lambda meta, jobs: meta.get("parent_close_requested") == "1")
        self.judged = self.close.start()
        self.addCleanup(self.close.stop)
        clock = mock.patch.object(self.watch.time, "monotonic", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    def rewrite(self, text):
        # A rewrite with a new inode, as the registry writer replaces it.
        replacement = self.jobs.with_suffix(".next")
        replacement.write_text(text, encoding="utf-8")
        replacement.replace(self.jobs)

    def test_registry_refresh_keeps_the_passive_judgment_inside_one_read_scope(self):
        import dispatch_registry_cache
        import route_parent_close
        registry = self.watch.RegistryObservation(self.jobs, "att-idle")
        observed = []
        with mock.patch.object(
                route_parent_close, "row_requested",
                side_effect=lambda *_args: observed.append(
                    dispatch_registry_cache.registry_scope_active()) or False):
            registry.refresh(force=True)
        self.assertEqual(observed, [True])

    def test_unchanged_registry_is_not_reparsed_and_close_intent_has_a_backstop(self):
        registry = self.watch.RegistryObservation(self.jobs, "att-idle")
        for _ in range(10):
            self.assertEqual(registry.refresh()[0], "open")
            self.now[0] += 2.0
        self.assertEqual(self.reads.call_count, 1)
        # 20 s idle: only the first judgment.
        self.assertEqual(self.judged.call_count, 1)
        self.now[0] += self.watch.CLOSE_INTENT_BACKSTOP_SECONDS
        registry.refresh()
        self.assertEqual((self.reads.call_count, self.judged.call_count), (1, 2))

        # Another row changed: the watched row is re-read, not re-judged.
        self.rewrite("other\tchanged\n" + self.jobs.read_text(encoding="utf-8"))
        registry.refresh()
        self.assertEqual((self.reads.call_count, self.judged.call_count), (2, 2))

        # The watched row itself carries a parent close: judged at once.
        self.rewrite(self.ROW.format(extra=",parent_close_requested=1"))
        status, _metadata, close_requested = registry.refresh()
        self.assertEqual((status, close_requested), ("open", True))
        self.assertEqual((self.reads.call_count, self.judged.call_count), (3, 3))

    def test_watch_reacts_to_parent_close_and_owner_exit_without_polling_registry(self):
        args = SimpleNamespace(jobs=self.jobs, attempt_id="att-idle", pid=4242,
                               pid_start="1", interval=2.0)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.now[0] += seconds
            if len(sleeps) == 20:
                self.rewrite(self.ROW.format(extra=",parent_close_requested=1"))

        with mock.patch.object(self.watch, "process_start", return_value="1"), \
             mock.patch.object(self.watch.time, "sleep", side_effect=sleep), \
             mock.patch("route_parent_close.recover_attempt",
                             return_value={"state": "cancelled"}) as recover, \
             mock.patch.object(self.watch, "_remove_supervisor_state") as removed:
            self.assertEqual(self.watch.watch(args), 0)
        recover.assert_called_once()
        removed.assert_called_once()
        # 40 s with a live owner: one row read and one backstop judgment
        # before the parent close arrived, instead of 20 of each.
        self.assertEqual((self.reads.call_count, self.judged.call_count), (2, 3))

        self.reads.reset_mock()
        self.judged.reset_mock()
        self.rewrite(self.ROW.format(extra=""))
        with mock.patch.object(self.watch, "process_start", side_effect=["1"] * 5 + ["2"]), \
             mock.patch.object(self.watch.time, "sleep", side_effect=sleep), \
             mock.patch.object(self.watch, "observed_owner_lifecycle",
                                    return_value=(None, "", {})), \
             mock.patch.object(self.watch, "reconcile_exact_exit", return_value=0) as exited:
            self.assertEqual(self.watch.watch(args), 0)
        exited.assert_called_once_with(args)
        self.assertEqual((self.reads.call_count, self.judged.call_count), (1, 1))


if __name__ == "__main__":
    unittest.main()
