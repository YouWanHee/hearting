import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "non_git_headless_acceptance", ROOT / "tools/non-git-headless-acceptance.py")
M = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(M)


class FixtureAcceptanceClassificationTest(unittest.TestCase):
    def setUp(self):
        self.route = {
            "route_id": "rt-fixture",
            "nodes": [
                {"id": "frame-a", "unit": "plan/frame", "worker_type": "frame"},
                {"id": "frame-b", "unit": "plan/frame", "worker_type": "frame"},
                {"id": "owner", "unit": "_kernel/owner", "worker_type": "owner"},
            ],
        }

    def rows(self):
        return [
            {"route_id": "rt-fixture", "route_node": "frame-a", "worker_type": "frame",
             "status": "done", "failure_class": "pass"},
            {"route_id": "rt-fixture", "route_node": "frame-b", "worker_type": "frame",
             "status": "done", "failure_class": "pass"},
            {"route_id": "rt-fixture", "owner_route_id": "rt-fixture", "route_node": "owner",
             "worker_type": "owner", "status": "done", "failure_class": "pass"},
            {"route_id": "rt-fixture", "route_node": "execute", "worker_type": "stage",
             "status": "done", "failure_class": "pass"},
        ]

    def test_foreign_route_rows_are_ignored(self):
        rows = self.rows()
        rows.append({"route_id": "rt-foreign", "route_node": "frame-b", "worker_type": "frame",
                     "status": "done", "failure_class": "pass"})
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertTrue(classified["verified"])
        self.assertEqual({row["route_id"] for row in classified["frame"]}, {"rt-fixture"})

    def test_missing_expected_frame_node_cannot_verify(self):
        rows = [row for row in self.rows() if row.get("route_node") != "frame-b"]
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertFalse(classified["expected_frames_present"])
        self.assertFalse(classified["verified"])

    def test_fixture_answer_provenance_is_explicit_and_nonhuman(self):
        provenance = M._frame_answer_provenance("rt-fixture")
        self.assertEqual(provenance, {
            "source": "fixture-driver",
            "fixture_gate_actor_kind": "supervisor",
            "person_or_real_supervisor": False,
            "scope": "isolated fixture route rt-fixture",
        })


if __name__ == "__main__":
    unittest.main()
