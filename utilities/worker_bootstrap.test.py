#!/usr/bin/env python3

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker_bootstrap as W
import dispatch_terminal_commit as T
import artifact_producer as AP

ROOT = Path(__file__).resolve().parents[1]

_OPA_SPEC = importlib.util.spec_from_file_location(
    "owner_preview_for_bootstrap", Path(__file__).with_name("owner_preview_approval.test.py"))
OPA = importlib.util.module_from_spec(_OPA_SPEC)
_OPA_SPEC.loader.exec_module(OPA)


class WorkerBootstrapTest(unittest.TestCase):
    def test_issued_cycle_context_supplies_missing_output_directory(self):
        env = {"AGENT_ARTIFACT_CYCLE_ID": "cyc-test", "AGENT_ARTIFACT_CYCLE_DIR": "/issued/cycle"}
        values = W.artifact_cycle_environment(env)
        self.assertEqual(values["AGENT_ARTIFACT_OUTPUT_DIR"], "/issued/cycle/artifacts")
        self.assertIn("/issued/cycle/artifacts", W.artifact_context_prompt(env))
        self.assertNotIn("AGENT_ARTIFACT_OUTPUT_DIR", env)
        self.assertEqual(W.artifact_context_prompt({}), "")

    def test_deterministic_fallback_types(self):
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=1), "owner")
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="test"),
            "stage",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="plan-review"),
            "review",
        )
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=2), "support")

    def test_worker_role_is_only_a_legacy_fallback(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="support",
                dispatch_depth=2,
                worker_role="external-adversary",
                route_node="review",
            ),
            "support",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, worker_role="code-test"),
            "stage",
        )

    def test_explicit_and_profile_precedence(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="review", dispatch_depth=1, profile_type="stage"
            ),
            "review",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=1, profile_type="support"),
            "support",
        )

    def test_explicit_frame_beats_depth_one_owner_fallback(self):
        # frame-universal (2026-09-10): a depth-1 frame node's explicit
        # worker_type must win over the dispatch_depth==1 "owner" fallback --
        # the same precedence already pinned for review at depth 1 above, now
        # pinned for frame too, so a depth-1 frame node is never silently
        # resolved away from "frame" (here to "owner"; elsewhere, if it ever
        # reached the unrelated topology-kind fallback, to "support").
        self.assertEqual(
            W.resolve_worker_type(explicit="frame", dispatch_depth=1),
            "frame",
        )
        self.assertEqual(
            W.resolve_worker_type(
                explicit="frame", dispatch_depth=1, worker_role="map-worker",
                route_node="frame",
            ),
            "frame",
        )

    def test_render_has_one_kernel_one_type_and_exact_handoff(self):
        rendered = W.render_worker_bootstrap(ROOT, "stage")
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)
        self.assertIn(W.handoff_template(), rendered)
        self.assertIn("has no Markdown fence", rendered)
        self.assertNotIn("```text\nartifact:", rendered)
        self.assertNotIn("# Worker Type: Owner", rendered)
        self.assertNotIn("# Worker Type: Review", rendered)

    def test_ordinary_worker_does_not_receive_chain_bookkeeping(self):
        for worker_type in W.WORKER_TYPES:
            text=W.render_worker_bootstrap(ROOT,worker_type)
            self.assertNotIn("after at most three",text)
            self.assertNotIn("Native helper support",text)
            self.assertNotIn("dispatch_subsession_handoff.py",text)
        self.assertIn("No per-tool heartbeat",W.runtime_progress_prompt())

    def test_render_appends_unit_persona_body(self):
        bare = W.render_worker_bootstrap(ROOT, "review")
        rendered = W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review")
        self.assertTrue(rendered.startswith(bare.rstrip("\n")))
        self.assertIn("# Unit: qa/code-review", rendered)
        self.assertNotIn("worker_type: review\n", rendered)  # frontmatter stripped
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)

    def test_reserved_and_absent_units_render_unchanged(self):
        bare = W.render_worker_bootstrap(ROOT, "owner")
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit="_kernel/owner"), bare)
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit=None), bare)
        self.assertIsNone(W.unit_persona_path(ROOT, "_kernel/resource"))
        self.assertIsNone(W.unit_persona_body(ROOT, None))

    def test_unknown_or_malformed_unit_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "unknown unit"):
            W.unit_persona_path(ROOT, "qa/does-not-exist")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.unit_persona_path(ROOT, "../escape/path")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.render_worker_bootstrap(ROOT, "stage", unit="Bad/Unit")

    def test_unit_persona_path_points_into_catalog(self):
        path = W.unit_persona_path(ROOT, "editorial/report")
        self.assertEqual(path, ROOT / "roles" / "units" / "editorial" / "report.md")
        body = W.unit_persona_body(ROOT, "editorial/report")
        self.assertTrue(body.startswith("# Unit: editorial/report"))

    def test_profile_type_scalar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles").mkdir()
            (root / "profiles" / "x.yaml").write_text(
                "name: x\nworker_type: review\n", encoding="utf-8"
            )
            self.assertEqual(W.profile_worker_type(root, "x"), "review")

    def test_assigned_stage_contract(self):
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="stage",
                route_node="test",
                completion_gate="code-test",
                root=ROOT,
            ),
            "code-test",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="owner",
                route_node=None,
                root=ROOT,
            ),
            "autopilot-code",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-design",
                worker_type="support",
                route_node="refs",
                completion_gate="design-refs",
                root=ROOT,
            ),
            "design-refs",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-spec",
                worker_type="support",
                route_node="research",
                completion_gate="spec-research",
                root=ROOT,
            ),
            "autopilot-spec",
        )

    def test_topology_kind_maps_only_to_bootstrap_type(self):
        self.assertEqual(W.worker_type_for_kind("capability-owner"), "owner")
        self.assertEqual(W.worker_type_for_kind("pipeline-stage"), "stage")
        self.assertEqual(W.worker_type_for_kind("review-worker"), "review")
        self.assertEqual(W.worker_type_for_kind("map-worker"), "support")
        with self.assertRaises(ValueError):
            W.worker_type_for_kind("resource-runner")

    def test_frame_contract_is_its_unit_even_with_legacy_owner_default(self):
        self.assertEqual(W.assigned_contract(capability="autopilot-code",worker_type="frame",
            route_node="frame",explicit="autopilot-code",unit="plan/frame",root=ROOT),"plan/frame")


class NodeScopeTest(unittest.TestCase):
    """Item 7: a stage worker's prompt must carry the route node's resolved
    (absolute) scope -- env binding first, the owner's producer binding
    second, and an explicit unbound statement when neither is available."""

    def _route(self, tmp: Path) -> Path:
        route_file = tmp / "route.json"
        route_file.write_text(json.dumps({
            "route_id": "rt-scope-test",
            "artifact_root": str(tmp / "artifact_root"),
            "nodes": [{
                "id": "research",
                "outputs": ["shards/research/notes.md"],
                "write_scope": ["spec/<component>/_internal/research/**"],
            }],
        }), encoding="utf-8")
        return route_file

    def test_stage_worker_prompt_carries_resolved_node_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            cycle_dir = root / "artifact_root" / "campaigns" / "camp1" / "cyc1"
            env = {"AGENT_ARTIFACT_CYCLE_ID": "cyc1", "AGENT_ARTIFACT_CYCLE_DIR": str(cycle_dir)}
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="research")
            prompt = W.assignment_prompt(args, "do the research", env)
            self.assertIn(
                str(cycle_dir / "artifacts" / "spec" / "<component>" / "_internal" / "research" / "**"),
                prompt,
            )
            self.assertIn(str(cycle_dir / "artifacts" / "shards" / "research" / "notes.md"), prompt)

    def test_scope_resolves_from_producer_binding_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            cycle_dir = root / "artifact_root" / "campaigns" / "camp1" / "cyc1"
            args = SimpleNamespace(
                worker_type="stage", route_file=str(route_file), route_node="research",
                parent_attempt_id="att-owner",
            )
            binding = T.ProducerBindingResult(
                "loaded", root / "binding.json",
                {"campaign_id": "camp1", "cycle_id": "cyc1"}, "digest", True,
            )
            with mock.patch.object(T, "load_producer_binding", return_value=binding), \
                 mock.patch.object(AP, "cycle_dir", return_value=cycle_dir):
                prompt = W.assignment_prompt(args, "do the research", {})
            self.assertIn(
                str(cycle_dir / "artifacts" / "spec" / "<component>" / "_internal" / "research" / "**"),
                prompt,
            )

    def test_scope_unbound_is_stated_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="research")
            prompt = W.assignment_prompt(args, "do the research", {})
            self.assertIn("no open cycle is bound", prompt)
            self.assertNotIn(str(root / "artifact_root"), prompt)

    def test_source_and_test_scope_stay_in_worktree_while_reports_use_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "linked-worktree"
            (worktree / "tests").mkdir(parents=True)
            (worktree / "module.py").write_text("before\n", encoding="utf-8")
            route_file = root / "route.json"
            route_file.write_text(json.dumps({
                "route_id": "rt-source-scope", "cwd": str(worktree),
                "artifact_root": str(root / "artifacts"),
                "nodes": [{"id": "execute", "outputs": ["dev_logs/execute.md"],
                           "write_scope": ["source/**", "source-alternative/**", "tests/**",
                                           str(root / "explicit-artifact.md")]}],
            }), encoding="utf-8")
            cycle_output = root / "canonical" / "artifacts"
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="execute")
            prompt = W.assignment_prompt(args, "edit module", {
                "AGENT_ARTIFACT_OUTPUT_DIR": str(cycle_output),
            })
            self.assertIn(str(worktree), prompt)
            self.assertIn(str(worktree / "tests"), prompt)
            self.assertIn(str(cycle_output / "dev_logs" / "execute.md"), prompt)
            self.assertIn(str(root / "explicit-artifact.md"), prompt)
            self.assertNotIn(str(cycle_output / "source"), prompt)
            (worktree / "module.py").write_text("after\n", encoding="utf-8")
            (cycle_output / "dev_logs").mkdir(parents=True)
            (cycle_output / "dev_logs" / "execute.md").write_text("handoff\n", encoding="utf-8")
            self.assertEqual((worktree / "module.py").read_text(), "after\n")
            self.assertTrue((cycle_output / "dev_logs" / "execute.md").is_file())


class OwnerGatePromptTest(unittest.TestCase):
    """The owner's assignment names the approval gate on a node it executes itself.

    Text only: the gate is still enforced by the runtime. The one shared renderer serves all
    three adapters, so one rendering test plus a check that each adapter calls it covers them.
    """

    def _route(self, tmp: Path, *, bound=True, owner_node=True) -> Path:
        node = {"id": "transaction", "kind": "capability-owner", "unit": "_kernel/owner",
                "dispatch_depth": 1, "terminal": True} if owner_node else {
                "id": "transaction", "kind": "review-worker", "unit": "editorial/review", "dispatch_depth": 2}
        route_file = tmp / "rt-gate.json"
        route_file.write_text(json.dumps({
            "route_id": "rt-gate-prompt", "artifact_root": str(tmp / "artifact_root"),
            "nodes": [{"id": "review", "dispatch_depth": 2, "unit": "editorial/review",
                       "continuation": {"kind": "human-gate", "gate": "preview-disposition"}}, node],
            "human_gate_bindings": ([{"gate": "preview-disposition", "node": "transaction", "position": "entry"}]
                                    if bound else []),
        }), encoding="utf-8")
        return route_file

    def _owner(self, route_file, *, via_binding=False):
        binding = SimpleNamespace(route_file=str(route_file), route_id="rt-gate-prompt")
        return SimpleNamespace(worker_type="owner", route_file=None if via_binding else str(route_file),
                               route_node=None, owner_route_binding=binding if via_binding else None,
                               jobs="/tmp/jobs.log")

    def test_the_owner_is_told_to_raise_the_gate_before_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_file = self._route(Path(tmp))
            for via_binding in (False, True):
                with self.subTest(via_binding=via_binding):
                    prompt = W.assignment_prompt(self._owner(route_file, via_binding=via_binding), "Refine it", {})
                    self.assertIn("preview-disposition", prompt)
                    self.assertIn("transaction", prompt)
                    self.assertIn("workflow-supervisor.py gate --route", prompt)
                    self.assertIn("--block", prompt)
                    self.assertIn(str(route_file), prompt)
                    self.assertIn("do not apply", prompt)
                    self.assertIn("BLOCKED", prompt)

    def test_a_route_without_the_binding_or_for_a_child_node_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = W.assignment_prompt(self._owner(self._route(Path(tmp), bound=False)), "Refine it", {})
            self.assertNotIn("preview-disposition", plain)
            self.assertNotIn("--block", plain)
            child_node = W.assignment_prompt(self._owner(self._route(Path(tmp), owner_node=False)), "Refine it", {})
            self.assertNotIn("--block", child_node)
            stage = SimpleNamespace(worker_type="stage", route_file=str(self._route(Path(tmp))), route_node="review")
            self.assertNotIn("--block", W.assignment_prompt(stage, "Review it", {}))

    def test_all_three_adapters_render_the_assignment_through_the_shared_function(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8")
                self.assertIn("assignment_prompt(args, task, os.environ)", source)


class OwnerInlineMarkerPromptTest(OPA.OwnerRefineBase):
    """The owner is told to finish its stage first and how to publish a stage it ran itself (D2, round 1).

    The OpenCode r3 owner ran `review` inline because nothing told it that the inline run still
    publishes a marker, and then could not settle. Text only: settlement still refuses an unmarked stage.
    """

    def prompt(self, **kw):
        args = SimpleNamespace(worker_type="owner", route_file=str(self.path), jobs=str(self.jobs),
                               attempt_id=self.owner, **kw)
        return W.assignment_prompt(args, "Refine the README", {})

    def test_the_refine_owner_is_told_review_first_and_the_named_command_publishes_the_marker(self):
        self.build("opencode", review=False, close=False, parent="poll-fallback")
        text = self.prompt()
        self.assertIn("first finish stage review", text)
        self.assertLess(text.index("first finish stage review"), text.index("--gate preview-disposition --block"))
        command = re.search(r"`([^`]*capability-route\.py complete[^`]*)`", text).group(1)
        for token in ("--execution-surface inline", "--registered-worker 0", "--fallback-hop inline",
                      "--dispatch-depth 2", f"--route {self.path}"):
            self.assertIn(token, command)
        self.assertNotIn("--jobs", command)
        # the rendered command, run as written, publishes the marker settlement asks for
        verdict = self.write_output(self.cycle, rel="reviews/refine-verdict.md", data=b"verdict: PASS\n")
        argv = [a.replace("<node>", "review") for a in shlex.split(
            command.replace("<stage terminal artifact>", str(verdict)))]
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_ATTEMPT_ID": self.owner}
        done = subprocess.run([sys.executable, *argv[1:]], text=True, capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env():
            self.assertEqual(OPA.R.owner_terminal_prerequisites(self.route, node, self.jobs), {})

    def test_the_text_is_one_rendering_for_every_harness(self):
        self.build("claude", review=False, close=False)
        texts = [self.prompt(adapter=harness) for harness in ("claude", "codex", "opencode")]
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(texts[1], texts[2])
        self.assertIn("first finish stage review", texts[0])

    def test_no_text_without_a_route_or_for_a_stage_worker(self):
        self.assertEqual(W.assignment_prompt(SimpleNamespace(worker_type="owner"), "T", {}), "Assignment:\nT\n\n")
        self.assertEqual(W.owner_inline_marker_prompt(SimpleNamespace(worker_type="stage", route_file="x")), "")

    def test_a_code_owner_gets_the_marker_line_and_no_gate_line(self):
        os.environ["AGENT_HOME"] = str(OPA.R.ROOT)
        self.activate()
        route = OPA.R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph="plan,execute,test",
            slug="code-owner", cwd=OPA.R.ROOT, artifact_root=self.root, intensity="standard",
            dispatch_evidence={"tuples": [OPA.nested("claude", "codex")]}, unassigned=True,
            work_request={"text": "Fix it", "owner_harness": "claude"})
        path = Path(OPA.L.admit_runtime_route(self.root, route).route_file)
        text = W.assignment_prompt(
            SimpleNamespace(worker_type="owner", route_file=str(path), jobs=str(self.jobs), attempt_id="att-code"),
            "Fix it", {})
        self.assertIn("A declared stage (plan, execute, test)", text)
        self.assertNotIn("Human gate", text)


if __name__ == "__main__":
    unittest.main()
