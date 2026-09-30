#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker_bootstrap as W
import dispatch_terminal_commit as T
import artifact_producer as AP

ROOT = Path(__file__).resolve().parents[1]


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


class ReportContractTest(unittest.TestCase):
    def _contract(self, capability, gate, node, root=ROOT):
        return W.assigned_contract(capability=capability, worker_type="stage", route_node=node,
                                   completion_gate=gate, root=root)

    def test_report_gates_resolve_to_their_own_contract(self):
        self.assertEqual(self._contract("autopilot-lab", "lab-report", "report"), "autopilot-lab")
        self.assertEqual(self._contract("autopilot-research", "research-report", "report"), "autopilot-research")
        self.assertEqual(self._contract("audit", "audit-report", "report"), "audit")
        self.assertEqual(self._contract("autopilot-code", "code-report", "report"), "code-report")
        self.assertEqual(self._contract("autopilot-design", "design-handoff", "handoff"), "design-handoff")
        self.assertEqual(self._contract("autopilot-draft", "draft-finalize", "finalize"), "autopilot-draft")

    def test_every_recipe_node_matches_gate_contract_table(self):
        data = json.loads((ROOT / "capabilities" / "topologies.json").read_text(encoding="utf-8"))
        table = data["completion_gate_contracts"]
        worker_types = {"pipeline-stage": "stage", "review-worker": "review", "map-worker": "support"}
        checked = 0
        for recipe in data["recipes"]:
            for node in recipe["standard_plus"]["nodes"]:
                gate = node.get("completion_gate")
                if node.get("kind") not in worker_types or gate not in table:
                    continue
                entry = table[gate]
                expected = Path(entry["contract"]).stem if entry["kind"] == "capability-doc" else recipe["capability"]
                got = W.assigned_contract(capability=recipe["capability"], worker_type=worker_types[node["kind"]],
                                          route_node=node["id"], completion_gate=gate, root=ROOT)
                self.assertEqual(got, expected, f"{recipe['capability']}/{node['id']}/{gate}")
                checked += 1
        self.assertGreater(checked, 30)

    def test_node_id_fallback_is_autopilot_code_only(self):
        for node in ("report", "plan", "execute", "test"):
            self.assertEqual(W.assigned_contract(capability="autopilot-lab", worker_type="stage",
                                                 route_node=node, root=ROOT), "autopilot-lab")
        self.assertEqual(W.assigned_contract(capability="autopilot-code", worker_type="stage",
                                             route_node="report", root=ROOT), "code-report")

    def test_unreadable_gate_table_falls_back_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._contract("autopilot-lab", "lab-report", "report", root=Path(tmp)), "autopilot-lab")

    def test_report_reference_line_names_existing_lab_docs(self):
        lab = SimpleNamespace(worker_type="stage", completion_gate="lab-report", assigned_contract="autopilot-lab")
        for harness in ("claude", "codex", "opencode"):
            prompt = W.contract_read_prompt(lab, harness)
            self.assertIn(str(ROOT / "skills/autopilot-lab/references/eval-procedure.md"), prompt)
            self.assertIn(str(ROOT / "skills/autopilot-lab/references/data-contract.md"), prompt)
        code = SimpleNamespace(worker_type="stage", completion_gate="code-report", assigned_contract="code-report")
        self.assertNotIn("report format is specified in", W.contract_read_prompt(code, "claude"))
        review = SimpleNamespace(worker_type="review", completion_gate="lab-report", assigned_contract="autopilot-lab")
        self.assertNotIn("report format is specified in", W.contract_read_prompt(review, "claude"))


def _write(path: Path, size: int = 2048, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text * size)[:size], encoding="utf-8")
    return path


def _cycle(root: Path, campaign: str, cycle: str, *, sealed: bool = True, files=()) -> Path:
    cycle_dir = root / "campaigns" / campaign / cycle
    (cycle_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    if sealed:
        (cycle_dir / "manifest.json").write_text("{}", encoding="utf-8")
    for rel, size in files:
        _write(cycle_dir / "artifacts" / rel, size)
    return cycle_dir


class DeliverableExemplarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / ".agent_reports"
        self.root.mkdir()

    def _env(self, cycle: Path) -> dict:
        return {"AGENT_ARTIFACT_CYCLE_DIR": str(cycle)}

    def _current(self, campaign="camp-now", cycle="2026-09-30_now"):
        return _cycle(self.root, campaign, cycle, sealed=False, files=[("report/REPORT.md", 4096)])

    def test_same_campaign_sealed_report_is_preferred(self):
        current = self._current()
        same = _cycle(self.root, "camp-now", "2026-01-01_old", files=[("report/REPORT.md", 2048)])
        _cycle(self.root, "camp-other", "2026-09-01_newer", files=[("report/REPORT.md", 2048)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [same / "artifacts/report/REPORT.md"])

    def test_falls_back_to_project_when_campaign_has_none(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        newer = _cycle(self.root, "camp-b", "2026-05-01_b", files=[("report/REPORT.md", 2048),
                                                                    ("report/index.html", 3000)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [newer / "artifacts/report/REPORT.md", newer / "artifacts/report/index.html"])

    def test_current_and_unsealed_cycles_are_excluded(self):
        current = self._current()
        _cycle(self.root, "camp-now", "2026-09-29_open", sealed=False, files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current)), [])
        (current / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current)), [])

    def test_legacy_claude_reports_sibling_is_scanned(self):
        current = self._current()
        legacy = self.tmp / ".claude_reports"
        old = _cycle(legacy, "camp-old", "2025-01-01_old", files=[("experiments/e1/REPORT.md", 2048)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [old / "artifacts/experiments/e1/REPORT.md"])

    def test_stub_and_internal_files_are_ignored(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-02-01_stub", files=[("report/REPORT.md", 499)])
        _cycle(self.root, "camp-a", "2026-01-01_int", files=[("experiments/_internal/REPORT.md", 4096),
                                                             ("experiments/e1/REPORT.md", 5000)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual([p.name for p in found], ["REPORT.md"])
        self.assertNotIn("_internal", str(found[0]))
        self.assertIn("2026-01-01_int", str(found[0]))

    def test_unknown_gate_has_no_exemplar(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("no-such-gate", self.root, self._env(current)), [])
        self.assertEqual(W._deliverable_exemplars("lab-report", None, {}), [])

    def test_scan_respects_deadline(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current), deadline_s=0), [])

    def test_scan_stops_after_max_cycles(self):
        current = self._current()
        for n in range(5):
            _cycle(self.root, "camp-a", f"2026-02-0{n}_x", sealed=False)
        _cycle(self.root, "camp-a", "2026-01-01_hit", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current), max_cycles=3), [])
        self.assertEqual(len(W._deliverable_exemplars("lab-report", self.root, self._env(current))), 1)


def _fake_unit_root(tmp: Path, frontmatter_extra: str, unit: str = "editorial/report") -> Path:
    path = tmp / "roles" / "units" / f"{unit}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nunit: {unit}\nrole: deep editor\naliases: {{}}\n{frontmatter_extra}---\n\n# Unit: {unit}\n",
                    encoding="utf-8")
    return tmp


def _fake_mem(tmp: Path, body: str) -> None:
    path = tmp / "tools" / "memory" / "mem.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class UnitBootstrapDeclarationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_declaration_is_parsed_from_frontmatter(self):
        want = {"memory": ("report-format",), "exemplar": "deliverable", "gates": ("x-report",)}
        for block in (
            "bootstrap:\n  memory: [report-format]\n  exemplar: deliverable\n  gates: [x-report]\n",
            "bootstrap:   # note\n  memory:   [ report-format ]  # topics\n\texemplar:\tdeliverable\n  gates: [x-report]\n",
        ):
            root = _fake_unit_root(self.tmp / "a", block)
            self.assertEqual(W.unit_bootstrap_declaration(root, "editorial/report"), want)

    def test_undeclared_reserved_missing_and_broken_are_empty(self):
        root = _fake_unit_root(self.tmp, "")
        self.assertEqual(W.unit_bootstrap_declaration(root, "editorial/report"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "_kernel/owner"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, None), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "qa/none"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "../bad"), {})
        broken = _fake_unit_root(self.tmp / "b", "bootstrap:\n  memory: [\n  ???\n")
        self.assertIsInstance(W.unit_bootstrap_declaration(broken, "editorial/report"), dict)

    def _args(self, **kw):
        base = dict(worker_type="stage", unit="editorial/report", completion_gate="lab-report",
                    artifact_root=None, worktree=str(self.tmp))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_undeclared_unit_gets_nothing_and_spawns_nothing(self):
        root = _fake_unit_root(self.tmp, "")
        with mock.patch("subprocess.run") as run, mock.patch.object(W, "_deliverable_exemplars") as scan:
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "task", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(unit="_kernel/owner", worker_type="owner"), "t", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(unit=None), "t", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(worker_type="frame", unit="plan/frame"), "t", {}, root=root), "")
        run.assert_not_called()
        scan.assert_not_called()

    def test_owner_and_frame_ignore_a_declaration(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        with mock.patch("subprocess.run") as run:
            for kind in ("owner", "frame"):
                self.assertEqual(W.unit_bootstrap_prompt(self._args(worker_type=kind), "t", {}, root=root), "")
        run.assert_not_called()

    def test_gate_limited_unit_skips_other_gates(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n  gates: [research-report]\n",
                               unit="research/research-survey")
        _fake_mem(root, "print('# Saved preferences\\n## [durable/feedback] id (global)\\nbody')\n")
        args = self._args(unit="research/research-survey", completion_gate="research-retrieval")
        with mock.patch("subprocess.run") as run:
            self.assertEqual(W.unit_bootstrap_prompt(args, "t", {}, root=root), "")
            run.assert_not_called()
        args = self._args(unit="research/research-survey", completion_gate="research-report")
        section = W.unit_bootstrap_prompt(args, "t", {}, root=root)
        self.assertIn("Unit bootstrap material", section)
        self.assertIn("body", section)

    def test_exemplar_without_a_location_row_still_carries_memory(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n  exemplar: deliverable\n")
        _fake_mem(root, "print('## [durable/feedback] id (global)\\nprefer html')\n")
        section = W.unit_bootstrap_prompt(self._args(completion_gate="brand-new-gate"), "t", {}, root=root)
        self.assertIn("prefer html", section)
        self.assertNotIn("Format exemplar", section)

    def test_exemplar_line_and_missing_artifact_root(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  exemplar: deliverable\n")
        reports = self.tmp / ".agent_reports"
        cycle = _cycle(reports, "c", "2026-01-01_a", files=[("report/REPORT.md", 3000)])
        section = W.unit_bootstrap_prompt(self._args(artifact_root=str(reports)), "t", {}, root=root)
        self.assertIn(str(cycle / "artifacts/report/REPORT.md"), section)
        self.assertIn("(3 KB)", section)
        self.assertNotIn("Saved preferences", section)
        args = self._args()
        del args.artifact_root
        self.assertEqual(W.unit_bootstrap_prompt(args, "t", {}, root=root), "")

    def test_memory_block_is_capped(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        _fake_mem(root, "print('Q' * 20000)\n")
        section = W.unit_bootstrap_prompt(self._args(), "t", {}, root=root)
        self.assertLessEqual(len(section.encode()), 6144 + 700)
        self.assertLess(section.count("Q"), 6145)

    def test_memory_failures_are_silent(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        for body in (None, "import sys; sys.exit(1)\n", "import time; time.sleep(10)\n", "print('')\n"):
            if body is not None:
                _fake_mem(root, body)
            got = W._bootstrap_memory_block(("report-format",), "t", str(self.tmp), root=root, timeout_s=0.5)
            self.assertEqual(got, "", body)
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "t", {}, root=root), "")

    def test_unknown_topic_and_kind_are_ignored(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [nope]\n  exemplar: nothing\n")
        with mock.patch("subprocess.run") as run:
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "t", {}, root=root), "")
        run.assert_not_called()

    def test_memory_query_carries_topic_project_and_task_words(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        wt = self.tmp / "My_Project-x"
        wt.mkdir()
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="## m\nbody\n")

        with mock.patch("subprocess.run", side_effect=fake_run):
            W._bootstrap_memory_block(("report-format",), "speech /path/x a=b 42 evaluation", str(wt), root=root)
        cmd = captured["cmd"]
        queries = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--query"]
        self.assertIn("report", queries[0])
        self.assertEqual(queries[1], "My Project speech evaluation")
        self.assertEqual(cmd[cmd.index("--limit") + 1], "4")
        self.assertEqual(cmd[cmd.index("--max-bytes") + 1], "6144")


if __name__ == "__main__":
    unittest.main()
