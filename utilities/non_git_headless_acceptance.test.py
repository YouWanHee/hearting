import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "non_git_headless_acceptance", ROOT / "tools/non-git-headless-acceptance.py")
M = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(M)


class FixtureAcceptanceClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.tmp.name) / "artifacts"
        (self.artifact_root / "shards/frame").mkdir(parents=True)
        self.route_id = "rt-" + "a" * 16
        self.leg_id = "rt-" + "b" * 16
        self.digest = "sha256:" + "c" * 64
        (self.artifact_root / "shards/frame/route-decision.json").write_text(
            json.dumps({"digest": self.digest}), encoding="utf-8")
        leg_file = self.artifact_root / ".runtime/routes" / f"{self.leg_id}.json"
        leg_file.parent.mkdir(parents=True)
        leg_file.write_text(json.dumps({
            "route_id": self.leg_id, "route_hash": "sha256:leg",
            "route_plan": {"digest": self.digest},
        }), encoding="utf-8")
        cursor = self.artifact_root / ".runtime/framed-decision" / f"{self.route_id}.plan-cursor.jsonl"
        cursor.parent.mkdir(parents=True)
        cursor.write_text(json.dumps({
            "schema": 1, "digest": self.digest, "index": 0, "route_id": self.leg_id,
            "route_hash": "sha256:leg", "route_file": f".runtime/routes/{self.leg_id}.json",
        }) + "\n", encoding="utf-8")
        self.route = {
            "route_id": self.route_id, "artifact_root": str(self.artifact_root),
            "nodes": [
                {"id": "frame-a", "unit": "plan/frame", "worker_type": "frame"},
                {"id": "route-decision", "kind": "runtime-terminal"},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [
            {"route_id": self.route_id, "route_node": "frame-a", "worker_type": "frame",
             "status": "done", "failure_class": "pass"},
            {"route_id": self.leg_id, "owner_route_id": self.leg_id, "route_node": "owner",
             "worker_type": "owner", "status": "done", "failure_class": "pass"},
            {"route_id": self.leg_id, "owner_route_id": self.leg_id,
             "route_node": "execute", "worker_type": "stage",
             "status": "done", "failure_class": "pass"},
        ]

    def test_foreign_route_rows_are_ignored(self):
        rows = self.rows()
        rows.append({"route_id": "rt-foreign", "route_node": "frame-b", "worker_type": "frame",
                     "status": "done", "failure_class": "pass"})
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertTrue(classified["verified"])
        self.assertEqual({row["route_id"] for row in classified["frame"]}, {self.route_id})

    def test_missing_expected_frame_node_cannot_verify(self):
        rows = [row for row in self.rows() if row.get("route_node") != "frame-a"]
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertFalse(classified["expected_frames_present"])
        self.assertFalse(classified["verified"])

    def test_leg_owner_and_stage_are_bound_to_the_started_route_plan(self):
        rows = self.rows()
        rows.extend([
            {"route_id": "rt-" + "d" * 16, "owner_route_id": self.route_id,
             "route_node": "owner", "worker_type": "owner", "status": "done",
             "failure_class": "pass"},
            {"route_id": "rt-" + "e" * 16, "owner_route_id": "rt-unrelated",
             "route_node": "execute", "worker_type": "stage", "status": "done",
             "failure_class": "pass"},
        ])
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertTrue(classified["verified"])
        self.assertEqual(classified["started_leg_route_ids"], [self.leg_id])
        self.assertEqual(len(classified["owners"]), 1)
        self.assertEqual(len(classified["stages"]), 1)

    def test_fixture_answer_provenance_is_explicit_and_nonhuman(self):
        provenance = M._frame_answer_provenance(self.route_id)
        self.assertEqual(provenance, {
            "source": "fixture-driver",
            "fixture_gate_actor_kind": "supervisor",
            "person_or_real_supervisor": False,
            "scope": f"isolated fixture route {self.route_id}",
        })

    def test_fixture_governor_uses_canonical_artifact_runtime_path(self):
        output = Path(self.tmp.name) / "acceptance-output"
        world, env = M.prepare_fixture(output)
        self.assertEqual(Path(env["AGENT_MODEL_GOVERNOR_ROOT"]),
                         Path(env["AGENT_ARTIFACT_ROOT"]) / ".runtime/model-worker-governor")
        self.assertTrue(Path(env["AGENT_MODEL_GOVERNOR_ROOT"]).is_dir())

    def test_d3_uses_supported_staged_standard_compose_for_each_mutating_capability(self):
        prompt = Path(self.tmp.name) / "task.md"
        env = {"AGENT_ARTIFACT_ROOT": str(self.artifact_root),
               "AGENT_DISPATCH_JOBS": str(Path(self.tmp.name) / "jobs.log")}
        evidence = Path(self.tmp.name) / "evidence.json"
        cases = (("autopilot-code", "dev"), ("autopilot-apply", ""), ("autopilot-refine", ""))
        for capability, mode in cases:
            with self.subTest(capability=capability):
                command = M._d3_compose_command(capability, mode, prompt, Path(self.tmp.name),
                                                env, evidence, "claude")
                self.assertEqual(command[command.index("--shape") + 1], "staged")
                self.assertEqual(command[command.index("--intensity") + 1], "standard")
                self.assertEqual(command[command.index("--capability") + 1], capability)
                self.assertEqual("--capability-mode" in command, bool(mode))
                if mode:
                    self.assertEqual(command[command.index("--capability-mode") + 1], mode)

    def test_fixture_root_accepts_non_git_parent_and_rejects_checkout(self):
        base = Path(self.tmp.name)
        self.assertEqual(M._resolve_fixture_base(str(base)), base.resolve())
        with self.assertRaisesRegex(ValueError, "outside any Git checkout"):
            M._resolve_fixture_base(str(ROOT))


if __name__ == "__main__":
    unittest.main()
