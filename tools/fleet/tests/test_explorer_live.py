"""VM mapping keeps unknowns explicit and freezes nested input."""
import unittest
import json
from pathlib import Path

from tools.fleet.explorer.live import build_view_model
from tools.fleet.model import Session


class CoreFields(unittest.TestCase):
    def test_harness_fields_and_deep_immutability(self):
        sessions = [Session(harness=harness, pid=index + 1, session_tag=f"s{index}",
                            cwd="/synthetic/repo", title=None, ctx_pct=0,
                            liveness="working", elapsed_min=7)
                    for index, harness in enumerate(("claude", "codex", "opencode"))]
        vm = build_view_model({"sessions": sessions, "compute_hosts": [],
                               "project_of": lambda _cwd: "synthetic-repo"})
        self.assertEqual(tuple(row["harness"] for row in vm.sessions), ("claude", "codex", "opencode"))
        self.assertIsNone(vm.sessions[0]["title"])
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.sessions[0]["state"], "working")
        self.assertEqual(vm.sessions[0]["age"], 7)
        with self.assertRaises(TypeError):
            vm.sessions[0]["title"] = "changed"

    def test_reference_fixture_has_complete_scrubbed_core_records(self):
        path = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        vm = build_view_model(payload)
        self.assertGreater(len(vm.hosts), 0)
        self.assertGreater(len(vm.sessions), 0)
        self.assertGreater(len(vm.routes), 0)
        self.assertGreater(len(vm.nodes), 0)
        self.assertGreater(len(vm.questions), 0)
        self.assertGreater(len(vm.seats), 0)
        self.assertGreater(len(vm.generations), 0)
        self.assertGreater(len(vm.cards), 0)
        self.assertGreater(len(vm.dialogue), 0)
        self.assertGreater(len(vm.peer_nodes), 0)
        self.assertGreater(len(vm.compute), 0)
        self.assertTrue(vm.models)
        self.assertTrue(vm.system)
        encoded = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("openrig", encoded)
        self.assertNotIn("mock", encoded)

    def test_live_boundary_keeps_exact_pane_and_source_record_joins(self):
        session = {"harness": "codex", "session_id": "sid-live", "cwd": "/synthetic/repo",
                   "herdr_pane": "workspace:pane-7", "context_pct": 0}
        graph = ({"harness": "codex", "session_id": "sid-live",
                  "pane_id": "workspace:pane-7"},)
        facts = {"routes": (("rt-test", {"id": "rt-test"}),), "warnings": ()}
        vm = build_view_model({"sessions": [session], "pane_graph": graph,
                               "project_of": lambda _cwd: "synthetic-repo"},
                              accepted_detail={"source_records": facts})
        self.assertEqual(vm.sessions[0]["pane"], "workspace:pane-7")
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.pane_graph[0]["session_id"], "sid-live")
        self.assertEqual(vm.source_records["warnings"], ())


class ConfigFields(unittest.TestCase):
    def test_cached_config_and_memory_projection_preserve_missing_and_zero(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        config = {"models": {"profiles": ["balanced"], "context": {"codex": 0}}}
        snapshot = {"sessions": [], "resources": [], "memory": None}
        vm = build_view_model(snapshot, accepted_detail={"memory": [("repo", [("12:00", "saved", "note", "fact")])]},
                              cached_config=config)
        self.assertEqual(vm.models["profiles"], ("balanced",))
        self.assertEqual(vm.models["context"]["codex"], 0)
        self.assertEqual(vm.memory[0][1][0][3], "fact")
        missing = build_view_model({"sessions": [], "resources": []}, cached_config={})
        self.assertIsNone(missing.models)
        self.assertIsNone(missing.memory)
        self.assertIsInstance(ExplorerViewModel.from_mapping(missing), ExplorerViewModel)
