#!/usr/bin/env python3
"""Real process contention plus fake GitHub head/CI transitions; no live merges."""
import copy
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("merge_line", Path(__file__).with_name("merge-line.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def hold_turn(directory, pr, entered, release, messages, fail):
    try:
        with M.MergeTurn(directory, pr, emit=messages.put, interval=0.01):
            messages.put(("entered", pr))
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timeout")
            if fail:
                raise M.MergeError("CI failed: unit-tests")
    except M.MergeError as exc:
        messages.put(("failed", pr, str(exc)))


class Contention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = mp.get_context("fork")
        self.messages = self.ctx.Queue()
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.is_alive():
                child.kill()
            child.join(3)
        self.messages.close()

    def start(self, pr, fail=False):
        entered, release = self.ctx.Event(), self.ctx.Event()
        process = self.ctx.Process(target=hold_turn,
            args=(self.root, pr, entered, release, self.messages, fail))
        process.start()
        self.children.append(process)
        return process, entered, release

    def queued(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                rows = json.loads((self.root / "line.json").read_text())
            except FileNotFoundError:
                rows = []
            if len(rows) == count:
                return rows
            time.sleep(0.01)
        self.fail("request did not enter the queue")

    def entries(self):
        messages = []
        while True:
            try:
                messages.append(self.messages.get(timeout=0.1))
            except queue.Empty:
                return messages

    def test_simultaneous_prs_wait_in_fifo_order_and_show_predecessor(self):
        first, entered1, release1 = self.start(11)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        third, entered3, release3 = self.start(33)
        self.queued(3)
        self.assertFalse(entered2.is_set())
        self.assertFalse(entered3.is_set())
        release1.set()
        self.assertTrue(entered2.wait(5))
        self.assertFalse(entered3.is_set())
        release2.set()
        self.assertTrue(entered3.wait(5))
        release3.set()
        for process in (first, second, third):
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        messages = self.entries()
        self.assertEqual([m for m in messages if isinstance(m, tuple) and m[0] == "entered"],
                         [("entered", 11), ("entered", 22), ("entered", 33)])
        self.assertIn("merge-line: waiting PR=#22 position=1 ahead=#11", messages)
        self.assertIn("merge-line: waiting PR=#33 position=2 ahead=#22", messages)
        self.assertEqual(json.loads((self.root / "line.json").read_text()), [])

    def test_failed_front_pr_releases_next_turn(self):
        first, entered1, release1 = self.start(11, fail=True)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        release1.set()
        self.assertTrue(entered2.wait(5))
        release2.set()
        first.join(5)
        second.join(5)
        self.assertIn(("failed", 11, "CI failed: unit-tests"), self.entries())
        self.assertEqual(json.loads((self.root / "line.json").read_text()), [])

    def test_killed_holder_releases_flock_and_dead_pid_start_entry(self):
        first, entered1, _ = self.start(11)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        first.kill()
        first.join(5)
        self.assertTrue(entered2.wait(5))
        rows = self.queued(1)
        self.assertEqual(rows[0]["pr"], 22)
        release2.set()
        second.join(5)
        self.assertEqual(second.exitcode, 0)

    def test_reused_pid_with_wrong_start_does_not_hold_queue(self):
        (self.root / "line.json").write_text(json.dumps([
            {"token": "old", "pr": 11, "pid": os.getpid(), "start": "old-start", "holding": True}]))
        with M.MergeTurn(self.root, 22, emit=lambda _: None):
            self.assertEqual(self.queued(1)[0]["pr"], 22)


def snapshot(head="head1", checks=None, **extra):
    return {"state": "OPEN", "isDraft": False, "baseRefName": "main",
            "headRefOid": head, "mergeable": "MERGEABLE", "statusCheckRollup": checks if checks is not None else [
                {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}], **extra}


class FakeGitHub:
    base = "main"

    def __init__(self):
        self.current = snapshot()
        self.main = "base1"
        self.ancestry = {("base1", "head1")}
        self.updates, self.merges = [], []
        self.on_snapshot = self.on_sleep = lambda: None

    def snapshot(self, pr):
        self.on_snapshot()
        return copy.deepcopy(self.current)

    def base_head(self):
        return self.main

    def contains_base(self, base, head):
        return (base, head) in self.ancestry

    def update(self, pr, head):
        self.updates.append((pr, head))
        self.current = snapshot(head=head + "-updated")
        self.ancestry.add((self.main, self.current["headRefOid"]))

    def merge(self, pr, head):
        self.merges.append((pr, head))
        return "merge1"

    def sleep(self, _):
        self.on_sleep()


class HeadCI(unittest.TestCase):
    def run_pr(self, client):
        messages = []
        result = M.merge_pr(client, 123, emit=messages.append, sleep=client.sleep, interval=0)
        return result, messages

    def test_up_to_date_success_reuses_ci_without_update(self):
        client = FakeGitHub()
        self.assertEqual(self.run_pr(client)[0], "merge1")
        self.assertEqual(client.updates, [])
        self.assertEqual(client.merges, [(123, "head1")])

    def test_stale_head_is_updated_before_observing_old_failure(self):
        client = FakeGitHub()
        client.main = "base2"
        client.current["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])

    def test_ci_failure_never_merges(self):
        client = FakeGitHub()
        client.current["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        with self.assertRaisesRegex(M.MergeError, "CI failed: tests"):
            self.run_pr(client)
        self.assertEqual(client.merges, [])

    def test_no_checks_and_pending_checks_wait_for_actual_success(self):
        client = FakeGitHub()
        client.current = snapshot(checks=[])
        seen = []
        def advance():
            seen.append(1)
            if len(seen) == 1:
                client.current["statusCheckRollup"] = [{"name": "tests", "status": "IN_PROGRESS"}]
            else:
                client.current = snapshot()
        client.on_sleep = advance
        self.run_pr(client)
        self.assertEqual(len(seen), 2)
        self.assertEqual(client.merges, [(123, "head1")])

    def test_main_movement_during_ci_rechecks_and_updates_once(self):
        client = FakeGitHub()
        client.current["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        def advance():
            client.main = "base2"
            client.current = snapshot()
        client.on_sleep = advance
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])

    def test_head_push_before_merge_checks_new_head_instead(self):
        client = FakeGitHub()
        calls = []
        def push():
            calls.append(1)
            if len(calls) == 2:
                client.current = snapshot(head="head2")
                client.ancestry.add((client.main, "head2"))
        client.on_snapshot = push
        self.run_pr(client)
        self.assertEqual(client.merges, [(123, "head2")])

    def test_checks_skips_are_normal_but_all_skips_or_cancel_are_not_success(self):
        current = snapshot()
        current["statusCheckRollup"].append({"name": "conditional", "status": "COMPLETED", "conclusion": "SKIPPED"})
        current["statusCheckRollup"].append({"__typename": "StatusContext", "context": "legacy", "state": "SUCCESS"})
        self.assertTrue(M.check_state(current))
        current["statusCheckRollup"] = current["statusCheckRollup"][1:2]
        with self.assertRaisesRegex(M.MergeError, "without a successful check"):
            M.check_state(current)
        current["statusCheckRollup"][0]["conclusion"] = "CANCELLED"
        with self.assertRaises(M.MergeError):
            M.check_state(current)

    def test_closed_draft_wrong_base_and_conflict_release_without_merge(self):
        for extra in ({"state": "CLOSED"}, {"isDraft": True}, {"baseRefName": "other"}, {"mergeable": "CONFLICTING"}):
            client = FakeGitHub()
            client.current.update(extra)
            with self.subTest(extra=extra), self.assertRaises(M.MergeError):
                self.run_pr(client)
            self.assertEqual(client.merges, [])

    def test_merged_pr_is_idempotent(self):
        client = FakeGitHub()
        client.current["state"] = "MERGED"
        self.assertIsNone(self.run_pr(client)[0])
        self.assertEqual(client.merges, [])

    def test_main_push_immediately_before_merge_restarts_from_new_base(self):
        client = FakeGitHub()
        calls = []
        def push():
            calls.append(1)
            if len(calls) == 2:
                client.main = "base2"
        client.on_snapshot = push
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])


class GitHubRequests(unittest.TestCase):
    def test_updates_and_merges_pin_the_observed_head(self):
        repo = {"nameWithOwner": "owner/project", "url": "https://github.com/owner/project",
                "defaultBranchRef": {"name": "main"}}
        responses = [repo, {"message": "Updating"}, {"merged": True, "sha": "merge1"}]
        with mock.patch.object(subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="") for row in responses]) as run:
            client = M.GitHub()
            client.update(123, "head1")
            self.assertEqual(client.merge(123, "head1"), "merge1")
        self.assertIn("expected_head_sha=head1", run.call_args_list[1].args[0])
        self.assertIn("sha=head1", run.call_args_list[2].args[0])
        self.assertIn("merge_method=merge", run.call_args_list[2].args[0])

    def test_worktrees_and_harnesses_share_repository_key(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"XDG_STATE_HOME": td}):
            self.assertEqual(M.state_directory("github.com/OWNER/Repo"), M.state_directory("github.com/owner/repo"))
            self.assertNotEqual(M.state_directory("github.com/owner/repo"), M.state_directory("other.host/owner/repo"))


if __name__ == "__main__":
    unittest.main()
