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
import shutil
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
    def test_old_design_declaration_requires_exact_immutable_basis(self):
        current = R.TOPO.load_registry()

        def legacy_value(value):
            if isinstance(value, str):
                if value == "designs":
                    return "designs/<cycle>"
                if value.startswith("designs/"):
                    return value.replace("designs/", "designs/<cycle>/", 1)
                return value
            if isinstance(value, list):
                return [legacy_value(item) for item in value]
            if isinstance(value, dict):
                return {key: legacy_value(item) for key, item in value.items()}
            return value

        old_registry = legacy_value(current)
        def flat_value(value):
            if isinstance(value, str):
                return value.replace("designs/<cycle>", "designs")
            if isinstance(value, list):
                return [flat_value(item) for item in value]
            if isinstance(value, dict):
                return {key: flat_value(item) for key, item in value.items()}
            return value
        self.assertEqual(flat_value(old_registry), current)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sealed-v3.43.6"
            (root / "capabilities").mkdir(parents=True)
            shutil.copytree(ROOT / "roles" / "units", root / "roles" / "units")
            (root / "capabilities" / "topologies.json").write_text(
                json.dumps(old_registry, ensure_ascii=False), encoding="utf-8")
            unit_digest = R.unit_catalog_digest(root / "roles" / "units")
            basis = {"basis_version": 1, "registry_root": str(root),
                     "runtime_root": str(root), "unit_catalog_root": str(root),
                     "runtime_root_validated": True, "runtime_root_match": True}
            route = {"capability": "autopilot-design",
                     "registry_digest": R.TOPO.registry_digest(old_registry),
                     "capability_registry_digest": R.TOPO.capability_registry_digest(
                         old_registry, "autopilot-design"),
                     "unit_catalog_digest": unit_digest, "validation_basis": basis}
            original_hash = R.route_hash(route)
            actual_unit_digest = R.unit_catalog_digest
            with mock.patch.object(R, "_verified_immutable_release_identity",
                                   return_value={"version": "v3.43.6"}), \
                 mock.patch.object(R, "unit_catalog_digest", side_effect=lambda path=None:
                                   unit_digest if path is None else actual_unit_digest(path)):
                accepted = R._authenticated_legacy_design_registry(route, basis, current)
                self.assertEqual(accepted, old_registry)
                self.assertEqual(R.route_hash(route), original_hash)
                changed = json.loads(json.dumps(current))
                changed["artifact_buckets"]["documents"] += "/tampered"
                self.assertIsNone(R._authenticated_legacy_design_registry(route, basis, changed))
                with mock.patch.object(R, "unit_catalog_digest", return_value="sha256:" + "0" * 64):
                    self.assertIsNone(R._authenticated_legacy_design_registry(route, basis, current))
            with mock.patch.object(R, "_verified_immutable_release_identity", return_value=None):
                self.assertIsNone(R._authenticated_legacy_design_registry(route, basis, current))


if __name__ == '__main__':
    unittest.main()
