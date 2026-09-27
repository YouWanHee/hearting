"""Replay one SD-157 batch leg through the existing sealed batch admission.

The original N-member manifest and N-1 successful peers remain the authority.
Only the family claim supplies a different gap identity; this is not a new route
or an SD-106 cancellation/retry claim.
"""
from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import sys

import dispatch_contract as DC
import dispatch_replacement as R
from replica_batch_contract import ReplicaBatchContractError, verify_manifest

SCHEMA = 'automatic-parallel-replacement-v1'
INPUT_SCHEMA = 'automatic-parallel-input-v1'


def _batch():
    spec = importlib.util.spec_from_file_location('_automatic_replacement_batch',
                                                R.ROOT/'utilities/dispatch-batch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _input_path(jobs, digest):
    from replica_batch_contract import DIGEST
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise DC.DispatchContractError('replacement-batch-manifest-invalid')
    return R._directory(jobs)/'batch-inputs'/(digest.split(':')[1]+'.json')


def seal_launch_input(jobs, args, route, manifest, digest):
    """Persist resolved batch input before the first reservation or child start."""
    if args.continuation is not None:
        return
    checked, actual, _ = verify_manifest(manifest)
    if actual != digest:
        raise DC.DispatchContractError('replacement-batch-manifest-invalid')
    options = {
        'route': str(args.route.resolve()), 'parallel_group': args.parallel_group,
        'slug_prefix': args.slug_prefix, 'parent': args.parent,
        'prompt_text': args.prompt_text, 'qa': args.qa,
        'log_dir': str(args.log_dir) if args.log_dir else None,
        'allow_degraded_independence': args.allow_degraded_independence,
        'subdivision_manifest': getattr(args, 'subdivision_manifest', None),
    }
    if getattr(args, 'reviewed_evidence', None):
        options['reviewed_evidence'] = args.reviewed_evidence
    payload = {'schema': INPUT_SCHEMA, 'jobs': str(Path(jobs).resolve()),
               'route_id': route['route_id'], 'route_hash': route['route_hash'],
               'manifest': checked, 'manifest_digest': digest, 'options': options}
    path = _input_path(jobs, digest)
    previous = R._read(path)
    if previous:
        # Display prefixes do not participate in the canonical batch identity.
        # Keep the first launch input when an idempotent caller changes its label.
        if not isinstance(previous.get('options'), dict) or not previous['options'].get('slug_prefix'):
            raise DC.DispatchContractError('replacement-batch-input-conflict')
        normalized = dict(payload, options=dict(options, slug_prefix=previous['options']['slug_prefix']))
        if previous != normalized:
            raise DC.DispatchContractError('replacement-batch-input-conflict')
        return
    R._once(path, payload)


def _input(jobs, source):
    payload = R._read(_input_path(jobs, source.get('batch_manifest_sha256')))
    if (not payload or payload.get('schema') != INPUT_SCHEMA
            or payload.get('jobs') != str(Path(jobs).resolve())):
        raise DC.DispatchContractError('replacement-batch-input-unproven')
    try:
        manifest, digest, leg_digests = verify_manifest(payload.get('manifest'))
    except ReplicaBatchContractError as exc:
        raise DC.DispatchContractError('replacement-batch-source-drift', str(exc)) from exc
    aid = source['attempt_id']
    member = next((m for m in manifest['members'] if m['attempt_id'] == aid), None)
    options = payload.get('options')
    if not isinstance(options, dict) or not isinstance(options.get('prompt_text'), str):
        raise DC.DispatchContractError('replacement-batch-input-unproven')
    assignment = 'sha256:'+hashlib.sha256(options['prompt_text'].encode()).hexdigest()
    if (digest != source.get('batch_manifest_sha256')
            or payload.get('manifest_digest') != digest or not member
            or leg_digests[aid] != source.get('batch_leg_sha256')
            or member['route_node'] != source.get('route_node')
            or manifest['route_id'] != source.get('route_id')
            or manifest['parent_attempt_id'] != source.get('parent_attempt_id')
            or any(m['assignment_sha256'] != assignment for m in manifest['members'])
            or options.get('parallel_group') != manifest.get('parallel_group')
            or options.get('parent') != source.get('parent')):
        raise DC.DispatchContractError('replacement-batch-source-drift')
    return payload


def _evidence_path(jobs, family):
    # Validate the family using the same closed vocabulary as the family record.
    if not isinstance(family, str):
        raise DC.DispatchContractError('replacement-batch-family-invalid')
    R._record_path(jobs, family)
    return R._directory(jobs)/'batch-claims'/(family+'.json')


def validate_evidence(jobs, lines, evidence, source_route=None):
    """Validate under the jobs lock; peer marker checks run separately outside it."""
    if not isinstance(evidence, dict) or evidence.get('schema') != SCHEMA:
        raise DC.DispatchContractError('replacement-batch-claim-invalid')
    family = evidence.get('family_id')
    if R._read(_evidence_path(jobs, family)) != evidence:
        raise DC.DispatchContractError('replacement-batch-claim-drift')
    record = R._read(R._record_path(jobs, family))
    if not record:
        raise DC.DispatchContractError('replacement-batch-claim-invalid')
    fields, source, replay = R.validate_claim_source(jobs, lines, record)
    if (evidence.get('jobs') != str(Path(jobs).resolve())
            or evidence.get('claim_digest') != R._digest(record)
            or evidence.get('source_route_id') != record['route_id']
            or evidence.get('source_route_hash') != record['route_hash']):
        raise DC.DispatchContractError('replacement-batch-claim-binding-mismatch')
    if source_route is not None and (
            source_route.get('route_id') != record['route_id']
            or source_route.get('route_hash') != record['route_hash']):
        raise DC.DispatchContractError('replacement-batch-route-drift')
    payload = _input(jobs, source)
    partial = evidence.get('partial_group_continuation') or {}
    if (partial.get('failed_source_attempt_id') != record['original_attempt_id']
            or partial.get('replacement_attempt_id') != record['replacement_attempt_id']
            or partial.get('gap_leg_id') != source.get('route_node')
            or partial.get('source_batch_manifest_digest') != payload['manifest_digest']
            or evidence.get('input_digest') != R._digest(payload)):
        raise DC.DispatchContractError('replacement-batch-source-drift')
    return record, payload


def load_evidence(path, route, group):
    evidence = R._read(Path(path))
    if not evidence or evidence.get('schema') != SCHEMA:
        raise DC.DispatchContractError('replacement-batch-claim-invalid')
    jobs = Path(evidence.get('jobs', ''))
    if not jobs.is_absolute() or Path(path).resolve() != _evidence_path(jobs, evidence.get('family_id')):
        raise DC.DispatchContractError('replacement-batch-claim-path-invalid')
    with R._locked(jobs) as lines:
        validate_evidence(jobs, lines, evidence, route)
    partial = evidence['partial_group_continuation']
    if partial.get('source_group_id') != group:
        raise DC.DispatchContractError('replacement-batch-group-drift')
    return evidence, partial


def command(jobs, record, source, replay):
    """Return a checked batch replay command, or None for an ordinary worker."""
    if not source.get('batch_manifest_sha256'):
        return None
    payload = _input(jobs, source)
    batch = _batch()
    route = R._read(Path(record['route_file']))
    if (not route or route.get('route_id') != payload['route_id']
            or route.get('route_hash') != payload['route_hash']
            or payload['options'].get('route') != record['route_file']):
        raise DC.DispatchContractError('replacement-batch-route-drift')
    group = str(source.get('batch_group', ''))
    try:
        partial = batch.ROUTE_MODULE.partial_group_continuation(
            route, source_group_id=group, source_batch_manifest=payload['manifest'],
            failed_source_attempt_id=record['original_attempt_id'],
            gap_leg_id=source['route_node'])
    except (ValueError, KeyError) as exc:
        raise DC.DispatchContractError('replacement-batch-peer-unsettled', str(exc)) from exc
    partial['replacement_attempt_id'] = record['replacement_attempt_id']
    evidence = {'schema': SCHEMA, 'continuation_contract_version': 1,
                'continuation_id': 'replacement-'+record['family_id'],
                'family_id': record['family_id'], 'claim_digest': R._digest(record),
                'jobs': str(Path(jobs).resolve()), 'input_digest': R._digest(payload),
                'source_route_id': record['route_id'], 'source_route_hash': record['route_hash'],
                'partial_group_continuation': partial}
    path = _evidence_path(jobs, record['family_id'])
    R._once(path, evidence)
    with R._locked(jobs) as lines:
        validate_evidence(jobs, lines, evidence, route)
    options = payload['options']
    argv = [sys.executable, str(R.ROOT/'utilities/dispatch-batch.py'),
            '--action', 'start', '--jobs', str(Path(jobs).resolve()),
            '--continuation', str(path)]
    for name in ('route', 'parallel_group', 'slug_prefix', 'parent', 'prompt_text',
                 'qa', 'log_dir', 'subdivision_manifest', 'reviewed_evidence'):
        if options.get(name) is not None:
            argv += ['--'+name.replace('_', '-'), str(options[name])]
    if options.get('allow_degraded_independence'):
        argv += ['--allow-degraded-independence']
    return argv
