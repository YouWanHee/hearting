"""Narrow typed replay and parity-helper contract checks."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.tests.parity_compare import diff_frames, diff_view_models, validate_ledger
from tools.fleet.tests.parity_states import enumerate_states
from tools.fleet.tests.reference_injection import inject_model_catalog
from tools.fleet.tests.snapshot_fixture import replay_snapshot, replay_source_bundle


FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class ReplayContract(unittest.TestCase):
    def test_source_bundle_join_rebuilds_typed_rows_and_pointer_provenance(self):
        state = {"sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "aa",
                               "cwd": "/repo", "pid": 1}],
                 "jobs": [], "resource_jobs": [{"run_id": "resource-a", "cwd": "/repo"}],
                 "route": [{"route_id": "route-a", "nodes": []}],
                 "compute_hosts": {"hosts": []}, "peer_messages": {"records": []}}
        history = {"aa": {"seat": "seat-data", "cards": [], "handovers": []},
                   "_recent_handovers": [{"ts": "2026-10-20T01:00:00Z"}]}
        logs = {"sid-a:0": {"h": "claude", "sid": "sid-a", "lines": ["log row"]}}
        raw = {"lines": ["raw row"]}
        snapshot, detail = replay_source_bundle(state, history, logs, raw, now=10)
        from tools.fleet.model import ResourceJob, Session
        self.assertIsInstance(snapshot.sessions[0], Session)
        self.assertIsInstance(snapshot.resources[0], ResourceJob)
        self.assertEqual(snapshot.compute_hosts, {"hosts": []})
        self.assertEqual(detail.seats["claude:sid-a"][-1]["sid"], "sid-a")
        self.assertEqual(detail.source_records["aa.cards"], "/history_data.json/aa/cards")
        self.assertEqual(detail.source_records["logs.sid-a:0"], "/logs_hist.json/sid-a:0/lines")
        self.assertEqual(snapshot.observations["replay_sources"]["history_handovers"], history["_recent_handovers"])

    def test_snapshot_replay_preserves_allowlisted_values_and_unknown_provenance(self):
        snapshot, detail = replay_snapshot({"malformed": 2, "observations": {"ready": True},
                                            "extra_capture": {"sha256": "abc"}}, now=123)
        self.assertEqual(snapshot.malformed, 2)
        self.assertEqual(snapshot.observations["ready"], True)
        self.assertEqual(snapshot.observations["replay_unknown"]["extra_capture"]["sha256"], "abc")
        self.assertEqual(snapshot.observations["replay_now"], 123)
        self.assertIsNone(detail.focus)

    def test_snapshot_replay_rebuilds_nested_projection_dataclasses(self):
        from tools.fleet.model import ActiveNodeProjection, ProgressProjection, Session, WorkProjection
        snapshot, _detail = replay_snapshot({"sessions": [{"harness": "claude", "pid": 7,
            "work_projection": {"source": "route", "active_nodes": [{"id": "execute"}],
                                "progress": {"done": 2, "total": 4}, "unknown": "discarded"}}]})
        session = snapshot.sessions[0]
        self.assertIsInstance(session, Session)
        self.assertIsInstance(session.work_projection, WorkProjection)
        self.assertIsInstance(session.work_projection.active_nodes[0], ActiveNodeProjection)
        self.assertIsInstance(session.work_projection.progress, ProgressProjection)
        self.assertEqual(session.work_projection.progress.done, 2)
        self.assertFalse(hasattr(session.work_projection, "unknown"))

    def test_scrubbed_vm_fixture_loads_and_states_are_stable(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        states = enumerate_states({"states": ("OVERVIEW",)}, vm)
        ids = [state["id"] for state in states]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue({"OVERVIEW", "PEERS", "ROUTES"}.issubset(ids))

    def test_differs_keep_list_order_and_ledger_summary_is_recomputed(self):
        self.assertEqual(diff_view_models({"ids": ["a", "b"]}, {"ids": ["b", "a"]})[0]["path"],
                         "/ids/0")
        ledger = {"differences": [{"id": "x", "axis": "a", "entity": "r", "field": "t",
                                    "states": ["OVERVIEW"], "widths": [140], "source": [],
                                    "reason": "observed", "classification": None}]}
        self.assertEqual(validate_ledger(ledger), [])
        self.assertEqual(ledger["summary"]["unclassified"], 1)

    def test_frame_diff_checks_cells_and_ordered_hits(self):
        left = {"cells": [["a", "b"]], "hits": [[0, 0, 1, ["view", "A"]]]}
        right = {"cells": [["a", "c"]], "hits": [[0, 0, 1, ["view", "B"]]]}
        diffs = diff_frames(left, right)
        self.assertEqual([item["kind"] for item in diffs], ["cell", "hits"])

    def test_model_catalog_injection_uses_provider_entries_and_main_only_config(self):
        renderer = SimpleNamespace()
        result = inject_model_catalog(renderer, {
            "providers": {"opencode": [{"id": "id-from-provider", "version": "visible version"}]},
            "main_only": ["configured-main-only"],
        })
        self.assertEqual(renderer.MODEL_CHOICES, {"opencode": ["id-from-provider"]})
        self.assertEqual(renderer.MODEL_SHOWN, {"id-from-provider": "visible version"})
        self.assertEqual(renderer.MAIN_ONLY, ("configured-main-only",))
        self.assertEqual(result["model_counts"], {"opencode": 1})


if __name__ == "__main__":
    unittest.main()
