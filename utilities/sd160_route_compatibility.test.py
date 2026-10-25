#!/usr/bin/env python3
"""SD-160 old sealed routes: exact registry bridge and common lifecycle gates.

Fixtures were emitted by capability-route.py and topologies.json from 63a92974,
using compose_route(autopilot-code/dev, staged, standard|strong), all eight base
nodes, Codex parent, checked Codex/Claude children, and --unassigned. No current
compiler or _sd160_legacy_registry generated their graphs. Their original
absolute locators and route hashes are intentionally retained. Tests never run
Git, the old compiler, a model, or a wrapper child process. Registration, launch
claim and completion run the real common writers against temporary state;
this does not assert adapter process spawning or workflow/human-gate release.

Generation input SHA-256:
capability-route.py e844a5ab90f9e370dc9489ce4a1da593dc884746ba956842e2a359bad5f6b328
topologies.json b67129e9eaaaa1888495aba0ade0fd1a10aa682c9d20484809516d17f17edb88
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import dispatch_contract as D
import replica_batch_contract as M

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name('test-fixtures')
LEGACY_REGISTRY = 'sha256:4260c26aebc1ab2f5bbb722d1b77a0d3fe555504a4122ec5dd3c891dd8071f10'
# SHA-256 of the exact, inspectable fixture files; locators are not rewritten.
FIXTURE_SHA256 = {
    'standard': '57d184dc645a5a8a446a3574b25bcbe45d50eb5b472df5bad4f3a557b48610f3',
    'strong': 'a22b05e449725a14e8c02de3428c0d4d6afa341598e05ef3f3739250348e5f18',
}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'utilities' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load('sd160_compatibility_route', 'capability-route.py')
B = load('sd160_compatibility_batch', 'dispatch-batch.py')


class LegacyRouteCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'dispatch' / 'jobs.log'
        self.jobs.parent.mkdir(); self.jobs.touch()
        environment = mock.patch.dict(os.environ, {
            'AGENT_HOME': str(ROOT), 'AGENT_DISPATCH_JOBS': str(self.jobs),
            'AGENT_ARTIFACT_ROOT': str(self.root / 'artifacts'),
            'AGENT_ARTIFACT_CYCLE_ID': '', 'AGENT_ARTIFACT_PRODUCER_ID': '',
            'AGENT_MODEL_GOVERNOR_ROOT': str(self.root / 'governor'),
        })
        environment.start(); self.addCleanup(environment.stop)

    def fixture(self, intensity):
        path = FIXTURES / f'sd160-legacy-{intensity}.json'
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURE_SHA256[intensity])
        route = json.loads(raw)
        self.assertNotIn('persona_independence_contract_version', route)
        self.assertEqual(route['registry_digest'], LEGACY_REGISTRY)
        self.assertEqual(route['route_hash'], R.route_hash(route))
        return path, raw, route

    def unchanged(self, path, raw, route, original):
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(route, original)
        self.assertEqual(route['route_hash'], R.route_hash(route))

    def reseal(self, route):
        route['route_hash'] = R.route_hash(route)
        route['route_id'] = 'rt-' + route['route_hash'].split(':', 1)[1][:16]
        return route

    def _row(self, route, node):
        aid = 'att-legacy-' + node['id']
        metadata = {
            'attempt_schema_version': '2', 'dispatch_depth': str(node['dispatch_depth']),
            'transport': 'headless', 'execution_surface': 'registered-headless',
            'registered_worker': '1', 'fallback_hop': 'same-harness-headless',
            'harness': 'codex', 'parent_harness': 'codex', 'attempt_id': aid,
            'worker_type': node.get('worker_type') or ('review' if node['kind']=='review-worker' else 'stage'),
            'route_id': route['route_id'], 'route_hash': route['route_hash'],
            'route_node': node['id'], 'unit': node['unit'],
            'model_profile': node['model_profile'],
        }
        return aid, '\t'.join(['now', 'open', str(self.root), str(self.root), node['id'],
                               ','.join(key+'='+value for key,value in metadata.items())])

    def _lifecycle(self, route, nodes, admission):
        """Common register/start admission and completion writers, no subprocess."""
        def check(lines):
            R.verify_route(route)
            admission(lines)
        rows = [self._row(route, node) for node in nodes]
        for aid, row in rows:
            self.assertTrue(D.claim_attempt_row(self.jobs, aid, row, mutation_precheck=check))
        registered = self.jobs.read_text()
        self.assertEqual(registered.count('launch_claimed=0'), len(nodes))
        for node, (aid, row) in zip(nodes, rows):
            self.assertTrue(D.claim_attempt_row(self.jobs, aid, row, launch=True, mutation_precheck=check))
            self.assertFalse(D.claim_attempt_row(self.jobs, aid, row, launch=True, mutation_precheck=check))
        markers = {}
        for node, (aid, _) in zip(nodes, rows):
            evidence = self.root / (node['id'] + '.md')
            evidence.write_text('PASS: isolated compatibility fixture\n')
            marker, result = R._complete_node_locked(route, node, node['id'], evidence,
                                                     jobs=self.jobs, attempt_id=aid)
            self.assertEqual(result['status'], 'closed')
            self.assertEqual(marker['route_hash'], route['route_hash'])
            self.assertEqual(marker['attempt_id'], aid)
            if node['kind'] == 'review-worker':
                self.assertEqual(marker['review_independence'], 'independent')
            markers[node['id']] = marker
            self.assertTrue((R.completion_dir(route['route_id']) / (node['id']+'.json')).is_file())
            _, replay = R._complete_node_locked(route, node, node['id'], evidence,
                                                jobs=self.jobs, attempt_id=aid)
            self.assertEqual(replay['status'], 'already-closed')
        final = self.jobs.read_text().splitlines()
        self.assertEqual(len(final), len(nodes))
        self.assertTrue(all(line.split('\t')[1] == 'done' for line in final))
        self.assertTrue(all(D.parse_registry_metadata(line.split('\t')[-1])['note'] == 'completed-marker'
                            for line in final))
        return markers, final

    def test_actual_old_composed_routes_verify_without_identity_or_byte_changes(self):
        self.assertNotEqual(R.TOPO.registry_digest(R.TOPO.load_registry()), LEGACY_REGISTRY)
        for intensity in FIXTURE_SHA256:
            with self.subTest(intensity=intensity):
                path, raw, route = self.fixture(intensity)
                original = copy.deepcopy(route)
                R.verify_route(route)
                frames = [n for n in route['nodes'] if n.get('worker_type') == 'frame']
                self.assertEqual(len(frames), 2)
                self.assertTrue(all('perspective' not in n for n in frames))
                self.assertEqual([n['unit'] for n in frames], ['plan/frame','plan/frame'])
                self.assertEqual([n['model_profile'] for n in frames], ['top','deep'])
                self.unchanged(path, raw, route, original)

    def test_legacy_frame_register_start_complete_accepts_same_harness_personas(self):
        path, raw, route = self.fixture('standard'); original = copy.deepcopy(route)
        frames = [n for n in route['nodes'] if n.get('worker_type') == 'frame']
        def admission(lines):
            self.assertEqual(D.frame_harness_admission(route,self.jobs,lines,
                ['codex','codex'],[n['model_profile'] for n in frames]), [])
        with mock.patch('dispatch_degradation.record_degradation') as degraded:
            markers, lines = self._lifecycle(route, frames, admission)
            plan = next(n for n in route['nodes'] if n['id'] == 'plan')
            D._frame_pair_attempt_gate(route, plan, markers, self.jobs, lines)
        degraded.assert_not_called()
        self.unchanged(path, raw, route, original)

    def _manifest(self, route, nodes):
        members = [{'assignment_sha256':'sha256:'+hashlib.sha256(n['id'].encode()).hexdigest(),
            'attempt_id':'att-legacy-'+n['id'], 'route_node':n['id'], 'harness':'codex',
            'fallback_hop':'same-harness-headless', 'fallback_ordinal':1,
            'model_profile':n['model_profile'], 'perspective':n['perspective'],
            'parallel_leg_index':n['parallel_leg_index'], 'leg_class':n['leg_class']}
            for n in nodes]
        return M.build_manifest(parallel_group=nodes[0]['parallel_group'], route_id=route['route_id'],
            parent_attempt_id='att-isolated-owner', independence='persona', members=members,
            required_independence_axes=nodes[0]['parallel_independence_axes'],
            realized_independence_axes=['model-profile','perspective'])

    def test_legacy_strong_review_group_register_start_complete_preserves_sealed_axes(self):
        path, raw, route = self.fixture('strong'); original = copy.deepcopy(route)
        nodes = B.parallel_nodes(route, 'impl-review')
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]['parallel_independence_axes'], ['cross-harness','model-profile','perspective'])
        manifest, digest, legs = self._manifest(route,nodes)
        frozen_manifest = copy.deepcopy(manifest)
        def admission(lines):
            self.assertEqual(M.verify_manifest(manifest), (manifest,digest,legs))
            self.assertEqual(B.parallel_nodes(route,'impl-review'), nodes)
            for n in nodes:
                selected = B.DISPATCH_NODE.resolve_checked_tuple(route,n,'codex',
                    {'parent_harness':'codex','parent_transport':'headless','parent_sandbox':'workspace-write'})
                self.assertEqual(selected.fallback_hop,'same-harness-headless')
        self._lifecycle(route,nodes,admission)
        self.assertEqual(manifest,frozen_manifest)
        self.unchanged(path, raw, route, original)

    def test_legacy_group_duplicate_persona_fails_before_registry_mutation(self):
        _,_,route = self.fixture('strong')
        nodes = copy.deepcopy(B.parallel_nodes(route,'impl-review'))
        nodes[1]['perspective'] = nodes[0]['perspective']
        with self.assertRaises(M.ReplicaBatchContractError):
            self._manifest(route,nodes)
        self.assertEqual(self.jobs.read_text(),'')

    def test_resealed_profile_width_role_and_registry_tampering_still_fails(self):
        _,_,original = self.fixture('strong')
        original['unit_catalog_digest'] = R.unit_catalog_digest()
        self.reseal(original)
        for field,value in [('model_profile','light'),('parallel_leg_count',3),('role','invented reviewer')]:
            with self.subTest(field=field):
                route = copy.deepcopy(original)
                node = next(n for n in route['nodes'] if n.get('parallel_group')=='impl-review')
                node[field] = value; self.reseal(route)
                aid,row = self._row(route,node)
                with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
                    with self.assertRaises(ValueError):
                        D.claim_attempt_row(self.jobs,aid,row,
                            mutation_precheck=lambda lines: R.verify_route(route))
        route = copy.deepcopy(original); route['registry_digest']='sha256:'+'0'*64; self.reseal(route)
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            with self.assertRaisesRegex(ValueError,'registry'): R.verify_route(route)
        self.assertEqual(self.jobs.read_text(),'')

    def test_exact_registry_bridge_does_not_accept_other_registry_or_unit_catalog_drift(self):
        _,_,route = self.fixture('strong')
        route['unit_catalog_digest'] = R.unit_catalog_digest()
        self.reseal(route)
        registry = R.TOPO.load_registry()
        for mutation in ('role','width','model_profile','unrelated'):
            changed = copy.deepcopy(registry)
            recipe = next(r for r in changed['recipes'] if r['capability']=='autopilot-code')
            if mutation == 'width':
                recipe['standard_plus']['parallel_groups'][0]['width_by_intensity']['strong'] = 3
            elif mutation == 'unrelated':
                changed['fixture_unrelated_change'] = True
            else:
                recipe['standard_plus']['nodes'][0][mutation] = 'unexpected'
            with self.subTest(mutation=mutation), \
                    mock.patch.object(R.TOPO,'load_registry',return_value=changed), \
                    mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
                with self.assertRaisesRegex(ValueError,'registry'): R.verify_route(route)
        with mock.patch.object(R,'unit_catalog_digest',return_value='sha256:'+'e'*64), \
                mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            with self.assertRaisesRegex(ValueError,'registry|unit.catalog'): R.verify_route(route)
        self.assertEqual(self.jobs.read_text(),'')

    def test_legacy_payload_cannot_claim_new_persona_contract_to_bypass_digest(self):
        _,_,original = self.fixture('standard')
        original['unit_catalog_digest'] = R.unit_catalog_digest()
        self.reseal(original)
        for version in (1,2,True):
            with self.subTest(version=version):
                route=copy.deepcopy(original); route['persona_independence_contract_version']=version
                self.reseal(route)
                with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
                    with self.assertRaises(ValueError): R.verify_route(route)


class LegacyDesignRegistryBasisTest(unittest.TestCase):
    @staticmethod
    def _legacy_value(value):
        if isinstance(value, str):
            if value == "designs":
                return "designs/<cycle>"
            if value.startswith("designs/"):
                return "designs/<cycle>/" + value[len("designs/"):]
            return value
        if isinstance(value, list):
            return [LegacyDesignRegistryBasisTest._legacy_value(item) for item in value]
        if isinstance(value, dict):
            return {key: LegacyDesignRegistryBasisTest._legacy_value(item)
                    for key, item in value.items()}
        return value

    def _original_style_route(self, old_root):
        current = R.TOPO.load_registry()
        old_registry = self._legacy_value(current)
        base = Path(old_root).parent
        artifact_root = base / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)
        jobs = base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        checked = {
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture", "probe_time": "2026-10-25T00:00:00Z",
            "failure_class": "", "checked_worktree": str(ROOT),
            "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0,
        }
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        with mock.patch.dict(os.environ, {
                "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_ARTIFACT_ROOT": str(artifact_root), "HEARTING_GATES": "on",
        }, clear=False):
            route = R.compile_route(
                capability="autopilot-design", capability_mode="default",
                requested_intensity="standard", cwd=ROOT, artifact_root=artifact_root,
                transport="headless", tracking="tracked", tracked_gate_evidence=gate,
                dispatch_evidence={"tuples": [checked], "native_subagent": []},
                registered_headless_evidence={"candidates": [{
                    "harness": "codex", "transport": "headless",
                    "surface": "registered-headless", "status": "supported",
                    "probe_source": "fixture", "probe_time": "2026-10-25T00:00:00Z",
                }]},
            )
        route = self._legacy_value(route)
        route["registry_digest"] = R.TOPO.registry_digest(old_registry)
        route["capability_registry_digest"] = R.TOPO.capability_registry_digest(
            old_registry, "autopilot-design")
        route["validation_basis"] = {
            "basis_version": 1, "registry_root": str(old_root),
            "runtime_root": str(old_root), "unit_catalog_root": str(old_root),
            "runtime_root_validated": True, "runtime_root_match": True,
        }
        route["route_hash"] = R.route_hash(route)
        route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
        return current, old_registry, route

    def test_pre_authoring_design_registry_keeps_exact_digest_after_main_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            current, old, route = self._original_style_route(Path(temporary) / "pruned")
            code = next(item for item in old["recipes"] if item["capability"] == "autopilot-code")
            execute = next(item for item in code["standard_plus"]["nodes"] if item["id"] == "execute")
            for item in (code["quick"], execute):
                item["write_scope"].remove("spec/**")
                del item["guard_preconditions"]
            route["registry_digest"] = R.TOPO.registry_digest(old)
            route["capability_registry_digest"] = R.TOPO.capability_registry_digest(old, "autopilot-design")
            route["route_hash"] = R.route_hash(route)
            route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
            with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):
                self.assertEqual(R.verify_route(route)["route_hash"], route["route_hash"])
                changed = copy.deepcopy(current)
                next(item for item in changed["recipes"] if item["capability"] == "autopilot-code")[
                    "quick"]["resource_class"] = "changed"
                with mock.patch.object(R.TOPO, "load_registry", return_value=changed):
                    with self.assertRaisesRegex(ValueError, "registry"):
                        R.verify_route(route)

    def test_unavailable_old_catalogue_and_release_verify_from_exact_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.base = Path(temporary)
            for label, create_root in (("catalogue-missing", True), ("release-missing", False)):
                with self.subTest(old_root=label):
                    root = self.base / label
                    if create_root:
                        root.mkdir()
                    current, old_registry, route = self._original_style_route(root)
                    basis = route["validation_basis"]
                    original = copy.deepcopy(route)
                    original_hash = route["route_hash"]
                    accepted = R._authenticated_legacy_design_registry(route, basis, current)
                    self.assertEqual(accepted, old_registry)
                    self.assertFalse((root / "capabilities" / "topologies.json").exists())
                    self.assertEqual(route, original)
                    self.assertEqual(R.route_hash(route), original_hash)

                    route_file = self.base / f"{label}.json"
                    route_bytes = json.dumps(route, sort_keys=True).encode() + b"\n"
                    route_file.write_bytes(route_bytes)
                    old_path_calls = []
                    verified_identity = R._verified_immutable_release_identity

                    def only_current_release(path):
                        candidate = Path(path).resolve(strict=False)
                        if candidate == root.resolve(strict=False):
                            old_path_calls.append(str(candidate))
                            raise AssertionError("historical validation root was reopened")
                        return verified_identity(path)

                    with mock.patch.dict(os.environ, {
                            "AGENT_HOME": str(ROOT),
                            "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
                            "AGENT_ARTIFACT_ROOT": str(self.base / "artifacts"),
                            "HEARTING_GATES": "on",
                    }, clear=False), \
                         mock.patch.object(R, "_verified_immutable_release_identity",
                                           side_effect=only_current_release):
                        self.assertEqual(R.verify_route(route, expected_cwd=ROOT)["route_hash"],
                                         original_hash)
                        compatible, mismatches = R.revalidate_launch_compatibility(route)
                        self.assertTrue(compatible, mismatches)
                        argv = ["capability-route.py", "verify", "--route", str(route_file),
                                "--cwd", str(ROOT), "--launch-phase", "start"]
                        with mock.patch.object(R.sys, "argv", argv):
                            R.main()
                    self.assertEqual(old_path_calls, [])
                    self.assertEqual(route_file.read_bytes(), route_bytes)

    def test_exact_bridge_rejects_digest_registry_unit_recipe_and_tuple_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.base = Path(temporary)
            root = self.base / "pruned-release"
            current, old_registry, route = self._original_style_route(root)
            basis = route["validation_basis"]
            helper = R._authenticated_legacy_design_registry

            for key in ("registry_digest", "capability_registry_digest"):
                changed = copy.deepcopy(route)
                changed[key] = "sha256:" + "0" * 64
                self.assertIsNone(helper(changed, basis, current), key)

            changed_registries = {}
            changed = copy.deepcopy(current)
            changed["artifact_buckets"]["documents"] += "/tampered"
            changed_registries["unrelated-bytes"] = changed
            changed = copy.deepcopy(current)
            design = next(item for item in changed["recipes"]
                          if item.get("capability") == "autopilot-design")
            design["standard_plus"]["nodes"][0]["role"] = "fast implementer"
            changed_registries["role"] = changed
            changed = copy.deepcopy(current)
            changed["parallel_group_max_width"] += 1
            changed_registries["parallel-width"] = changed
            changed = copy.deepcopy(current)
            changed["owner_profile_by_intensity"]["standard"] = "balanced-deep"
            changed_registries["profile"] = changed
            for label, changed in changed_registries.items():
                self.assertIsNone(helper(route, basis, changed), label)

            with mock.patch.object(R, "unit_catalog_digest", return_value="sha256:" + "0" * 64):
                self.assertIsNone(helper(route, basis, current), "current unit drift")

            recipe_tamper = copy.deepcopy(route)
            recipe_tamper["composed"] = True
            recipe_tamper["composed_recipe"] = next(
                item for item in old_registry["recipes"]
                if item.get("capability") == "autopilot-design")
            recipe_tamper["capability_registry_digest"] = R.TOPO.capability_registry_digest(
                old_registry, "autopilot-design", [recipe_tamper["composed_recipe"]])
            self.assertIsNone(helper(recipe_tamper, basis, current), "unverified embedded recipe")
            recipe_tamper["composed_recipe"] = copy.deepcopy(recipe_tamper["composed_recipe"])
            recipe_tamper["composed_recipe"]["standard_plus"]["nodes"][0]["role"] = "fast implementer"
            recipe_tamper["capability_registry_digest"] = R.TOPO.capability_registry_digest(
                old_registry, "autopilot-design", [recipe_tamper["composed_recipe"]])
            self.assertIsNone(helper(recipe_tamper, basis, current), "tampered embedded recipe")

            node_tamper = copy.deepcopy(route)
            node_tamper["nodes"][0]["write_scope"] = ["designs/<cycle>/outside/**"]
            node_tamper["route_hash"] = R.route_hash(node_tamper)
            node_tamper["route_id"] = "rt-" + node_tamper["route_hash"].split(":", 1)[1][:16]
            with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}, clear=False):
                with self.assertRaisesRegex(ValueError, "node frame outputs outside write_scope|nodes differ"):
                    R.verify_route(node_tamper, expected_cwd=ROOT)

            tuple_tamper = copy.deepcopy(route)
            tuple_tamper["launch_compatibility_tuple"]["registry_root"]["binding_digest"] = "sha256:" + "0" * 64
            tuple_tamper["route_hash"] = R.route_hash(tuple_tamper)
            tuple_tamper["route_id"] = "rt-" + tuple_tamper["route_hash"].split(":", 1)[1][:16]
            with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(ROOT), "HEARTING_GATES": "on",
            }, clear=False):
                compatible, mismatch = R.revalidate_launch_compatibility(tuple_tamper)
            self.assertFalse(compatible)
            self.assertIn("registry_root", mismatch)

    def test_valid_composed_legacy_recipe_verifies_without_its_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifact_root = base / "artifacts"
            artifact_root.mkdir()
            jobs = base / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            missing_release = base / "pruned-release"
            current = R.TOPO.load_registry()
            old_registry = self._legacy_value(current)
            checked = {
                "parent_harness": "codex", "parent_transport": "headless",
                "parent_sandbox": "workspace-write", "child_harness": "codex",
                "launch_authority": "conductor", "status": "supported",
                "probe_source": "fixture", "probe_time": "2026-10-25T00:00:00Z",
                "failure_class": "", "checked_worktree": str(ROOT),
                "failure_scope": "none", "codex_command": "ok",
                "retry_on_isolated_worktree": 0,
            }
            readiness = {
                "tuples": [checked],
                "candidates": [{
                    "harness": "codex", "transport": "headless",
                    "surface": "registered-headless", "status": "supported",
                    "probe_source": "fixture", "probe_time": "2026-10-25T00:00:00Z",
                }],
            }
            with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
                    "AGENT_ARTIFACT_ROOT": str(artifact_root), "HEARTING_GATES": "on",
            }, clear=False), mock.patch.object(R, "_compose_readiness", return_value=readiness):
                route = R.compose_route(
                    capability="autopilot-design", capability_mode="default", shape="staged",
                    graph="build,visual-verify,critic-review,handoff", slug="legacy-composed-fixture",
                    cwd=ROOT, artifact_root=artifact_root, spec_read="fixture",
                    drift_verdict="within-spec", tracking="tracked", artifact_guard="fixture",
                    dispatch_evidence={"tuples": [checked], "native_subagent": []}, unassigned=True,
                )
            route = self._legacy_value(route)
            route["registry_digest"] = R.TOPO.registry_digest(old_registry)
            route["capability_registry_digest"] = R.TOPO.capability_registry_digest(
                old_registry, "autopilot-design", [route["composed_recipe"]])
            route["validation_basis"] = {
                "basis_version": 1, "registry_root": str(missing_release),
                "runtime_root": str(missing_release), "unit_catalog_root": str(missing_release),
                "runtime_root_validated": True, "runtime_root_match": True,
            }
            route["route_hash"] = R.route_hash(route)
            route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
            route_file = base / "composed-route.json"
            # Preserve registry declaration order in this original-style route;
            # the width-tier map is an ordered contract.
            route_bytes = json.dumps(route, ensure_ascii=False, indent=2).encode() + b"\n"
            route_file.write_bytes(route_bytes)
            verified = R.verify_route(json.loads(route_file.read_text()), expected_cwd=ROOT)
            self.assertEqual(verified["route_hash"], route["route_hash"])
            self.assertEqual(route_file.read_bytes(), route_bytes)


if __name__ == '__main__':
    unittest.main()
