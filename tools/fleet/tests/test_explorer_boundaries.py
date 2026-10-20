import copy
import dataclasses
import tempfile
import unittest

from tools.fleet.explorer.focus import SelectionReader
from tools.fleet.explorer.nav import build_nav
from tools.fleet.explorer.types import FocusKey, FocusRequest, LedgerRow, RequestLedger, RequestTab
from tools.fleet.tests.explorer_fixture import collect, materialize, tab


class SourceBoundaryTests(unittest.TestCase):
    def test_unexpanded_focus_is_passed_without_hidden_session_reads(self):
        seen = []
        reader = SelectionReader(lambda request: seen.append(request.expanded_sessions) or {})
        reader.read(FocusRequest(FocusKey("claude", "s"), 1, 1, ()))
        self.assertEqual(seen, [()])


class KillTargetsDoNotGrow(unittest.TestCase):
    """s/x acts on nav session and worker rows only; request data adds no target of any kind."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.snapshot = cls.fx.snapshot
        cls.before = copy.deepcopy((cls.snapshot.sessions, cls.snapshot.jobs))
        cls.plain = build_nav(cls.snapshot)
        b6 = tab(collect(cls.fx), "b6-report")
        cls.result = collect(cls.fx, request_id=b6.identity, stage_id="test")
        cls.rich = build_nav(cls.snapshot, cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @staticmethod
    def targets(rows):
        return [(row.kind, row.identity) for row in rows if row.target_ref is not None]

    def test_targets_are_exactly_the_session_and_worker_rows(self):
        self.assertEqual({kind for kind, _identity in self.targets(self.rich)}, {"session", "worker"})

    def test_request_data_adds_no_target_over_the_snapshot_alone(self):
        self.assertEqual(set(self.targets(self.rich)), set(self.targets(self.plain)))
        self.assertEqual(len(self.targets(self.rich)), len(set(self.targets(self.rich))))

    def test_targets_never_grow_over_the_old_group_path(self):
        """Old path: every session and every dispatched job that has a parent session."""
        def owner(job):
            return next((s for s in self.snapshot.sessions
                         if job["parent_sid"] in [s.get("session_id")] + list(s.get("session_aliases") or ())), None)

        old = {("session", f"session:{s['harness']}:{s['session_id']}") for s in self.snapshot.sessions
               if s.get("session_id")}
        old |= {("worker", f"worker:{owner(job)['harness']}:{owner(job)['session_id']}:"
                           f"{job.get('attempt_id') or job['key']}")
                for job in self.snapshot.jobs if owner(job)}
        new = set(self.targets(self.rich))
        self.assertTrue(new <= old, sorted(new - old))
        self.assertEqual(len([t for t in new if t[0] == "session"]), 13)       # main sessions only
        self.assertEqual(len([t for t in new if t[0] == "worker"]), 8)         # live jobs only
        self.assertLess(len(new), len(old))

    def test_every_target_survives_folding_and_searching(self):
        from tools.fleet.explorer.nav import visible_rows
        from tools.fleet.explorer.state import ExplorerState
        view = ExplorerState(nav_fold=("project:hearting",), search="no-such-name")
        rows = build_nav(self.snapshot, self.result, view)
        self.assertEqual(set(self.targets(rows)), set(self.targets(self.rich)))
        self.assertEqual(visible_rows(rows, view), [])

    def test_tabs_stages_ledger_overview_and_duties_are_informational(self):
        informational = [row for row in self.rich if row.kind in ("request", "stage", "relation", "system", "section")]
        self.assertTrue([row for row in informational if row.kind == "request"])
        self.assertTrue([row for row in informational if row.kind == "stage"])
        self.assertTrue(all(row.target_ref is None for row in informational))

    def test_focus_values_carry_no_process_identity_to_act_on(self):
        for cls in (RequestTab, LedgerRow, RequestLedger):
            names = {field.name for field in dataclasses.fields(cls)}
            self.assertFalse(names & {"pid", "proc_start", "target_ref", "session_id"}, cls.__name__)
        self.assertTrue(all("pid" not in duty for duty in self.result.obligations))

    def test_reading_never_modifies_the_snapshot(self):
        self.assertEqual((self.snapshot.sessions, self.snapshot.jobs), self.before)


if __name__ == "__main__":
    unittest.main()
