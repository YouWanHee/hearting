#!/usr/bin/env python3
"""Per-repository supervision count: the one aggregation the grant limit and Fleet share.

Rows, markers and relations are injected, so no herdr, registry or ledger is read.
Linked worktrees fold into their primary through ``resolve_gitdir``.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import steward                                   # noqa: E402

EXPLICIT = {"harness": "unknown", "session_id": None, "name": "-", "kind": "explicit",
            "source": "explicit"}


def _git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True, text=True)


def _relation(sid, *, origin=None, generation=None, targets=(EXPLICIT,), harness="claude"):
    return {"generation": generation or f"gen-{sid}", "released": False,
            "origin": {"harness": harness, "sid": origin or sid},
            "holder": {"harness": harness, "sid": sid},
            "targets": [dict(target) for target in targets]}


class _RepoFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="steward-repo-cap-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.primary, self.linked, self.other = base / "alpha", base / "alpha-wt", base / "beta"
        for repo in (self.primary, self.other):
            _git("init", "-q", str(repo))
            _git("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "fixture")
        _git("-C", str(self.primary), "worktree", "add", "-q", "-b", "linked", str(self.linked))
        self.registry = base / "registry"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.registry),
                                               "FLEET_SESSION_REGISTRY_DIR": str(self.registry)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = steward._repository_key(str(self.primary))

    def row(self, sid, cwd, harness="claude"):
        return SimpleNamespace(harness=harness, session_id=sid, cwd=str(cwd))

    def census(self, rows, relations, markers=None, **kw):
        return steward.repo_supervision(repository_key=self.key, limit=kw.pop("limit", 2),
                                        markers=markers or {}, relations=relations,
                                        sessions=rows, agents=kw.pop("agents", []), **kw)

    def write_record(self, pid, sid, proc_start=None):
        directory = self.registry / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "cwd": str(self.primary),
                        "procStart": proc_start}), encoding="utf-8")


class RepoSupervisionTest(_RepoFixture, unittest.TestCase):
    def test_primary_and_linked_worktree_count_as_one_repository(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.linked)]
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        self.assertEqual(result["counted"], 2)
        self.assertEqual(result["repository"], "alpha")
        self.assertEqual({h["basis"] for h in result["holders"]}, {"role"})
        self.assertFalse(result["over_limit"])

    def test_other_repository_holder_is_not_counted(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.other)]
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        self.assertEqual([h["session_id"] for h in result["holders"]], ["s1"])

    def test_same_session_with_two_generations_counts_once(self):
        rows = [self.row("s1", self.primary)]
        result = self.census(rows, [_relation("s1", generation="g1"), _relation("s1", generation="g2")])
        self.assertEqual(result["counted"], 1)

    def test_confirmed_handover_counts_the_successor_only(self):
        rows = [self.row("old", self.primary), self.row("new", self.primary)]
        result = self.census(rows, [_relation("new", origin="old")])
        self.assertEqual([h["session_id"] for h in result["holders"]], ["new"])

    def test_released_relation_is_not_a_holder(self):
        rows = [self.row("s1", self.primary)]
        relation = dict(_relation("s1"), released=True)
        self.assertEqual(self.census(rows, [relation])["counted"], 0)

    def test_grant_basis_counts_a_holder_whose_row_is_not_visible_yet(self):
        markers = {("claude", "s1"): {"repository": self.key}}
        result = self.census([], [_relation("s1")], markers=markers)
        self.assertEqual([(h["session_id"], h["basis"]) for h in result["holders"]], [("s1", "grant")])
        elsewhere = {("claude", "s1"): {"repository": str(self.other / ".git")}}
        self.assertEqual(self.census([], [_relation("s1")], markers=elsewhere)["counted"], 0)

    def test_grant_basis_yields_to_a_row_in_another_repository(self):
        markers = {("claude", "s1"): {"repository": self.key}}
        result = self.census([self.row("s1", self.other)], [_relation("s1")], markers=markers)
        self.assertEqual(result["counted"], 0)

    def test_ended_holder_frees_the_slot_and_unknown_still_counts(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.primary)]
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        by_sid = {h["session_id"]: h for h in result["holders"]}
        self.assertEqual((by_sid["s1"]["liveness"], by_sid["s1"]["counted"]), ("ended", False))
        self.assertEqual((by_sid["s2"]["liveness"], by_sid["s2"]["counted"]), ("unknown", True))
        self.assertEqual(result["counted"], 1)

    def test_over_limit_is_shown_and_nothing_is_dropped(self):
        rows = [self.row(f"s{i}", self.primary) for i in range(3)]
        result = self.census(rows, [_relation(f"s{i}") for i in range(3)])
        self.assertEqual((result["counted"], result["limit"], result["over_limit"]), (3, 2, True))
        self.assertEqual(len(result["holders"]), 3)

    def test_supervision_by_repository_separates_repositories(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.linked),
                self.row("s3", self.other)]
        board = steward.supervision_by_repository(
            rows, limit=2, markers={}, relations=[_relation(s) for s in ("s1", "s2", "s3")],
            agents=[])
        self.assertEqual({key: value["counted"] for key, value in board.items()},
                         {self.key: 2, steward._repository_key(str(self.other)): 1})
        self.assertEqual(board[self.key], self.census(rows, [_relation(s) for s in ("s1", "s2", "s3")]))

    def test_enrich_keeps_one_board_census_for_the_models_view(self):
        rows = [self.row("s1", self.primary), self.row("s3", self.other)]
        relations = [_relation("s1"), _relation("s3")]
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=relations) as read, \
                mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                mock.patch.object(steward, "_steward_limit", return_value=2), \
                mock.patch("fleet.collectors.herdr.list_agents", return_value=[]):
            steward.enrich(rows, markers={})
            census = steward.last_supervision()
        read.assert_called_once()
        self.assertEqual({key: value["counted"] for key, value in census.items()},
                         {self.key: 1, steward._repository_key(str(self.other)): 1})
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=[]):
            steward.enrich(rows, markers={})
            self.assertEqual(steward.last_supervision(), {})


class SessionLivenessTest(_RepoFixture, unittest.TestCase):
    def test_herdr_row_means_live(self):
        agents = [{"agent": "claude", "agent_session": {"value": "s1"}}]
        self.assertEqual(steward.session_liveness("claude", "s1", agents=agents), "live")

    def test_dead_pid_is_ended(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "ended")

    def test_recycled_pid_is_ended_and_matching_start_is_live(self):
        from fleet.collectors import procscan
        self.write_record(os.getpid(), "s1", proc_start="1")
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "ended")
        self.write_record(os.getpid(), "s1", proc_start=str(procscan.read_proc_start(os.getpid())))
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "live")

    def test_missing_registry_is_unknown(self):
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "unknown")
        self.assertEqual(steward.session_liveness("opencode", "s1", agents=[]), "unknown")


class RepositoryForSessionTest(_RepoFixture, unittest.TestCase):
    def test_linked_worktree_row_folds_to_the_primary_key(self):
        rows = [self.row("s1", self.linked)]
        self.assertEqual(steward.repository_for_session("claude", "s1", sessions=rows), self.key)

    def test_missing_or_ambiguous_row_is_none_and_never_the_process_cwd(self):
        self.assertIsNone(steward.repository_for_session("claude", "s1", sessions=[]))
        rows = [self.row("s1", self.primary), self.row("s1", self.other)]
        self.assertIsNone(steward.repository_for_session("claude", "s1", sessions=rows))


if __name__ == "__main__":
    unittest.main()
