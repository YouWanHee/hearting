"""SD-157: one proof-bound replacement of a dead logical execution.

The jobs lock owns the claim and lineage. Launching stays in the checked adapter;
its existing registration/spawn fence handles a lost launcher response. Original
terminal rows are evidence and are never rewritten into replacement outcomes.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

import dispatch_contract as DC
from artifact_receipt import _write_once

ROOT = Path(__file__).resolve().parents[1]
DEATH_NOTES = frozenset({
    'dead-exact-pid', 'dead-namespace-absent', 'dead-worker-silent-exit',
    'dead-missing-result', 'dead-parent-orphaned', 'dead-governor-reservation-transfer',
    'dead-no-progress', 'dead-timeout',
})
SCHEMA = 'automatic-dead-replacement-v1'


def _bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))+'\n').encode()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'att-[A-Za-z0-9._-]{1,240}', value):
        raise DC.DispatchContractError('replacement-attempt-invalid')
    return value


def _once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _bytes(value)
    if path.is_symlink() or (not _write_once(path.parent, path, raw) and path.read_bytes() != raw):
        raise DC.DispatchContractError('replacement-record-conflict', str(path))


def _read(path):
    if path.is_symlink():
        raise DC.DispatchContractError('replacement-record-symlink', str(path))
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DC.DispatchContractError('replacement-record-unreadable', str(path)) from exc
    if not isinstance(value, dict):
        raise DC.DispatchContractError('replacement-record-invalid', str(path))
    return value


def _directory(jobs):
    return Path(jobs).resolve().parent / 'automatic-replacements'


def _rows(lines):
    result = {}
    for line in lines:
        fields = line.split('\t')
        if len(fields) != 6:
            continue
        meta = DC.parse_registry_metadata(fields[5])
        aid = meta.get('attempt_id')
        if aid:
            if aid in result:
                raise DC.DispatchContractError('replacement-attempt-ambiguous', aid)
            result[aid] = (fields, meta)
    return result


@contextmanager
def _locked(jobs):
    jobs = Path(jobs).resolve()
    DC.ensure_global_registry_writable(jobs)
    with Path(str(jobs)+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield jobs.read_text().splitlines()


INPUT_OPTIONS = frozenset({'--start', '--register', '--dry-run', '--attempt-id',
    '--jobs', '--worktree', '--prompt-file', '--prompt-text', '--automatic-retry-of'})
RESOLVED_INPUT_KEYS = ('capability', 'capability_mode', 'unit', 'worker_type',
    'worker_mode', 'assigned_contract', 'dispatch_depth', 'intensity', 'qa',
    'sandbox', 'permission_mode', 'parent_harness', 'parent_transport',
    'parent_sandbox', 'launch_authority', 'parent_session_id', 'parent_attempt_id',
    'execution_surface', 'registered_worker', 'fallback_hop', 'model_role',
    'model_profile', 'model', 'reasoning', 'resolved_model_settings',
    'resolved_completion_delivery', 'parent_completion_delivery')


def _applied_permissions(args):
    result = {}
    posture = getattr(args, 'resolved_permission_posture', None)
    if isinstance(posture, dict):
        result['claude'] = {key:posture.get(key) for key in
                           ('mode','mode_flag','allowed_tools','inherited_default_mode')}
    if hasattr(args, 'replacement_runtime_sandbox'):
        result['runtime_sandbox'] = args.replacement_runtime_sandbox
    grant = getattr(args, 'execution_access_grant', None)
    if grant is not None:
        result['execution_access'] = json.loads(json.dumps(dataclasses.asdict(grant), default=str))
    config = getattr(args, 'opencode_config_content', None)
    if config:
        parsed = json.loads(config)
        result['opencode_permission'] = parsed.get('permission')
    for key in ('launch_lifecycle','nested_headless_network'):
        if hasattr(args,key): result[key] = getattr(args,key)
    return result


def seal_launch_input(args, harness: str, task: str) -> str:
    """Called after wrapper validation, before its first registry mutation.

    Only raw caller input is retained: the rendered worker prompt embeds old
    identities and must never be replayed. No credentials/environment snapshot.
    """
    argv = getattr(args, 'replacement_input_argv', None)
    if argv is None or not getattr(args, 'attempt_id', None):
        return ''  # Legacy callers remain observable but cannot invent replay input.
    aid = _id(args.attempt_id)
    if harness not in {'claude', 'codex', 'opencode'} or not isinstance(task, str):
        raise DC.DispatchContractError('replacement-input-invalid')
    jobs = Path(args.jobs_path).resolve()
    defaults = {'--'+key.replace('_','-'): str(getattr(args,key))
                for key in ('parent_session_id','parent_attempt_id','parent_harness',
                            'parent_transport','parent_sandbox','launch_authority',
                            'sandbox','permission_mode','reviewed_evidence') if getattr(args,key,None)}
    normalized = _canonical_argv(_replace_options(list(argv), defaults, remove=INPUT_OPTIONS))
    payload = {
        'schema': SCHEMA, 'attempt_id': aid, 'harness': harness,
        'jobs': str(jobs), 'worktree': str(Path(args.worktree).resolve()),
        'argv': normalized, 'task': task,
        'resolved': {key: getattr(args, key) for key in RESOLVED_INPUT_KEYS
                     if hasattr(args, key)},
        'launch_home': str(ROOT), 'applied_permissions': _applied_permissions(args),
        'route_id': getattr(args, 'route_id', None) or '',
        'route_node': getattr(args, 'route_node', None) or '',
        'owner_route_id': getattr(getattr(args, 'owner_route_binding', None), 'route_id', ''),
    }
    _once(_directory(jobs)/'inputs'/(aid+'.json'), payload)
    return ',replacement_input_digest='+_digest(payload)


def launch_input(jobs, aid, meta):
    value = _read(_directory(jobs)/'inputs'/(_id(aid)+'.json'))
    if (value is None or value.get('schema') != SCHEMA or value.get('attempt_id') != aid
            or value.get('jobs') != str(Path(jobs).resolve())
            or _digest(value) != meta.get('replacement_input_digest')):
        raise DC.DispatchContractError('replacement-input-unproven', aid)
    return value


def _terminal_absent(fields, meta):
    from codex_dispatch_terminal import inspect_terminal_attempt
    result = inspect_terminal_attempt(meta.get('log_file'), worktree=fields[3],
                                      artifact_root_metadata=meta.get('artifact_root'))
    return result.get('state') == 'absent'


def death_proof(fields, meta):
    """No broad dead-* permission; a semantic result still owns settlement."""
    if fields[1] not in {'done', 'cancelled', 'killed'}:
        raise DC.DispatchContractError('replacement-terminal-unsettled')
    if DC.terminal_conflict_pending(meta):
        raise DC.DispatchContractError('replacement-terminal-conflict')
    automatic_receiptless = (meta.get('note') == 'cancelled-receipt-unavailable'
        and meta.get('classifier_source') == DC.AUTOMATIC_RECEIPTLESS_CLASSIFIER)
    if not automatic_receiptless and (meta.get('note') not in DEATH_NOTES
            or fields[1] in {'cancelled', 'killed'}):
        raise DC.DispatchContractError('replacement-not-silent-death')
    proof = DC.attempt_process_quiescence(meta, terminal_receipt=True)
    if proof.state != 'quiescent':
        raise DC.DispatchContractError('replacement-process-'+proof.state, proof.reason)
    if not _terminal_absent(fields, meta):
        raise DC.DispatchContractError('replacement-result-settlement-required')
    return {'state': proof.state, 'reason': proof.reason,
            'note': meta.get('note', ''), 'cleanup_receipt_digest': meta.get('cleanup_receipt_digest', ''),
            'cancellation_receipt_digest': meta.get('cancellation_receipt_digest', '')}


def _children_quiescent(rows, owner):
    owned = {owner}
    changed = True
    while changed:
        changed = False
        for aid, (_, meta) in rows.items():
            if meta.get('parent_attempt_id') in owned and aid not in owned:
                owned.add(aid); changed = True
    for aid in sorted(owned-{owner}):
        fields, meta = rows[aid]
        proof = DC.attempt_process_quiescence(meta, terminal_receipt=fields[1] in {'done','cancelled','killed'})
        if fields[1] in {'open','running'} or proof.state != 'quiescent':
            raise DC.DispatchContractError('replacement-owner-child-unsettled', aid)


def _route(jobs, aid, meta):
    from owner_route_binding import resolve_owner_route_lifecycle
    if meta.get('worker_type') == 'owner':
        binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=aid)
        if binding is None and not meta.get('route_file'):
            raise DC.DispatchContractError('replacement-owner-route-unproven')
        path = Path(binding.route_file if binding else meta['route_file'])
    else:
        path = Path(meta.get('route_file') or '')
    if not path.is_file():
        raise DC.DispatchContractError('replacement-route-unreadable')
    route = _read(path)
    DC._route_module().verify_route(route)
    # The closed route is immutable history, not an executable obligation.
    if path.with_suffix('.outcome.json').exists():
        raise DC.DispatchContractError('replacement-route-closed')
    return path, route


def _logical_key(route, meta):
    # Sealed continuation lineage carries its original route through generations.
    from route_lineage import verified_route_lineage
    lineage = verified_route_lineage(route)
    origin = lineage[-1]['route_id']
    node = '__owner__' if meta.get('worker_type') == 'owner' else meta.get('route_node')
    if not origin or not node:
        raise DC.DispatchContractError('replacement-logical-node-unproven')
    return {'root_route_id': origin, 'node': node}


def _record_path(jobs, family):
    if not re.fullmatch(r'[0-9a-f]{64}', family):
        raise DC.DispatchContractError('replacement-family-invalid')
    return _directory(jobs)/'claims'/(family+'.json')


def source_reservation(jobs, aid):
    """The first durable write consumes the budget even before row annotation."""
    index = _read(_directory(jobs)/'by-source'/(_id(aid)+'.json'))
    if index is not None and (index.get('schema') != SCHEMA
            or index.get('original_attempt_id') != aid
            or not re.fullmatch(r'[0-9a-f]{64}', index.get('family_id', ''))):
        raise DC.DispatchContractError('replacement-claim-invalid')
    return index


def _reserve_source(jobs, aid, family):
    _once(_directory(jobs)/'by-source'/(_id(aid)+'.json'),
          {'schema': SCHEMA, 'original_attempt_id': aid, 'family_id': family})


def _check_record(record, family, source=None):
    if (not record or record.get('schema') != SCHEMA or record.get('family_id') != family
            or _digest(record.get('logical_node')) != family
            or record.get('replacement_attempt_id') != 'att-'+hashlib.sha256(
                ('replacement:'+family).encode()).hexdigest()[:48]
            or record.get('ordinal') != 1):
        raise DC.DispatchContractError('replacement-lineage-unproven')
    if source and source.get('replacement_claim_digest') not in {None, _digest(record)}:
        raise DC.DispatchContractError('replacement-claim-drift')
    return record


def legacy_budget_exhausted(jobs, lines, source, *, route=None, include_family=False):
    """Read SD106 and SD157 consumption from the caller's jobs-lock snapshot.

    Do not move this check ahead of SD106's exact existing-recovery-id replay.
    SD157 permits its own by-source reservation to resume; SD106 calls with
    include_family=True and must not consume even that reservation again.
    No registry, route, or index is written and no second jobs lock is taken.
    """
    rows = _rows(lines)
    aid = _id(source.get('attempt_id'))
    references = [(fields, meta) for fields, meta in rows.values()
                  if meta.get('retry_attempt_id') == aid]
    if len(references) > 1:
        raise DC.DispatchContractError('replacement-legacy-budget-ambiguous')
    if references:
        _, prior = references[0]
        if (prior.get('retry_ordinal') != '1' or not prior.get('recovery_id')
                or DC._stable_recovery_attempt_id(prior['recovery_id']) != aid
                or any(prior.get(key) != source.get(key) for key in
                       ('route_node', 'parent_attempt_id', 'parent_sid',
                        'worker_type', 'dispatch_depth'))):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        # The current SD104/106 gap executes on the original source route.
        if (prior.get('route_id') and prior.get('route_hash')
                and (prior['route_id'], prior['route_hash'])
                == (source.get('route_id'), source.get('route_hash'))):
            return True

    reference_ids = {meta.get('attempt_id') for _, meta in references}
    candidates = []
    for fields, meta in rows.values():
        other = meta.get('attempt_id')
        same_node = (meta.get('worker_type') == 'owner'
                     if source.get('worker_type') == 'owner'
                     else meta.get('worker_type') != 'owner'
                     and meta.get('route_node') == source.get('route_node'))
        if not same_node and other not in reference_ids and meta.get('automatic_retry_of') != aid:
            continue
        # The original row can be unchanged after an index-first crash.
        # Delay unrelated index errors until the row's lineage is established.
        attention = reservation = None
        index_error = None
        try:
            attention = _read(Path(jobs).parent / 'recovery-attention' /
                              'by-source' / (_id(other) + '.json'))
            if attention is not None and (
                    attention.get('original_attempt_id') != other
                    or not attention.get('recovery_id')):
                raise DC.DispatchContractError('replacement-recovery-attention-invalid')
            reservation = source_reservation(jobs, other)
        except (DC.DispatchContractError, OSError, ValueError, TypeError) as exc:
            index_error = exc
        legacy = (meta.get('retry_ordinal') == '1'
                  or meta.get('recovery_exhausted') == '1'
                  or (meta.get('start_permitted') == '0' and meta.get('recovery_id'))
                  or attention is not None or bool(meta.get('automatic_retry_of')
                                                    and not meta.get('replacement_family_id')))
        family_hint = (reservation or {}).get('family_id')
        # SD157's own reservation is a replay, not a second consumption.
        # Its own SD106 exhaustion, however, is always a veto.
        family_consumes = bool(family_hint) and (other != aid or include_family)
        if legacy or family_consumes or index_error or other in reference_ids:
            candidates.append((fields, meta, bool(legacy), family_hint,
                               family_consumes, index_error))
    if not candidates and not references:
        # Preserve legacy route-less first-claim callers: no consumption
        # evidence means no reason to require a historical route here.
        return False

    from route_lineage import verified_route_lineage

    def historical_route(meta):
        path = meta.get('owner_route_file') or meta.get('route_file')
        if not path:
            return None
        value = _read(Path(path))
        if value is None:
            return None
        expected_id = meta.get('owner_route_id') or meta.get('route_id')
        expected_hash = meta.get('owner_route_hash') or meta.get('route_hash')
        if value.get('route_id') != expected_id or value.get('route_hash') != expected_hash:
            raise DC.DispatchContractError('replacement-legacy-budget-route-unproven')
        verified_route_lineage(value)
        return value

    if route is None:
        route = historical_route(source)
    if route is None:
        # An exact predecessor reference cannot be waved away as unrelated.
        if references or any(meta.get('attempt_id') == aid and
                             (legacy or family_consumes or error)
                             for _, meta, legacy, _, family_consumes, error in candidates):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        return False
    logical = _logical_key(route, source)
    family = _digest(logical)
    lineage = {r['route_id']: r for r in verified_route_lineage(route)}
    if include_family:
        record = _read(_record_path(jobs, family))
        if record is not None:
            _check_record(record, family)
            return True

    for _, prior, legacy, family_hint, family_consumes, index_error in candidates:
        other = prior.get('attempt_id')
        # A checked by-source index is already a durable exact family binding;
        # no family record or original-row annotation need exist yet.
        if family_consumes and family_hint == family:
            if index_error:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from index_error
            return True
        prior_id = prior.get('owner_route_id') or prior.get('route_id')
        prior_hash = prior.get('owner_route_hash') or prior.get('route_hash')
        previous = lineage.get(prior_id)
        direct = (previous is not None or other in reference_ids or other == aid
                  or prior.get('automatic_retry_of') == aid)
        if prior.get('automatic_retry_of') == aid and any(
                prior.get(key) != source.get(key) for key in
                ('route_id', 'route_hash', 'route_node', 'parent_attempt_id',
                 'parent_sid', 'worker_type', 'dispatch_depth')):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        try:
            if previous is not None:
                if previous['route_hash'] != prior_hash:
                    raise DC.DispatchContractError('replacement-legacy-budget-route-unproven')
            else:
                # Other streams share both a registry and common node names.
                # Their unavailable/corrupt history does not poison this stream.
                previous = historical_route(prior)
            if previous is None:
                if direct:
                    raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
                continue
            same_logical = _logical_key(previous, prior) == logical
        except (DC.DispatchContractError, OSError, ValueError, TypeError) as exc:
            if direct:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from exc
            continue
        if not same_logical:
            if other in reference_ids:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
            continue
        # From here on the prior row belongs to this exact logical family.
        if index_error:
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from index_error
        if family_consumes and family_hint != family:
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        if legacy or family_consumes:
            return True
    if references:
        raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
    return False


def _budget_exhausted(jobs, source, *, lines=None, route=None):
    aid = _id(source.get('attempt_id'))
    index = _read(Path(jobs).parent/'recovery-attention/by-source'/(aid+'.json'))
    if index is not None:
        if index.get('original_attempt_id') != aid or not index.get('recovery_id'):
            raise DC.DispatchContractError('replacement-recovery-attention-invalid')
        return True
    if (source.get('automatic_retry_of') or source.get('retry_ordinal') == '1'
            or source.get('recovery_exhausted') == '1' or source.get('start_permitted') == '0'):
        return True
    recovery = source.get('recovery_id')
    if recovery:
        path = Path(jobs).parent/'recovery-attention'/(hashlib.sha256(recovery.encode()).hexdigest()+'.json')
        record = _read(path)
        if record is not None:
            if record.get('recovery_id') != recovery or record.get('original_attempt_id') != source.get('attempt_id'):
                raise DC.DispatchContractError('replacement-recovery-attention-invalid')
            return True
    if lines is None:
        lines = Path(jobs).read_text().splitlines()
    return legacy_budget_exhausted(jobs, lines, source, route=route)


def _no_competing_successor(rows, aid, replacement=None):
    for other, (_, candidate) in rows.items():
        if other != replacement and (candidate.get('automatic_retry_of') == aid
                or candidate.get('prior_attempt_id') == aid):
            raise DC.DispatchContractError('automatic-replacement-exhausted', aid)


def claim(jobs: Path, aid: str) -> dict:
    """Lock-held death, children and terminal-fence checks precede one claim."""
    aid = _id(aid)
    with _locked(jobs) as lines:
        rows = _rows(lines)
        if aid not in rows:
            raise DC.DispatchContractError('replacement-source-missing', aid)
        fields, meta = rows[aid]
        DC.validate_attempt_metadata(meta)
        old_family = meta.get('replacement_family_id')
        if old_family:
            record = _check_record(_read(_record_path(jobs, old_family)), old_family, meta)
            if not record or aid not in {record['original_attempt_id'], record['replacement_attempt_id']}:
                raise DC.DispatchContractError('replacement-lineage-unproven')
            if aid == record['replacement_attempt_id']:
                raise DC.DispatchContractError('automatic-replacement-exhausted', record['logical_node']['node'])
            _no_competing_successor(rows, aid, record['replacement_attempt_id'])
            return record
        proof = death_proof(fields, meta)
        path, route = _route(jobs, aid, meta)
        logical = _logical_key(route, meta)
        family = _digest(logical)
        old = _read(_record_path(jobs, family))
        if old:
            _check_record(old, family)
            if old.get('original_attempt_id') != aid:
                raise DC.DispatchContractError('automatic-replacement-exhausted', logical['node'])
            _no_competing_successor(rows, aid, old['replacement_attempt_id'])
            _reserve_source(jobs, aid, family)
            _bind_source(jobs, lines, aid, old)
            return old
        if _budget_exhausted(jobs, meta, lines=lines, route=route):
            raise DC.DispatchContractError('automatic-replacement-exhausted', logical['node'])
        _no_competing_successor(rows, aid)
        owner = aid if meta.get('worker_type') == 'owner' else meta.get('parent_attempt_id') or aid
        _source_fences(jobs, meta, route['route_id'], aid)
        if meta.get('worker_type') == 'owner':
            _children_quiescent(rows, aid)
        replay = launch_input(jobs, aid, meta)
        replacement = 'att-'+hashlib.sha256(('replacement:'+family).encode()).hexdigest()[:48]
        record = {'schema': SCHEMA, 'family_id': family, 'logical_node': logical,
                  'original_attempt_id': aid, 'replacement_attempt_id': replacement, 'ordinal': 1,
                  'route_file': str(path), 'route_id': route['route_id'], 'route_hash': route['route_hash'],
                  'input_digest': _digest(replay), 'proof': proof, 'proof_digest': _digest(proof),
                  'reuse': _reuse_snapshot(jobs, route, lines)}
        # Index first: every retry admission sees the consumed budget after a crash.
        _reserve_source(jobs, aid, family)
        _once(_record_path(jobs, family), record)
        _bind_source(jobs, lines, aid, record)
        return record


def _bind_source(jobs, lines, aid, record):
    values = {'replacement_family_id': record['family_id'],
              'replacement_attempt_id': record['replacement_attempt_id'], 'replacement_ordinal': '1',
              'replacement_claim_digest': _digest(record)}
    for i,line in enumerate(lines):
        fields=line.split('\t')
        if len(fields)!=6 or not DC.row_has_attempt(fields[5],aid):
            continue
        meta=DC.parse_registry_metadata(fields[5])
        for key,value in values.items():
            if key in meta and meta[key]!=value:
                raise DC.DispatchContractError('replacement-lineage-conflict')
        fields[5]+=''.join(','+key+'='+value for key,value in values.items() if key not in meta)
        lines[i]='\t'.join(fields)
        DC._atomic_registry_replace(Path(jobs),lines)
        return
    raise DC.DispatchContractError('replacement-source-missing')


def _reuse_snapshot(jobs, route, lines):
    """Pin every current completion, including non-prefix successful siblings."""
    module = DC._route_module()
    directory = module.completion_dir(route['route_id'], jobs=Path(jobs))
    completed = []
    for node in route.get('nodes', []):
        path = directory/(str(node['id'])+'.json')
        if not path.exists():
            continue
        marker = _read(path)
        if not DC.completion_marker_is_current(route, node, path, marker):
            raise DC.DispatchContractError('replacement-completion-unproven', str(node['id']))
        ready = DC.completion_attempt_readiness(route, node, marker, Path(jobs), registry_lines=lines)
        if ready.state != 'ready':
            raise DC.DispatchContractError('replacement-completion-unsettled', str(node['id']))
        completed.append({'node': node['id'], 'marker_digest': _digest(marker),
                          'attempt_id': marker.get('attempt_id', '')})
    root = Path(route['artifact_root'])
    from artifact_producer import route_cycle_for, cycle_route_admission
    cycle = route_cycle_for(root, route)
    if not cycle or not cycle_route_admission(root, cycle, route).allow:
        raise DC.DispatchContractError('replacement-producer-not-open')
    return {'completed': completed, 'cycle_id': cycle['cycle_id'],
            'producer_id': cycle.get('producer_id', ''), 'gates': _gate_snapshot(jobs, route)}


def _gate_snapshot(jobs, route):
    import workflow_state as WS
    ledger = WS.WorkflowLedger(route['route_id'], route['route_hash'], jobs=jobs)
    try:
        raw = ledger.journal_path.read_text()
    except FileNotFoundError:
        raw = ''
    try:
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except ValueError as exc:
        raise DC.DispatchContractError('replacement-workflow-journal-unreadable') from exc
    if any(not isinstance(e,dict) or e.get('route_id') != route['route_id']
           or e.get('route_hash') != route['route_hash'] for e in entries):
        raise DC.DispatchContractError('replacement-workflow-journal-mismatch')
    if ledger._rebuild(entries)['workflow_state'] in {'CANCELLED','COMPLETE'}:
        raise DC.DispatchContractError('replacement-workflow-not-open')
    names = {str(b['gate']) for b in route.get('human_gate_bindings',[]) if b.get('gate')}
    names.update(str((e.get('evidence') or {})['gate']) for e in entries
                 if e.get('workflow_state') == 'BLOCKED_HUMAN_GATE' and (e.get('evidence') or {}).get('gate'))
    gates = [WS.human_gate_resolution(entries,name) for name in sorted(names)]
    if any(g['status'] in {'revise','stop'} for g in gates):
        raise DC.DispatchContractError('replacement-human-gate-changed-scope')
    return gates


def _reuse_preserved(previous, current):
    if any(previous.get(key) != current.get(key) for key in ('cycle_id','producer_id')):
        return False
    now = {r['node']:r for r in current.get('completed',[])}
    if any(now.get(r['node']) != r for r in previous.get('completed',[])):
        return False
    gates = {r['gate']:r for r in current.get('gates',[])}
    for old in previous.get('gates',[]):
        new = gates.get(old['gate'])
        if new is None:
            return False
        if old['status'] == 'not-raised':
            continue
        if old['status'] == 'blocked' and new['status'] == 'proceed':
            # The same raise acquired its answer while launch was interrupted.
            if any(old.get(k) != new.get(k) for k in
                   ('epoch','raised_at','artifact','artifact_sha256','interview','questions','release_authority')):
                return False
        elif old != new:
            return False
    return True


def _source_fences(jobs, source, route_id, aid):
    owner = aid if source.get('worker_type') == 'owner' else source.get('parent_attempt_id') or aid
    for rid in {route_id, source.get('owner_route_id'), source.get('route_id')} - {None, ''}:
        DC.ensure_terminal_claim_absent(jobs, rid, owner)


def validate_claim_source(jobs, lines, record):
    """Shared lock-held source proof for admission and partial-batch reservation."""
    family = record.get('family_id', '')
    _check_record(record, family)
    canonical = _read(_record_path(jobs, family))
    if canonical != record:
        raise DC.DispatchContractError('replacement-claim-drift')
    rows = _rows(lines); prior = record['original_attempt_id']
    if prior not in rows:
        raise DC.DispatchContractError('replacement-source-missing')
    fields, source = rows[prior]
    if (source.get('replacement_claim_digest') != _digest(record)
            or source.get('replacement_family_id') != family
            or source.get('replacement_attempt_id') != record['replacement_attempt_id']
            or (source_reservation(jobs, prior) or {}).get('family_id') != family):
        raise DC.DispatchContractError('replacement-claim-pending')
    _no_competing_successor(rows, prior, record['replacement_attempt_id'])
    if _budget_exhausted(jobs, source, lines=lines):
        raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
    death_proof(fields, source)
    _source_fences(jobs, source, record['route_id'], prior)
    if source.get('worker_type') == 'owner':
        _children_quiescent(rows, prior)
    replay = launch_input(jobs, prior, source)
    if _digest(replay) != record['input_digest']:
        raise DC.DispatchContractError('replacement-input-drift')
    path, route = _route(jobs, prior, source)
    if str(path) != record['route_file'] or route.get('route_hash') != record['route_hash']:
        raise DC.DispatchContractError('replacement-route-drift')
    current_reuse = _reuse_snapshot(jobs, route, lines)
    if not _reuse_preserved(record['reuse'], current_reuse):
        raise DC.DispatchContractError('replacement-reuse-evidence-drift')
    if source.get('worker_type') == 'owner' and any(g['status'] == 'blocked' for g in current_reuse.get('gates',[])):
        raise DC.DispatchContractError('replacement-human-gate-pending')
    return fields, source, replay


def admission(jobs, lines, metadata):
    """Called with the jobs lock at both registration and actual spawn."""
    prior = metadata.get('automatic_retry_of') or metadata.get('prior_attempt_id')
    if not prior:
        return None
    rows = _rows(lines)
    if prior not in rows:
        raise DC.DispatchContractError('retry-predecessor-missing', prior)
    source_fields, source = rows[prior]
    reservation = source_reservation(jobs, prior)
    family = source.get('replacement_family_id') or (reservation or {}).get('family_id')
    if reservation and not source.get('replacement_claim_digest'):
        raise DC.DispatchContractError('replacement-claim-pending', prior)
    if not family:
        if _budget_exhausted(jobs, source, lines=lines):
            raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
        return None
    record = _check_record(_read(_record_path(jobs, family)), family, source)
    if (not record or record.get('original_attempt_id') != prior
            or record.get('replacement_attempt_id') != metadata.get('attempt_id')):
        raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
    route_key = 'owner_route_id' if source.get('owner_route_id') else 'route_id'
    hash_key = 'owner_route_hash' if source.get('owner_route_id') else 'route_hash'
    if (metadata.get(route_key) != record['route_id'] or metadata.get(hash_key) != record['route_hash']
            or any(metadata.get(k) != source.get(k) for k in
                   ('route_node', 'parent_attempt_id', 'parent_sid', 'worker_type', 'dispatch_depth',
                    'session_chain_id','subsession_id','subsession_index','subsession_count',
                    'subsession_mode','stage_authority','fixed_inputs_sha256',
                    'narrow_verify_sha256','phase_brief_sha256'))):
        raise DC.DispatchContractError('replacement-launch-binding-mismatch', prior)
    if source.get('session_chain_id'):
        from dispatch_replacement_subsession import SCOPE_KEYS
        if any(metadata.get(key) != source.get(key) for key in SCOPE_KEYS):
            raise DC.DispatchContractError('replacement-subsession-scope-mismatch')
    _, source, replay = validate_claim_source(jobs, lines, record)
    candidate = launch_input(jobs, metadata['attempt_id'], metadata)
    if source.get('review_input_digest') or metadata.get('review_input_digest'):
        from review_input import read_binding
        original_input = read_binding(jobs, source, verify_current=True)
        replacement_input = read_binding(jobs, metadata, verify_current=True)
        if (any(replacement_input.get(k) != original_input.get(k) for k in ('path', 'sha256', 'producer'))
                or replacement_input.get('source') != {
                    'attempt_id': source['attempt_id'], 'binding_digest': source['review_input_digest']}):
            raise DC.DispatchContractError('reviewed-evidence-replacement-mismatch')
    expected_task = _replacement_task(record, source, replay)
    for key in ('harness', 'jobs', 'worktree', 'launch_home', 'resolved', 'applied_permissions'):
        if candidate.get(key) != replay.get(key):
            raise DC.DispatchContractError('replacement-input-tuple-mismatch', key)
    if candidate.get('task') != expected_task:
        raise DC.DispatchContractError('replacement-task-mismatch')
    expected_argv = _replacement_argv(record, source, replay)
    if candidate.get('argv') != expected_argv:
        raise DC.DispatchContractError('replacement-argv-mismatch')
    if Path(record['route_file']).with_suffix('.outcome.json').exists():
        raise DC.DispatchContractError('replacement-route-closed')
    route = _read(Path(record['route_file']))
    if not route or route.get('route_hash') != record['route_hash']:
        raise DC.DispatchContractError('replacement-route-drift')
    return record


def replacement_row(jobs, lines, row):
    row = row.rstrip('\n')
    fields = row.split('\t'); metadata = DC.parse_registry_metadata(fields[5])
    record = admission(jobs, lines, metadata)
    if not record:
        return row, False
    values = {'replacement_family_id': record['family_id'],
              'replacement_original_attempt_id': record['original_attempt_id'], 'replacement_ordinal': '1',
              'replacement_claim_digest': _digest(record)}
    for key, value in values.items():
        if key in metadata and metadata[key] != value:
            raise DC.DispatchContractError('replacement-lineage-conflict')
        if key not in metadata:
            fields[5] += ','+key+'='+value
    return '\t'.join(fields), True


def _replace_options(argv, replacements, remove=()):
    """Parse long options without ever passing through a shell."""
    result = []; i = 0
    while i < len(argv):
        arg = argv[i]; key = arg.split('=',1)[0]
        if key in replacements or key in remove:
            if '=' not in arg and i+1 < len(argv) and not argv[i+1].startswith('--'):
                i += 1
        else:
            result.append(arg)
        i += 1
    for key, value in replacements.items():
        result.append(key)
        if value is not None:
            result.append(str(value))
    return result


def _canonical_argv(argv):
    """Order-independent option groups; repeated values retain their own order."""
    groups=[]; i=0
    while i<len(argv):
        arg=argv[i]
        if not arg.startswith('--'):
            raise DC.DispatchContractError('replacement-argv-invalid')
        if '=' in arg:
            key,value=arg.split('=',1); group=[key,value]
        else:
            group=[arg]
            if i+1<len(argv) and not argv[i+1].startswith('--'):
                group.append(argv[i+1]); i+=1
        groups.append(group);i+=1
    return [value for group in sorted(groups,key=lambda g:g[0]) for value in group]


def _authorized(jobs, rows, meta):
    if meta.get('dispatch_depth') == '2':
        parent = meta.get('parent_attempt_id')
        if not parent or os.environ.get('AGENT_DISPATCH_ATTEMPT_ID') != parent or parent not in rows:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        fields, parent_meta = rows[parent]
        if fields[1] not in {'open','running'} or not DC._parent_liveness_evidence(Path(jobs), parent_meta)[0]:
            raise DC.DispatchContractError('replacement-parent-not-live')
    else:
        from work_start import _current_parent_session_id
        if not meta.get('parent_sid') or _current_parent_session_id() != meta['parent_sid']:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')


def _replacement_task(record, source, replay):
    task = replay['task']
    if source.get('worker_type') == 'owner':
        original_route = source.get('owner_route_id') or source.get('route_id')
        if original_route != record['route_id']:
            route = _read(Path(record['route_file']))
            request = route.get('work_request') or {}
            if not isinstance(request.get('text'), str) or not request['text'].strip():
                raise DC.DispatchContractError('replacement-current-route-input-unproven')
            task = request['text']
    return task


def _replacement_argv(record, source, replay):
    options = {}
    if source.get('worker_type') != 'owner' or source.get('route_file'):
        options.update({'--route-file': record['route_file'], '--route-id': record['route_id'],
                        '--route-hash': record['route_hash']})
    return _canonical_argv(_replace_options(replay['argv'], options, remove=INPUT_OPTIONS))


def _command(jobs, record, source, replay):
    root = Path(replay['launch_home']).resolve()
    if root != ROOT.resolve() or replay['harness'] not in {'codex','claude','opencode'}:
        raise DC.DispatchContractError('replacement-runtime-mismatch')
    task = _replacement_task(record, source, replay)
    prompt = _directory(jobs)/'tasks'/(record['replacement_attempt_id']+'.txt')
    prompt.parent.mkdir(parents=True, exist_ok=True)
    raw = task.encode()
    if prompt.is_symlink() or (not _write_once(prompt.parent,prompt,raw) and prompt.read_bytes()!=raw):
        raise DC.DispatchContractError('replacement-task-conflict')
    options = {'--start': None, '--attempt-id': record['replacement_attempt_id'],
               '--automatic-retry-of': record['original_attempt_id'], '--prompt-file': prompt,
               '--jobs': Path(jobs).resolve(), '--worktree': replay['worktree']}
    argv = _replace_options(_replacement_argv(record, source, replay), options)
    return [sys.executable,str(root/f'adapters/{replay["harness"]}/bin/dispatch-headless.py'),*argv]


def advance(jobs, aid, *, run=subprocess.run, authority_check=None):
    """A bound runtime checkpoint, never a read-only observer, calls this."""
    rows = _rows(Path(jobs).read_text().splitlines())
    if aid not in rows:
        return {'state':'unavailable','reason':'replacement-source-missing'}
    fields, source = rows[aid]
    if (source.get('replacement_original_attempt_id') and fields[1] in {'open','running'}
            and source.get('launch_claimed') != '1'):
        return advance(jobs, source['replacement_original_attempt_id'], run=run,
                       authority_check=authority_check)
    if (source.get('replacement_original_attempt_id') and fields[1] not in {'open','running'}
            and not DC.verdict_pass(source)):
        return exhausted_attention(jobs, aid, source)
    # Avoid side effects or errors on ordinary success/live observations.
    if (source.get('note') not in DEATH_NOTES
            and source.get('classifier_source') != DC.AUTOMATIC_RECEIPTLESS_CLASSIFIER):
        return {'state':'not-applicable'}
    try:
        if authority_check is None:
            _authorized(jobs, rows, source)
        elif authority_check(jobs, aid, source) is not True:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        record = claim(Path(jobs), aid)
        rows = _rows(Path(jobs).read_text().splitlines())
        source = rows[aid][1]
        replacement = record['replacement_attempt_id']
        if replacement in rows:
            replacement_fields, replacement_meta = rows[replacement]
            if replacement_fields[1] not in {'open','running'}:
                if not DC.verdict_pass(replacement_meta):
                    return exhausted_attention(jobs, replacement, replacement_meta)
                return {'state':'reused','attempt_id':replacement,'record':record}
            if replacement_meta.get('launch_claimed') == '1':
                return {'state':'running','attempt_id':replacement,'record':record}
        replay = launch_input(jobs,aid,source)
        from dispatch_replacement_batch import command as batch_command
        command = batch_command(jobs, record, source, replay) or _command(jobs,record,source,replay)
        env = dict(os.environ)
        # The old launcher reservation/owner tuple is not a grant for its successor.
        for key in list(env):
            if key.startswith('AGENT_OWNER_ROUTE_') or key in {
                    DC.GOVERNOR_RESERVATION_ENV}:
                env.pop(key,None)
        if source.get('worker_type') == 'owner' and not source.get('route_file'):
            env.update(AGENT_OWNER_ROUTE_FILE=record['route_file'],
                       AGENT_OWNER_ROUTE_ID=record['route_id'],AGENT_OWNER_ROUTE_HASH=record['route_hash'])
        completed = run(command,env=env,text=True,capture_output=True,check=False,timeout=120)
        current = _rows(Path(jobs).read_text().splitlines()).get(replacement)
        if current and current[1].get('launch_claimed') == '1':
            return {'state':'running','attempt_id':replacement,'record':record}
        return {'state':'needs-attention','reason':'replacement-launch-pending',
                'attempt_id':replacement,'record':record,'launcher_exit':completed.returncode,
                'source_attempt_id':aid,'node':source.get('route_node') or '__owner__'}
    except (DC.DispatchContractError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = getattr(exc,'reason','replacement-observation-unavailable')
        if reason == 'automatic-replacement-exhausted':
            family_record = None
            if not _budget_exhausted(jobs, source):
                _, route = _route(jobs, aid, source)
                family = _digest(_logical_key(route, source))
                family_record = _check_record(_read(_record_path(jobs,family)),family)
            return exhausted_attention(jobs, aid, source, family_record=family_record)
        return {'state':'needs-attention','reason':reason,'source_attempt_id':aid,
                'node': source.get('route_node') or '__owner__'}


def effective_attempts(jobs, attempts):
    """Read a verified one-edge mapping; failed originals remain in the ledger."""
    rows = _rows(Path(jobs).read_text().splitlines())
    effective = set(attempts); mapping = []
    for aid in sorted(attempts):
        if aid not in rows:
            continue
        _, source = rows[aid]
        family = source.get('replacement_family_id')
        replacement = source.get('replacement_attempt_id')
        if not family or not replacement:
            continue
        record = _check_record(_read(_record_path(jobs,family)), family, source)
        if (not record or record.get('original_attempt_id') != aid
                or record.get('replacement_attempt_id') != replacement
                or record.get('family_id') != family):
            raise DC.DispatchContractError('replacement-lineage-unproven',aid)
        if replacement not in rows:
            continue  # A claim is not a launch receipt.
        _, target = rows[replacement]
        if (target.get('replacement_claim_digest') != _digest(record)
                or target.get('replacement_family_id') != family or target.get('automatic_retry_of') != aid
                or target.get('replacement_original_attempt_id') != aid
                or any(target.get(k) != source.get(k) for k in ('parent_sid','parent_attempt_id','worker_type','dispatch_depth'))):
            raise DC.DispatchContractError('replacement-lineage-unproven',replacement)
        effective.discard(aid); effective.add(replacement)
        mapping.append({'original_attempt_id':aid,'replacement_attempt_id':replacement,
                        'family_id':family,'claim_digest':_digest(record)})
    return effective,mapping


def advance_batch(jobs, attempts, *, authority_check=None, run=subprocess.run):
    """Runtime-only checkpoint; no model turn or new polling service."""
    rows = _rows(Path(jobs).read_text().splitlines())
    attention=[]
    for aid in sorted(attempts):
        pair=rows.get(aid)
        if pair is None:
            continue
        if pair[0][1] in {'open','running'}:
            if pair[1].get('replacement_original_attempt_id') and pair[1].get('launch_claimed') != '1':
                step=advance(jobs,aid,authority_check=authority_check,run=run)
                if step.get('state')=='needs-attention': attention.append(step)
            continue
        meta=pair[1]
        if meta.get('replacement_original_attempt_id'):
            if not DC.verdict_pass(meta):
                attention.append(exhausted_attention(jobs, aid, meta))
            continue
        step=advance(jobs,aid,authority_check=authority_check,run=run)
        if step.get('state')=='needs-attention':
            attention.append(step)
    effective,mapping=effective_attempts(jobs,attempts)
    return effective,mapping,attention


def adopt_receipt(jobs, original_attempts, receipt):
    """A supervisor accepts only the registry-bound mapping, never a free ID."""
    mapping=receipt.get('replacement_lineage') or []
    if not mapping:
        return set(original_attempts),set()
    effective,expected=effective_attempts(jobs,set(original_attempts))
    if mapping!=expected:
        raise DC.DispatchContractError('replacement-receipt-lineage-mismatch')
    children={child.get('attempt_id') for child in receipt.get('children',[])}
    if children!=effective and not (receipt.get('state') == 'watch-expired' and not children):
        raise DC.DispatchContractError('replacement-receipt-attempt-mismatch')
    return effective,{row['original_attempt_id'] for row in mapping}


def exhausted_attention(jobs, aid, meta, *, family_record=None):
    result={'source_attempt_id':aid,'state':'needs-attention',
            'reason':'automatic-replacement-exhausted',
            'node':meta.get('route_node') or '__owner__',
            'failure':meta.get('note') or meta.get('failure_class') or 'terminal-failure'}
    family=meta.get('replacement_family_id') or (family_record or {}).get('family_id')
    if family:
        record=_check_record(_read(_record_path(jobs,family)),family)
        if (record['logical_node']['node'] != (meta.get('route_node') or '__owner__')
                and meta.get('worker_type') != 'owner'):
            raise DC.DispatchContractError('replacement-lineage-unproven')
        if record['replacement_attempt_id']==aid:
            _once(_directory(jobs)/'attention'/(family+'.json'), result)
        proof={'family_id':family,'claim_digest':_digest(record)}
    elif _budget_exhausted(jobs,meta):
        proof={'legacy_budget_exhausted':True}
    else:
        raise DC.DispatchContractError('replacement-exhaustion-unproven')
    _once(_directory(jobs)/'attention/by-source'/(_id(aid)+'.json'),
          {'attention':result,'proof':proof})
    return result


def validate_attention(jobs, attention, *, allowed_attempts=None):
    """Validate diagnostic association; this never grants execution authority."""
    if not isinstance(attention, list):
        raise DC.DispatchContractError('replacement-attention-invalid')
    rows = _rows(Path(jobs).read_text().splitlines())
    for item in attention:
        if not isinstance(item, dict):
            raise DC.DispatchContractError('replacement-attention-invalid')
        aid = item.get('source_attempt_id')
        if aid not in rows:
            raise DC.DispatchContractError('replacement-attention-source-missing')
        if allowed_attempts is not None and aid not in allowed_attempts:
            raise DC.DispatchContractError('replacement-attention-scope-mismatch')
        fields, meta = rows[aid]
        reason = item.get('reason')
        if (fields[1] not in {'done','cancelled','killed'} or DC.verdict_pass(meta)
                or item.get('state') != 'needs-attention'
                or item.get('node') != (meta.get('route_node') or '__owner__')
                or not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:-]{0,159}', reason)):
            raise DC.DispatchContractError('replacement-attention-invalid')
        if reason == 'automatic-replacement-exhausted':
            saved = _read(_directory(jobs)/'attention/by-source'/(_id(aid)+'.json'))
            if not saved or saved.get('attention') != item:
                raise DC.DispatchContractError('replacement-attention-drift')
            proof = saved.get('proof') or {}
            family = proof.get('family_id')
            if family:
                record = _check_record(_read(_record_path(jobs,family)),family)
                if proof.get('claim_digest') != _digest(record):
                    raise DC.DispatchContractError('replacement-attention-lineage-invalid')
            elif not proof.get('legacy_budget_exhausted') or not _budget_exhausted(jobs,meta):
                raise DC.DispatchContractError('replacement-attention-lineage-invalid')
    return [{key: (str(item[key])[:240] if key == 'failure' else item[key])
             for key in ('source_attempt_id','state','reason','node','failure') if key in item}
            for item in attention]


def recovery_instructions(args):
    """Fresh rendering adds resume context without modifying the sealed raw task."""
    prior = getattr(args, 'automatic_retry_of', None)
    if not prior or getattr(args, 'worker_type', '') != 'owner':
        return ''
    jobs = Path(args.jobs_path)
    index = source_reservation(jobs, prior)
    if not index:
        return ''
    record = _check_record(_read(_record_path(jobs, index['family_id'])),index['family_id'])
    if record['replacement_attempt_id'] != args.attempt_id:
        raise DC.DispatchContractError('replacement-instructions-binding-mismatch')
    completed = ', '.join(str(row['node']) for row in record['reuse']['completed']) or '(none)'
    return ('\n\n## Verified recovery context\n'
            f'You replace exact-dead attempt {prior} once, on the existing route {record["route_id"]}.\n'
            f'Reuse the existing cycle {record["reuse"]["cycle_id"]} and completion evidence for: {completed}.\n'
            'Continue only unfinished work. Do not rerun completed nodes, successful siblings, or completed prefixes. '
            'Keep existing human answers and gate releases; do not ask the same scope again. '
            'Preserve the original failure and report any second failure as needs-attention.\n')
