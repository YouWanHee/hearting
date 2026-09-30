#!/usr/bin/env python3
"""SD-163: a partial compose graph keeps inputs found in the prior cycle, and the
route artifact env names that cycle's output folder (stage-dispatch §13.62)."""
import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load("producer_test_for_sd163", "artifact_producer.test.py")
OWNER_T = _load("owner_test_for_sd163", "dispatch_owner.test.py")
R, P, TOPO = T.R, T.P, T.R.TOPO
import dispatch_stage_advance as ADVANCE  # noqa: E402

PARENT_VAR = "AGENT_ARTIFACT_PARENT_OUTPUT_DIR"
SIX = ["AGENT_ARTIFACT_ROOT", "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID",
       "AGENT_ARTIFACT_PRODUCER_ID", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR"]
FRESH = {"execute": ["task"], "test": ["source-diff"], "report": ["dev_logs/**", "test_logs/**"]}


class SourceBase(T.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.activate()
        self.count = 0

    def cycle(self, files=("plan.md", "checklist.md"), key="k163", parent=None):
        """Begin a direct cycle and leave `files` (relative to artifacts/) in it."""
        self.count += 1
        route, route_file = self.route("direct", slug=f"prior-{self.count}",
                                       campaign_key=None if parent else key, parent_cycle_id=parent)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel in files:
            self.write_output(result, rel=rel, data=b"x\n")
        return result

    def compose(self, graph="execute,test,report", **kw):
        return R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph=graph,
            slug="sd163", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]}, **kw)

    def without_finder(self, **kw):
        with mock.patch.object(sys.modules["artifact_producer"], "input_source_finder", return_value=None):
            return self.compose(**kw)

    @staticmethod
    def node(route, node_id):
        return next(n for n in route["nodes"] if n["id"] == node_id)

    def inputs(self, route):
        return {n["id"]: n["inputs"] for n in route["nodes"]}

    def assert_not_found(self, **kw):
        """The compose result is byte-identical to a compose that had no finder at all."""
        route = self.compose(**kw)
        baseline = self.without_finder(**kw)
        self.assertEqual(self.inputs(route), FRESH)
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))
        self.assertEqual(route["route_hash"], baseline["route_hash"])
        self.assertEqual(R.compose_card(route), R.compose_card(baseline))


class NoSourceRegressionTest(SourceBase):
    def test_patch_target_is_the_module_compose_imports(self):
        self.assertIs(P, sys.modules["artifact_producer"])
        self.cycle()
        source = {"cycle_id": "cyc_" + "1" * 32, "path": "campaigns/x/artifacts/plan.md"}
        find = lambda name: dict(source) if name == "plan.md" else None
        with mock.patch.object(P, "input_source_finder", return_value=find) as patched:
            route = self.compose(campaign_key="k163")
        patched.assert_called_once()
        self.assertEqual(self.node(route, "execute")["input_sources"], {"plan.md": source})

    def test_a163_1a_full_graph_is_unchanged(self):
        self.cycle()
        graph = ",".join(n["id"] for n in TOPO.resolve_recipe(
            TOPO.load_registry(), "autopilot-code", "dev")["standard_plus"]["nodes"]
            if n["id"] != "frame-alternative")
        route = self.compose(graph=graph, campaign_key="k163")
        baseline = self.without_finder(graph=graph, campaign_key="k163")
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))
        self.assertEqual(route["route_hash"], baseline["route_hash"])
        self.assertEqual(R.compose_card(route), R.compose_card(baseline))

    def test_a163_1b_preset_compile_never_asks_for_a_source(self):
        self.cycle()
        with mock.patch.object(P, "input_source_finder", wraps=P.input_source_finder) as spy:
            route = T.compile_for("standard", self.root, campaign_key="k163")
        spy.assert_not_called()
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))

    def test_a163_1c_partial_graph_without_a_source_is_unchanged(self):
        self.cycle()
        self.assert_not_found(campaign_key="brand-new-stream")

    def test_a163_1d_brief_without_input_sources_is_the_eight_line_template(self):
        route = self.compose(campaign_key="brand-new-stream")
        text, digest = ADVANCE.render_stage_brief(route, self.node(route, "execute"))
        self.assertEqual(text, (
            "capability: autopilot-code\ncapability_mode: dev\nintensity: standard\nunit: dev/backend\n"
            "inputs: task\noutputs: source-diff,dev_logs/**\n"
            "write_scope: source/**,checklist.md,dev_logs/**,evidence/**\ncompletion_gate: code-execute\n"))
        self.assertEqual(digest, "sha256:" + hashlib.sha256(
            (ADVANCE.BRIEF_TEMPLATE_ID + "\n" + text).encode("utf-8")).hexdigest())


class SealedSourceTest(SourceBase):
    def test_a163_2_parent_cycle_fills_inputs_card_brief_and_reproduces(self):
        prior = self.cycle()
        route = self.compose(parent_cycle_id=prior["cycle_id"])
        folder = Path(prior["cycle_dir"]).relative_to(self.root) / "artifacts"
        expected = {name: {"cycle_id": prior["cycle_id"], "path": (folder / name).as_posix()}
                    for name in ("plan.md", "checklist.md")}
        for node_id in ("execute", "test", "report"):
            node = self.node(route, node_id)
            self.assertLessEqual({"plan.md", "checklist.md"}, set(node["inputs"]), node_id)
            self.assertEqual(node["input_sources"], expected, node_id)
        R.verify_route(route, R.ROOT)
        card = R.compose_card(route)
        lines = [line for line in card.splitlines() if line.startswith("  입력 ")]
        self.assertEqual(lines, [f"  입력 plan.md·checklist.md ← {prior['cycle_id']} "
                                 f"{Path(prior['cycle_dir']) / 'artifacts'}"])
        execute = self.node(route, "execute")
        text, _digest = ADVANCE.render_stage_brief(route, execute)
        self.assertEqual(text.splitlines()[-1],
                         f"input_sources: plan.md={expected['plan.md']['path']},"
                         f"checklist.md={expected['checklist.md']['path']}")
        self.assertTrue(R._versioned_subgraph(TOPO.load_registry(), route["composed_recipe"]))
        self.assertEqual(R._continuation_node_projection(execute, [])["input_sources"], expected)
        baseline = self.without_finder(campaign_key="brand-new-stream")
        for field in ("model_profile", "profile_selection", "profile_demand"):
            self.assertEqual(self.node(route, "execute").get(field), self.node(baseline, "execute").get(field))

    def test_a163_3a_campaign_latest_cycle_is_the_source(self):
        self.cycle(files=("a/plan.md",))
        latest = self.cycle(files=("plan.md",))
        route = self.compose(campaign_key="k163")
        source = self.node(route, "execute")["input_sources"]["plan.md"]
        self.assertEqual(source["cycle_id"], latest["cycle_id"])
        self.assertTrue(source["path"].endswith("/artifacts/plan.md"))
        self.assertIn(Path(latest["cycle_dir"]).name, source["path"])

    def test_a163_3b_latest_cycle_without_the_file_does_not_fall_back(self):
        self.cycle(files=("plan.md",))
        self.cycle(files=("notes.md",))
        self.assert_not_found(campaign_key="k163")

    def test_unassigned_compose_has_no_source(self):
        self.cycle()
        self.assert_not_found(unassigned=True)

    def test_a163_4f_directory_input_is_sealed(self):
        prior = self.cycle(files=("dev_logs/execute.md", "test_logs/run.md"))
        route = self.compose(graph="report", parent_cycle_id=prior["cycle_id"])
        node = self.node(route, "report")
        self.assertEqual(node["inputs"], ["dev_logs/**", "test_logs/**"])
        folder = (Path(prior["cycle_dir"]).relative_to(self.root) / "artifacts").as_posix()
        self.assertEqual(node["input_sources"]["dev_logs/**"]["path"], folder + "/dev_logs")
        self.assertEqual(node["input_sources"]["test_logs/**"]["path"], folder + "/test_logs")


class LookupLimitsTest(SourceBase):
    def find(self, prior, name="plan.md"):
        return P.input_source_finder(self.root, parent_cycle_id=prior["cycle_id"])(name)

    def test_a163_4a_shortest_path_then_lexicographic(self):
        prior = self.cycle(files=("b/plan.md", "a/plan.md"))
        self.assertTrue(self.find(prior)["path"].endswith("/artifacts/a/plan.md"))
        self.write_output(prior, rel="plan.md")
        self.assertTrue(self.find(prior)["path"].endswith("/artifacts/plan.md"))
        # Character length decides, not directory depth: deeper `a/b/plan.md` (11) beats `zzzzzz/plan.md` (14).
        other = self.cycle(files=("zzzzzz/plan.md", "a/b/plan.md"))
        self.assertTrue(self.find(other)["path"].endswith("/artifacts/a/b/plan.md"))

    def test_a163_4b_symlinks_are_not_found(self):
        prior = self.cycle(files=("real.md", "elsewhere/plan.md"))
        artifacts = Path(prior["cycle_dir"]) / "artifacts"
        (artifacts / "plan.md").symlink_to(artifacts / "real.md")
        (artifacts / "linked").symlink_to(artifacts / "elsewhere")
        (artifacts / "elsewhere" / "plan.md").rename(artifacts / "elsewhere" / "other.md")
        self.assertIsNone(self.find(prior))
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])

    def test_a163_4c_artifacts_folder_outside_the_root_is_not_found(self):
        prior = self.cycle()
        artifacts = Path(prior["cycle_dir"]) / "artifacts"
        outside = Path(self._tmp.name) / "outside"
        shutil.move(str(artifacts), str(outside))
        artifacts.symlink_to(outside)
        self.assertIsNone(self.find(prior))
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])

    def test_a163_4d_caps_mean_not_found(self):
        prior = self.cycle(files=("plan.md", "one.md", "two.md", "three.md"))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_ENTRIES", 3):
            self.assertIsNone(self.find(prior))
        self.assertIsNotNone(self.find(prior))
        many = self.cycle(files=("a/plan.md", "b/plan.md"))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_CANDIDATES", 1):
            self.assertIsNone(self.find(many))
        deep = self.cycle(files=("a/b/c/plan.md",))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_DEPTH", 1):
            self.assertIsNone(self.find(deep))
        self.assertIsNotNone(self.find(deep))

    def test_a163_4e_unreadable_records_and_errors_are_not_found(self):
        prior = self.cycle()
        real_scandir = os.scandir

        def deny(path, *args, **kwargs):
            if "artifacts" in str(path):
                raise PermissionError(13, "denied")
            return real_scandir(path, *args, **kwargs)

        with mock.patch.object(P.os, "scandir", side_effect=deny):
            self.assert_not_found(parent_cycle_id=prior["cycle_id"])
        P.cycle_record_path(self.root, prior["cycle_id"]).write_text("{broken", encoding="utf-8")
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])
        self.assert_not_found(campaign_key="a-stream-with-no-record")

    def test_a163_4g_only_plain_paths_are_targets(self):
        self.assertEqual(P._input_target("plan.md"), (("plan.md",), False))
        self.assertEqual(P._input_target("reviews/smoke-attestation.json"),
                         (("reviews", "smoke-attestation.json"), False))
        self.assertEqual(P._input_target("dev_logs/**"), (("dev_logs",), True))
        for name in ("designs/<cycle>/…", "spec/<component>/x.md", "", "/plan.md", "a/../b", "./a", "a//b",
                     "**", "/**", "report/*.md", None):
            self.assertIsNone(P._input_target(name), name)


class ParentOutputEnvTest(SourceBase):
    def env(self, route_file, start=True):
        return P.prepare_route_artifact_env(route_file, start=start, jobs=self.jobs)

    def test_a163_5a_parent_cycle_names_its_output_dir(self):
        prior = self.cycle()
        _route, route_file = self.route("direct", slug="child", parent_cycle_id=prior["cycle_id"])
        env = self.env(route_file)
        self.assertEqual(env[PARENT_VAR], str((Path(prior["cycle_dir"]) / "artifacts").resolve()))
        self.assertEqual(list(env)[:6], SIX)
        record = P.route_cycle_for(self.root, P.load_route(self.root, route_file))
        self.assertEqual(env["AGENT_ARTIFACT_CYCLE_ID"], record["cycle_id"])
        self.assertEqual(env["AGENT_ARTIFACT_OUTPUT_DIR"],
                         str(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"))
        self.assertEqual(env, self.env(route_file, start=False))

    def test_a163_5b_campaign_previous_cycle_else_no_variable(self):
        first = self.cycle()
        _route, route_file = self.route("direct", slug="second", campaign_key="k163")
        fresh_file = self.route("direct", slug="first-of-fresh", campaign_key="fresh")[1]
        self.assertNotIn(PARENT_VAR, self.env(fresh_file))
        second = self.env(route_file)
        self.assertEqual(second[PARENT_VAR], str((Path(first["cycle_dir"]) / "artifacts").resolve()))
        # The campaign's first cycle has no predecessor.
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertNotIn(PARENT_VAR, P._env_for(self.root, record))
        # The degraded `_unassigned` container asserts no shared goal.
        for slug in ("loose-1", "loose-2"):
            env = self.env(self.route("direct", slug=slug)[1])
            self.assertNotIn(PARENT_VAR, env)

    def test_a163_5c_priority_parent_then_input_sources_then_campaign(self):
        c1, c2 = self.cycle(), self.cycle()
        c3 = self.cycle()
        record = P.read_cycle_record(self.root, c3["cycle_id"])
        artifacts = lambda cycle: str((Path(cycle["cycle_dir"]) / "artifacts").resolve())
        sealed = {"nodes": [{"input_sources": {"plan.md": {"cycle_id": c1["cycle_id"], "path": "x"}}}]}
        self.assertEqual(P._env_for(self.root, record)[PARENT_VAR], artifacts(c2))
        self.assertEqual(P._env_for(self.root, record, sealed)[PARENT_VAR], artifacts(c1))
        with_parent = {**record, "parent_cycle_id": c2["cycle_id"]}
        self.assertEqual(P._env_for(self.root, with_parent, sealed)[PARENT_VAR], artifacts(c2))

    def test_a163_5d_staged_owner_launch_env_has_the_variable(self):
        prior = self.cycle()
        route = self.compose(campaign_key="k163")
        from artifact_lifecycle import admit_runtime_route
        route_file = Path(admit_runtime_route(self.root, route).route_file)
        env = self.env(route_file)
        self.assertEqual(env[PARENT_VAR], str((Path(prior["cycle_dir"]) / "artifacts").resolve()))

    def test_a163_5e_closed_env_list_is_untouched(self):
        self.assertEqual(TOPO.PRODUCER_LIFECYCLE_ENV, [
            "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
            "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR"])
        TOPO.validate_registry(TOPO.load_registry())

    def test_a163_5f_owner_launch_drops_a_stale_inherited_variable(self):
        """A leftover value from an earlier route must not reach an owner with no source."""
        holder = OWNER_T.RouteDefaultsReceiptTest()
        path = holder._quick_route()
        jobs = path.parent / "jobs.log"
        jobs.touch()
        binding = SimpleNamespace(route_file=str(path), route_id="rt-x", route_hash="sha256:x",
                                  route_node="one-shot", registry_digest="sha256:r",
                                  write_scope="source-scoped", completion_gate="quick-complete")
        seen = []
        OWNER = OWNER_T.OWNER
        stale = {"AGENT_DISPATCH_JOBS": str(jobs), PARENT_VAR: "/stale/other-route/artifacts"}
        with mock.patch.object(OWNER.subprocess, "run", side_effect=lambda cmd, **kw: (
                seen.append(kw.get("env")), SimpleNamespace(returncode=0))[1]), \
             mock.patch.object(OWNER, "_usage", return_value={"claude": "ok", "codex": "ok", "opencode": "ok"}), \
             mock.patch.object(OWNER._capacity, "capacity_scores",
                               return_value={"claude": 80.0, "codex": 80.0, "opencode": 80.0}), \
             mock.patch.object(OWNER, "derive_quick_owner_binding", return_value=binding), \
             mock.patch("artifact_producer.prepare_route_artifact_env", return_value={}), \
             mock.patch.dict(os.environ, OWNER_T._isolated_env(stale), clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(OWNER.main(["--dry-run", "--route-evidence", str(path), "--prompt-text", "probe"]), 0)
        self.assertEqual(len(seen), 1)
        self.assertNotIn(PARENT_VAR, seen[0])


if __name__ == "__main__":
    unittest.main()
