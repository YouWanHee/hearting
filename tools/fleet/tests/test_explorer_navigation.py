import unittest
from types import SimpleNamespace

from tools.fleet.explorer.nav import build_nav


class ExplorerNavigationTests(unittest.TestCase):
    def test_four_sections_roles_previous_slot_and_target_boundary(self):
        parent = SimpleNamespace(session_id="sid-a", harness="claude", cwd="/repo",
                                 title="backend", liveness="working",
                                 previous_sid="sid-old")
        worker = SimpleNamespace(job_id="job-1", parent_sid="sid-a", title="reviewer",
                                 status="running")
        snapshot = SimpleNamespace(sessions=[parent], jobs=[worker], resources=[
            {"host": "gpu-a", "name": "GPU host"}], relations=[{"id": "edge", "label": "peer"}])
        focus = SimpleNamespace(tabs=[{"identity": "req-1", "label": "Request"}], ledger=[])

        rows = build_nav(snapshot, focus)
        sections = [row.identity for row in rows if row.kind == "section"]
        self.assertEqual(sections, ["section:sessions", "section:todo",
                                    "section:relations", "section:system"])
        session = next(row for row in rows if row.kind == "session")
        child = next(row for row in rows if row.kind == "worker")
        previous = next(row for row in rows if row.kind == "previous-generation")
        self.assertEqual(session.label, "backend")
        self.assertIs(session.target_ref, parent)
        self.assertEqual(child.label, "reviewer")
        self.assertIs(child.target_ref, worker)
        self.assertEqual(previous.meta[0][1], "sid-old")
        self.assertIsNone(previous.target_ref)
        self.assertTrue(all(row.target_ref is None for row in rows
                            if row.kind in {"section", "project", "request", "relation", "system"}))

    def test_missing_role_is_reported_as_unknown(self):
        rows = build_nav({"sessions": [{"sid": "s1", "harness": "codex"}]})
        self.assertEqual(next(row.label for row in rows if row.kind == "session"), "역할 미확인")


if __name__ == "__main__":
    unittest.main()
