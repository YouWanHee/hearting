#!/usr/bin/env python3
"""봉인 manifest의 부재 regular file exact bytes maintenance.

입력 digest는 사람 승인·cycle 재개 권한이 아니다. 원본 metadata는 불변이며
새 OPEN 작업 cycle에 실제 관측을 기록한다. 비협조 writer의 전역 원자성은
보장하지 않는다.
"""
from __future__ import annotations
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tarfile
import time
import artifact_identity as I
import artifact_manifest as M
import artifact_producer as P

MAX_BYTES = 16 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
INPUT_ROLES = {'root-identity', 'archive-seal', 'retired-manifest',
               'retirement-journal', 'record', 'cycle-binding', 'manifest'}
RECORD_KEYS = 'schema_version kind request_sha256 work_cycle_id producer_id sequence previous_sha256 payload'


class RestoreError(Exception):
    def __init__(self, reason, detail='', code=65):
        self.reason, self.detail, self.code = reason, str(detail), code
        super().__init__(reason + (':' + self.detail if detail else ''))


def require(value, reason, detail='', code=65):
    if not value:
        raise RestoreError(reason, detail, code)


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def decode(data):
    def pairs(items):
        result = {}
        for k, v in items:
            require(k not in result, 'input-schema', 'duplicate-key:' + k, 64)
            result[k] = v
        return result
    def constant(value):
        raise RestoreError('input-schema', value, 64)
    try:
        require(not data.startswith(b'\xef\xbb\xbf'), 'input-schema', 'BOM', 64)
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise RestoreError('input-schema', str(exc), 64) from exc


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected.split()),
            'input-schema', 'closed-object:' + expected, 64)


def integer(value, maximum=None):
    require(type(value) is int and value >= 0 and
            (maximum is None or value <= maximum), 'input-schema', 'integer', 64)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value),
            'input-schema', 'sha256', 64)
    return value


def normalized_sha(value):
    return sha(value if isinstance(value, str) and value.startswith('sha256:')
               else 'sha256:' + str(value))


def identity(value, kind):
    require(I.is_well_formed(value, kind), 'input-schema', kind, 64)


def relative(value):
    require(isinstance(value, str) and value and not value.startswith('/') and
            '\\' not in value and not re.search(r'[\x00-\x1f\x7f]', value) and
            all(p not in ('', '.', '..') for p in value.split('/')), 'path-unsafe', repr(value))
    return value


def absolute(value):
    require(isinstance(value, str) and value.startswith('/') and value != '/',
            'path-unsafe', repr(value))
    relative(value[1:])
    return value


def devino(st):
    return st.st_dev, st.st_ino


def primitives():
    require(all(f in os.supports_dir_fd for f in
                (os.open, os.stat, os.mkdir, os.link, os.unlink)) and
            os.link in os.supports_follow_symlinks and
            all(hasattr(os, flag) for flag in
                ('O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK')),
            'primitive-unsupported', code=69)


def _checkpoint(phase, context):
    """직접 test subprocess만 교체하는 seam. CLI override는 없다."""


def _publish_link(source, target, source_fd, target_fd):
    os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)


class Tree:
    """절대 경로의 retained dirfd와 이름→inode 연결."""
    def __init__(self):
        self.dirs = {'/': os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)}

    def close(self):
        for fd in reversed(list(self.dirs.values())):
            os.close(fd)
        self.dirs.clear()

    def directory(self, path, create=None):
        if path == '/':
            return self.dirs['/']
        absolute(path)
        if path in self.dirs:
            self.verify_directory(path)
            return self.dirs[path]
        parent, name = path.rsplit('/', 1)
        parent = parent or '/'
        fd = self.directory(parent, create)
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            if create is None or path not in create:
                raise
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            os.fsync(fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
        except OSError as exc:
            raise RestoreError('path-unsafe', path) from exc
        self.dirs[path] = child
        self.verify_directory(path)
        return child

    def verify_directory(self, path):
        if path == '/':
            return
        parent, name = path.rsplit('/', 1)
        parent = parent or '/'
        self.verify_directory(parent)
        try:
            current = os.stat(name, dir_fd=self.dirs[parent], follow_symlinks=False)
        except OSError as exc:
            raise RestoreError('path-unsafe', path) from exc
        require(stat.S_ISDIR(current.st_mode) and
                devino(current) == devino(os.fstat(self.dirs[path])), 'path-unsafe', path)

    def verify(self):
        for path in self.dirs:
            self.verify_directory(path)

    def open_file(self, path, flags=None, mode=0o600):
        absolute(path)
        parent, name = path.rsplit('/', 1)
        fd = self.directory(parent or '/')
        try:
            result = os.open(name, (os.O_RDONLY if flags is None else flags) |
                             os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, mode, dir_fd=fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RestoreError('path-unsafe', path) from exc
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise RestoreError('target-conflict', path)
        return result

    def named(self, path, fd):
        parent, name = path.rsplit('/', 1)
        pfd = self.directory(parent or '/')
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        require(stat.S_ISREG(st.st_mode) and devino(st) == devino(os.fstat(fd)),
                'input-drift', path)

    def missing_parents(self, path):
        parts = path.rsplit('/', 1)[0][1:].split('/')
        missing = []
        for i in range(1, len(parts) + 1):
            prefix = '/' + '/'.join(parts[:i])
            if missing:
                missing.append(prefix)
                continue
            try:
                self.directory(prefix)
            except FileNotFoundError:
                missing.append(prefix)
        return missing


def fd_content(fd, *, limit=None):
    os.lseek(fd, 0, os.SEEK_SET)
    h, chunks, count = hashlib.sha256(), [], 0
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            break
        count += len(data)
        require(limit is None or count <= limit, 'input-schema', 'oversize', 64)
        h.update(data)
        if limit is not None:
            chunks.append(data)
    return 'sha256:' + h.hexdigest(), count, b''.join(chunks)


class Snapshot:
    def __init__(self):
        self.tree, self.sources = Tree(), {}

    def close(self):
        for fd, _ in self.sources.values():
            os.close(fd)
        self.tree.close()

    def source(self, role, path, *, json_data=True):
        absolute(path)
        require(path not in self.sources, 'input-schema', 'duplicate-input:' + path, 64)
        fd = self.tree.open_file(path)
        try:
            st = os.fstat(fd)
            require(st.st_nlink == 1, 'input-drift', 'source-hardlink:' + path)
            h, size, data = fd_content(fd, limit=MAX_JSON if json_data else None)
            row = dict(role=role, path=path, sha256=h, size=size, dev=st.st_dev, ino=st.st_ino)
            self.tree.named(path, fd)
            require(size == st.st_size, 'input-drift', path)
            self.sources[path] = (fd, row)
            return data, row
        except BaseException:
            os.close(fd)
            raise

    def verify(self):
        self.tree.verify()
        for path, (fd, row) in self.sources.items():
            self.tree.named(path, fd)
            st = os.fstat(fd)
            h, size, _ = fd_content(fd)
            require(st.st_nlink == 1 and st.st_size == row['size'] == size and
                    h == row['sha256'], 'input-drift', path)
            self.tree.named(path, fd)


def selection_schema(value):
    keys(value, 'schema_version kind root archive selections')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and
            value['kind'] == 'sealed-bytes-selection', 'input-schema', 'selection-v1', 64)
    absolute(value['root'])
    keys(value['archive'], 'path sha256 seal_path seal_sha256')
    for k in ('path', 'seal_path'):
        absolute(value['archive'][k])
    for k in ('sha256', 'seal_sha256'):
        sha(value['archive'][k])
    require(isinstance(value['selections'], list) and 1 <= len(value['selections']) <= 4,
            'input-schema', 'selection-count', 64)
    seen = set()
    for row in value['selections']:
        keys(row, 'cycle_id artifact_id artifact_revision_id member')
        for key, kind in (('cycle_id', 'cycle'), ('artifact_id', 'artifact'),
                          ('artifact_revision_id', 'artifact_revision')):
            identity(row[key], kind)
            require(row[key] not in seen or key == 'cycle_id', 'input-schema', 'duplicate-id', 64)
            seen.add(row[key])
        relative(row['member'])
        require(row['member'] not in seen, 'input-schema', 'duplicate-member', 64)
        seen.add(row['member'])


def request_schema(value):
    keys(value, 'schema_version kind root inputs cycles archive files parents total_bytes')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and
            value['kind'] == 'sealed-bytes-restore-request', 'input-schema', 'request-v1', 64)
    keys(value['root'], 'path artifact_root_id repository_id dev ino')
    absolute(value['root']['path'])
    identity(value['root']['artifact_root_id'], 'artifact_root')
    identity(value['root']['repository_id'], 'repository')
    for k in ('dev', 'ino'):
        integer(value['root'][k])
    keys(value['archive'], 'path sha256 size dev ino')
    absolute(value['archive']['path']); sha(value['archive']['sha256'])
    for k in ('size', 'dev', 'ino'):
        integer(value['archive'][k])
    require(isinstance(value['inputs'], list), 'input-schema', 'inputs', 64)
    for row in value['inputs']:
        keys(row, 'role path sha256 size dev ino')
        require(row['role'] in INPUT_ROLES, 'input-schema', 'input-role', 64)
        absolute(row['path']); sha(row['sha256'])
        for k in ('size', 'dev', 'ino'):
            integer(row[k])
    require(isinstance(value['cycles'], list) and 1 <= len(value['cycles']) <= 4,
            'input-schema', 'cycles', 64)
    for row in value['cycles']:
        keys(row, 'cycle_id campaign_id record_path binding_path manifest_path manifest_digest')
        identity(row['cycle_id'], 'cycle'); identity(row['campaign_id'], 'campaign')
        sha(row['manifest_digest'])
        for k in ('record_path', 'binding_path', 'manifest_path'):
            absolute(row[k])
    require(isinstance(value['files'], list) and 1 <= len(value['files']) <= 4,
            'input-schema', 'files', 64)
    for row in value['files']:
        keys(row, 'cycle_id artifact_id artifact_revision_id target_locator member sha256 size mode')
        relative(row['target_locator']); relative(row['member']); sha(row['sha256'])
        integer(row['size'], MAX_BYTES); integer(row['mode'], 0o777)
    integer(value['total_bytes'], MAX_BYTES)
    require(value['total_bytes'] == sum(r['size'] for r in value['files']),
            'input-schema', 'total-bytes', 64)
    require(isinstance(value['parents'], list) and len(value['parents']) <= 3 and
            all(isinstance(p, str) for p in value['parents']) and
            value['parents'] == sorted(set(value['parents'])), 'input-schema', 'parents', 64)
    for parent in value['parents']:
        relative(parent)
        require(any(r['target_locator'].startswith(parent + '/') for r in value['files']),
                'input-schema', 'unrelated-parent', 64)


def one(rows, predicate, label):
    selected = [row for row in rows if predicate(row)]
    require(len(selected) == 1, 'identity-mismatch', label)
    return selected[0]


def build(selection, snapshot):
    """공식 producer 경로에서 도출한다. index 쓰기 resolver는 사용하지 않는다."""
    selection_schema(selection)
    root, inputs = Path(selection['root']), []
    tree = snapshot.tree
    root_st = os.fstat(tree.directory(str(root)))
    def source(role, path):
        data, row = snapshot.source(role, str(path))
        inputs.append(row)
        return data
    root_data = decode(source('root-identity', root / '.runtime/artifact-admission/v1/root-identity.json'))
    try:
        rid = I.RootIdentity.parse(root_data)
    except I.IdentityError as exc:
        raise RestoreError('identity-mismatch', str(exc)) from exc
    archive = selection['archive']
    archive_path = Path(archive['path'])
    require(Path(archive['seal_path']) == archive_path.parent / 'backup-seal.json',
            'identity-mismatch', 'archive-seal-path')
    seal_bytes = source('archive-seal', archive['seal_path'])
    require(digest(seal_bytes) == archive['seal_sha256'], 'input-drift', 'seal')
    seal = decode(seal_bytes)
    require(seal.get('schema_version') == 1 and seal.get('archive') == str(archive_path) and
            seal.get('artifact_root_id') == rid.artifact_root_id and
            normalized_sha(seal.get('archive_sha256')) == archive['sha256'],
            'identity-mismatch', 'seal-binding')
    _, arc = snapshot.source('archive', str(archive_path), json_data=False)
    require(arc['sha256'] == archive['sha256'], 'input-drift', 'archive')
    retired_bytes = source('retired-manifest', archive_path.parent / 'retired-manifest.jsonl')
    require(digest(retired_bytes) == normalized_sha(seal.get('manifest_sha256')),
            'input-drift', 'retired-manifest')
    retired = [decode(line) for line in retired_bytes.splitlines() if line]
    # artifact_cutover.retire가 같은 stamp로 발급한 정식 journal만 읽는다.
    stamp = archive_path.parent.name
    require(re.fullmatch(r'\d{8}T\d{6}Z', stamp), 'identity-mismatch', 'retirement-stamp')
    journal_path = root / '.runtime/artifact-producer/v1/migrations' / (stamp + '-retirement') / 'journal.jsonl'
    journal = [decode(line) for line in source('retirement-journal', journal_path).splitlines() if line]
    require(seal.get('file_count') == len(retired) and
            seal.get('byte_size') == sum(r.get('size', -1) for r in retired),
            'identity-mismatch', 'retired-count-size')
    cycles, manifests, locations = [], {}, {}
    for cid in sorted({r['cycle_id'] for r in selection['selections']}):
        record_path = P.cycle_record_path(root, cid)
        record = decode(source('record', record_path))
        require(record.get('cycle_id') == cid and record.get('state') == 'sealed',
                'identity-mismatch', 'sealed-record')
        camp = record.get('campaign_id'); identity(camp, 'campaign')
        directory = P.cycle_dir(root, camp, cid, record)
        directory.relative_to(root)
        tree.directory(str(directory))
        binding = decode(source('cycle-binding', directory / '.cycle.json'))
        require(binding.get('cycle_id') == cid and binding.get('campaign_id') == camp,
                'identity-mismatch', 'cycle-binding')
        manifest = decode(source('manifest', directory / 'manifest.json'))
        require(M.validate(manifest).ok and M.manifest_digest(manifest) == record.get('manifest_digest'),
                'identity-mismatch', 'manifest')
        require(manifest['artifact_root_id'] == rid.artifact_root_id and
                manifest['repository_id'] == rid.repository_id and
                manifest['cycle']['cycle_id'] == cid and manifest['cycle']['state'] == 'completed' and
                manifest['campaign']['campaign_id'] == camp, 'identity-mismatch', 'manifest-identity')
        manifests[cid], locations[cid] = manifest, directory
        cycles.append(dict(cycle_id=cid, campaign_id=camp, record_path=str(record_path),
                           binding_path=str(directory / '.cycle.json'),
                           manifest_path=str(directory / 'manifest.json'),
                           manifest_digest=record['manifest_digest']))
    files, payloads, parents = [], {}, set()
    total_bytes = 0
    afd = snapshot.sources[str(archive_path)][0]
    os.lseek(afd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(afd), 'rb') as raw, tarfile.open(fileobj=raw, mode='r:*') as tar:
        members = tar.getmembers()
        for row in selection['selections']:
            cid, aid, arev = row['cycle_id'], row['artifact_id'], row['artifact_revision_id']
            manifest = manifests[cid]
            one(manifest['artifacts'], lambda x: x['artifact_id'] == aid and x['cycle_id'] == cid, 'artifact')
            rev = one(manifest['artifact_revisions'], lambda x: x['artifact_id'] == aid and
                      x['artifact_revision_id'] == arev, 'revision')
            loc = rev['locator']
            require(loc['kind'] == 'cycle-relative' and relative(loc['path']).startswith('artifacts/'),
                    'identity-mismatch', 'manifest-locator')
            target = locations[cid] / loc['path']
            rel = target.relative_to(root).as_posix()
            require(loc['path'] == 'artifacts/' + row['member'], 'identity-mismatch', 'member-locator')
            old = one(retired, lambda x: x.get('source') == row['member'], 'retired-member')
            jr = one(journal, lambda x: x.get('source_locator') == row['member'], 'journal-member')
            # 현재 locator 또는 같은 stable cycle의 공식 구형 ID layout만 연결한다.
            # 다른 alias/다른 cycle/모호한 mapping을 content hash만으로 합치지 않는다.
            campaign_id = manifest['campaign']['campaign_id']
            historical = 'campaigns/' + campaign_id + '/cycles/' + cid + '/artifacts/' + row['member']
            require(old.get('target') in (rel, historical),
                    'identity-mismatch', 'retirement-target-cycle')
            require(jr.get('action') == 'retire_source' and jr.get('commit_state') == 'committed' and
                    jr.get('backup_archive') == str(archive_path) and
                    jr.get('target_locator') == old.get('target') and
                    normalized_sha(jr.get('sha256')) == rev['content_digest'] and
                    normalized_sha(old.get('sha256')) == rev['content_digest'] and
                    old.get('size') == rev['byte_size'], 'identity-mismatch', 'retirement-row')
            member = one(members, lambda x: x.name == row['member'], 'tar-member')
            require(member.isreg() and not member.issparse() and not member.linkname and
                    not any('sparse' in k.lower() for k in member.pax_headers) and
                    member.mode & ~0o777 == 0 and member.size == rev['byte_size'],
                    'member-invalid', row['member'])
            require(member.size <= MAX_BYTES, 'member-invalid', 'oversize')
            total_bytes += member.size
            require(total_bytes <= MAX_BYTES, 'member-invalid', 'total-oversize')
            stream = tar.extractfile(member)
            require(stream is not None, 'member-invalid', row['member'])
            data = stream.read(MAX_BYTES + 1); stream.close()
            require(len(data) == member.size and digest(data) == rev['content_digest'],
                    'member-invalid', 'bytes')
            payloads[rel] = data
            parents.update(str(Path(p).relative_to(root)) for p in tree.missing_parents(str(target)))
            files.append(dict(cycle_id=cid, artifact_id=aid, artifact_revision_id=arev,
                              target_locator=rel, member=row['member'], sha256=digest(data),
                              size=len(data), mode=member.mode))
    result = dict(schema_version=1, kind='sealed-bytes-restore-request',
                  root=dict(path=str(root), artifact_root_id=rid.artifact_root_id,
                            repository_id=rid.repository_id, dev=root_st.st_dev, ino=root_st.st_ino),
                  inputs=sorted(inputs, key=lambda r: (r['role'], r['path'])), cycles=cycles,
                  archive={k: v for k, v in arc.items() if k != 'role'},
                  files=sorted(files, key=lambda r: r['target_locator']),
                  parents=sorted(parents), total_bytes=sum(r['size'] for r in files))
    request_schema(result)
    snapshot.verify()
    return result, payloads

def selection_from_request(request):
    seal = one(request['inputs'], lambda r: r['role'] == 'archive-seal', 'archive-seal')
    return dict(schema_version=1, kind='sealed-bytes-selection', root=request['root']['path'],
                archive=dict(path=request['archive']['path'], sha256=request['archive']['sha256'],
                             seal_path=seal['path'], seal_sha256=seal['sha256']),
                selections=[{k: r[k] for k in ('cycle_id', 'artifact_id', 'artifact_revision_id', 'member')}
                            for r in request['files']])


def validate_request(request, snapshot):
    request_schema(request)
    rebuilt, payloads = build(selection_from_request(request), snapshot)
    require({k: v for k, v in request.items() if k != 'parents'} ==
            {k: v for k, v in rebuilt.items() if k != 'parents'},
            'input-drift', 'request-source-binding')
    require(set(rebuilt['parents']) <= set(request['parents']), 'path-unsafe', 'new-missing-parent')
    return payloads


def read_regular(tree, path, limit=MAX_JSON, *, record_temporary=False):
    fd = tree.open_file(path)
    try:
        st = os.fstat(fd)
        temporary = None
        if st.st_nlink == 2 and record_temporary:
            parent, leaf = path.rsplit('/', 1)
            pfd = tree.directory(parent)
            candidates = []
            for name in os.listdir(pfd):
                if not re.fullmatch(re.escape('.' + leaf + '.') + r'[0-9a-f]{24}\.tmp', name):
                    continue
                entry = os.stat(name, dir_fd=pfd, follow_symlinks=False)
                if stat.S_ISREG(entry.st_mode) and devino(entry) == devino(st):
                    candidates.append(parent + '/' + name)
            require(len(candidates) == 1, 'record-conflict', 'record-temporary-unproven')
            temporary = candidates[0]
        require(st.st_nlink == 1 or temporary is not None,
                'record-conflict', 'foreign-hardlink:' + path)
        h, size, data = fd_content(fd, limit=limit)
        tree.named(path, fd)
        observed = dict(dev=st.st_dev, ino=st.st_ino, sha256=h, size=size)
        if temporary is not None:
            observed['temporary'] = temporary
        return data, observed
    finally:
        os.close(fd)


def work_binding(request, cycle_id, tree):
    root = Path(request['root']['path'])
    identity(cycle_id, 'cycle')
    require(cycle_id not in {r['cycle_id'] for r in request['cycles']},
            'work-cycle-not-open', 'selected-cycle')
    rp = P.cycle_record_path(root, cycle_id)
    record_data, record_stat = read_regular(tree, str(rp))
    record = decode(record_data)
    require(record.get('state') == 'open' and record.get('cycle_id') == cycle_id,
            'work-cycle-not-open', cycle_id)
    identity(record.get('producer_id'), 'producer')
    route_id = record.get('route_id')
    require(isinstance(route_id, str) and re.fullmatch(r'rt-[0-9a-f]{16}', route_id),
            'work-cycle-not-open', 'route-id')
    route_path = root / '.runtime/routes' / (route_id + '.json')
    require(record.get('route_file') == str(route_path), 'work-cycle-not-open', 'route-path')
    route_bytes, route_stat = read_regular(tree, str(route_path))
    route = decode(route_bytes)
    require(isinstance(route, dict) and route.get('artifact_root') == str(root) and
            route.get('route_id') == route_id and
            route.get('route_hash') == record.get('route_hash') == P.route_identity.route_hash(route) and
            route_id == 'rt-' + route['route_hash'][7:23] and
            route.get('capability') == record.get('capability') == 'autopilot-code',
            'work-cycle-not-open', 'route-binding')
    directory = P.cycle_dir(root, record['campaign_id'], cycle_id, record)
    directory.relative_to(root)
    tree.directory(str(directory))
    binding_bytes, binding_stat = read_regular(tree, str(directory / '.cycle.json'))
    binding = decode(binding_bytes)
    require(binding.get('cycle_id') == cycle_id and
            binding.get('campaign_id') == record['campaign_id'], 'work-cycle-not-open', 'binding')
    # 공식 oracle는 새 작업 기록에만 적용한다. sealed target에 호출하지 않는다.
    base = directory / 'artifacts/plans/sealed-bytes-restore/_internal/requests'
    verdict = P.check_write(root, base / 'prospective.json')
    require(verdict.get('verdict') == 'allow' and verdict.get('cycle_id') == cycle_id and
            verdict.get('reason') == 'open-cycle-artifacts', 'work-cycle-not-open', verdict)
    tree.verify()
    return dict(path=str(base), cycle_id=cycle_id, producer_id=record['producer_id'],
                record_path=str(rp), record_stat=record_stat,
                route_path=str(route_path), route_stat=route_stat,
                binding_path=str(directory / '.cycle.json'), binding_stat=binding_stat)


def check_work(request, binding, tree):
    current = work_binding(request, binding['cycle_id'], tree)
    require(current == binding, 'work-cycle-not-open', 'work-binding-drift')


def target_state(tree, path, file, prepared=None):
    try:
        fd = tree.open_file(path)
    except FileNotFoundError:
        return dict(disposition='missing')
    try:
        st = os.fstat(fd)
        h, size, _ = fd_content(fd, limit=MAX_BYTES)
        tree.named(path, fd)
        require(h == file['sha256'] and size == file['size'], 'target-conflict', path)
        owned = prepared is not None and (st.st_dev, st.st_ino) == (prepared['dev'], prepared['ino'])
        require(st.st_nlink == 1 or (owned and st.st_nlink == 2),
                'target-conflict', 'foreign-hardlink:' + path)
        return dict(disposition='owned-published' if owned else 'verified-existing',
                    dev=st.st_dev, ino=st.st_ino, sha256=h, size=size, nlink=st.st_nlink)
    finally:
        os.close(fd)


# destructive-ok: reason=remove only exact owned private entry after inode and byte proof; boundary=retained work-cycle or admission dirfd exact leaf
def remove_owned(tree, path, fd, *, expected_sha=None, maximum_links=2):
    tree.named(path, fd)
    st = os.fstat(fd)
    require(1 <= st.st_nlink <= maximum_links, 'record-conflict', path)
    if expected_sha is not None:
        h, _, _ = fd_content(fd)
        require(h == expected_sha, 'record-conflict', path)
    parent, name = path.rsplit('/', 1)
    pfd = tree.directory(parent)
    tree.named(path, fd)
    os.unlink(name, dir_fd=pfd)
    os.fsync(pfd)


def write_bytes_fd(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        require(count > 0, 'record-write-failed', 'zero-write', 74)
        view = view[count:]


def atomic_record(tree, path, data):
    parent, leaf = path.rsplit('/', 1)
    pfd = tree.directory(parent)
    temporary = parent + '/.' + leaf + '.' + secrets.token_hex(12) + '.tmp'
    fd = tree.open_file(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL)
    anchor = None
    try:
        write_bytes_fd(fd, data)
        os.fsync(fd)
        tree.named(temporary, fd)
        try:
            os.link(temporary.rsplit('/', 1)[1], leaf, src_dir_fd=pfd, dst_dir_fd=pfd,
                    follow_symlinks=False)
        except OSError as exc:
            # NFS 오류 뒤에도 생성됐을 수 있다. own inode 확인 전 성공은 없다.
            try:
                out = tree.open_file(path)
            except (OSError, RestoreError):
                raise RestoreError('record-write-failed', path, 74) from exc
            try:
                require(devino(os.fstat(out)) == devino(os.fstat(fd)),
                        'record-conflict', path)
            finally:
                os.close(out)
        out = tree.open_file(path)
        try:
            h, size, _ = fd_content(out)
            require(devino(os.fstat(out)) == devino(os.fstat(fd)) and
                    h == digest(data) and size == len(data), 'record-write-failed', path, 74)
        finally:
            os.close(out)
        os.fsync(pfd)
        _checkpoint('record-published', dict(record_kind='request' if leaf == 'request.json'
                    else parent.rsplit('/', 1)[1], record_path=path, record_temporary=temporary))
        # NFS는 열린 임시 dentry를 unlink하면 .nfs link를 남길 수 있다.
        # 최종 이름의 동일 inode fd를 유지하고 임시 이름의 fd를 먼저 닫는다.
        anchor = tree.open_file(path)
        require(devino(os.fstat(anchor)) == devino(os.fstat(fd)), 'record-conflict', path)
        os.close(fd)
        fd = None
        remove_owned(tree, temporary, anchor, expected_sha=digest(data))
    finally:
        if anchor is not None:
            os.close(anchor)
        if fd is not None:
            os.close(fd)


class Records:
    def __init__(self, request, raw, binding, tree):
        self.request, self.raw, self.binding, self.tree = request, raw, binding, tree
        self.request_sha = digest(raw)
        self.base = binding['path'] + '/' + self.request_sha[7:]
        self.prepared, self.sealed, self.rows = {}, {}, []
        self.inert_staging = []
        self.sequence, self.previous = 0, None
        self.exists = False
        self.load()

    def load(self):
        try:
            self.tree.directory(self.base)
        except FileNotFoundError:
            return
        self.exists = True
        try:
            data, st = read_regular(self.tree, self.base + '/request.json', record_temporary=True)
        except FileNotFoundError:
            # crash가 남긴 directory만 있으면 기록 권한을 추정하지 않는다.
            names = os.listdir(self.tree.directory(self.base))
            require(all(n.startswith('.') and n.endswith('.tmp') for n in names),
                    'record-conflict', 'request-missing')
            for name in names:
                entry = os.stat(name, dir_fd=self.tree.directory(self.base), follow_symlinks=False)
                require(stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1,
                        'record-conflict', 'uncommitted-request-temporary')
                self.inert_staging.append(self.base + '/' + name)
            self.exists = False
            return
        require(data == self.raw, 'record-conflict', 'request-bytes')
        self.sealed[self.base + '/request.json'] = st
        allowed = {'request.json', 'prepared', 'observations', 'results', 'staging'}
        for name in os.listdir(self.tree.directory(self.base)):
            require(name in allowed or (name.startswith('.') and name.endswith('.tmp')),
                    'record-conflict', 'unknown-entry')
        pending, temporary_links = [], []
        for category in ('prepared', 'observations', 'results', 'staging'):
            directory = self.base + '/' + category
            try:
                fd = self.tree.directory(directory)
            except FileNotFoundError:
                continue
            for name in os.listdir(fd):
                path = directory + '/' + name
                if category == 'staging' or (name.startswith('.') and name.endswith('.tmp')):
                    # 임시 잔여는 권한이 아니며 자동 삭제/게시하지 않는다.
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    require(stat.S_ISREG(st.st_mode), 'record-conflict', path)
                    temporary_links.append((path, st))
                    self.inert_staging.append(path)
                    continue
                require(re.fullmatch(r'[0-9]+\.json', name), 'record-conflict', path)
                data, st = read_regular(self.tree, path, record_temporary=True)
                row = decode(data)
                keys(row, RECORD_KEYS)
                require(data == canonical(row) and type(row['schema_version']) is int and
                        row['schema_version'] == 1 and
                        row['request_sha256'] == self.request_sha and
                        row['work_cycle_id'] == self.binding['cycle_id'] and
                        row['producer_id'] == self.binding['producer_id'] and
                        row['kind'] == category, 'record-conflict', path)
                integer(row['sequence'])
                require(isinstance(row['payload'], dict), 'record-conflict', path)
                if category == 'prepared':
                    p = row['payload']
                    keys(p, 'index target_locator member sha256 size mode stage_path dev ino ancestors')
                    integer(p['index'])
                    require(p['index'] < len(self.request['files']) and name == str(p['index']) + '.json',
                            'record-conflict', 'prepared-index')
                    file = self.request['files'][p['index']]
                    require(all(p[k] == file[k] for k in ('target_locator', 'member', 'sha256', 'size', 'mode')),
                            'record-conflict', 'prepared-file')
                    require(p['stage_path'].startswith(self.base + '/staging/') and
                            re.fullmatch(str(p['index']) + r'\.[0-9a-f]{24}\.tmp',
                                         p['stage_path'].rsplit('/', 1)[1]),
                            'record-conflict', 'stage-path')
                    integer(p['dev']); integer(p['ino'])
                    require(p['index'] not in self.prepared, 'record-conflict', 'duplicate-prepared')
                    self.prepared[p['index']] = p
                else:
                    require(name == str(row['sequence']) + '.json', 'record-conflict', 'sequence-name')
                pending.append((row['sequence'], row, digest(data)))
                self.sealed[path] = st
        for path, st in temporary_links:
            require(st.st_nlink == 1 or (st.st_nlink == 2 and
                    (any(p['stage_path'] == path and (p['dev'], p['ino']) == devino(st)
                         for p in self.prepared.values()) or
                     any(s.get('temporary') == path and (s['dev'], s['ino']) == devino(st)
                         for s in self.sealed.values()))),
                    'record-conflict', 'foreign-temporary-hardlink')
        for sequence, row, h in sorted(pending, key=lambda r: r[0]):
            require(sequence == self.sequence and row['previous_sha256'] == self.previous,
                    'record-conflict', 'chain')
            self.sequence += 1
            self.previous = h
            self.rows.append(row)
        prepared_paths = {p['stage_path'] for p in self.prepared.values()}
        self.inert_staging = sorted(p for p in self.inert_staging if p not in prepared_paths)

    def verify(self):
        for path, expected in self.sealed.items():
            _, current = read_regular(self.tree, path, record_temporary=True)
            require(current == expected, 'record-conflict', path)

    def initialize(self):
        # 전체 요청·기록 schema/chain 검증 뒤에만 자체 record 게시 잔여를 정리한다.
        self.verify()
        for path, observed in list(self.sealed.items()):
            temporary = observed.get('temporary')
            if temporary is None:
                continue
            fd = self.tree.open_file(path)
            try:
                require(devino(os.fstat(fd)) == (observed['dev'], observed['ino']),
                        'record-conflict', path)
                remove_owned(self.tree, temporary, fd, expected_sha=observed['sha256'])
            finally:
                os.close(fd)
            _, self.sealed[path] = read_regular(self.tree, path)
            self.inert_staging = [p for p in self.inert_staging if p != temporary]
        paths = set(self.tree.missing_parents(self.base + '/request.json'))
        self.tree.directory(self.base, paths)
        if not self.exists:
            atomic_record(self.tree, self.base + '/request.json', self.raw)
            _, st = read_regular(self.tree, self.base + '/request.json')
            self.sealed[self.base + '/request.json'] = st
            self.exists = True
        for name in ('prepared', 'observations', 'results', 'staging'):
            path = self.base + '/' + name
            self.tree.directory(path, {path})

    def append(self, kind, payload, index=None):
        self.verify()
        row = dict(schema_version=1, kind=kind, request_sha256=self.request_sha,
                   work_cycle_id=self.binding['cycle_id'], producer_id=self.binding['producer_id'],
                   sequence=self.sequence, previous_sha256=self.previous, payload=payload)
        name = str(index if kind == 'prepared' else self.sequence) + '.json'
        path = self.base + '/' + kind + '/' + name
        data = canonical(row)
        atomic_record(self.tree, path, data)
        _, st = read_regular(self.tree, path)
        self.sealed[path] = st
        self.sequence += 1
        self.previous = digest(data)
        self.rows.append(row)
        if kind == 'prepared':
            self.prepared[index] = payload


@contextlib.contextmanager
def admission_lock(tree, root):
    path = root + '/.runtime/artifact-admission/v1/lock.flock'
    deadline = time.monotonic() + 30
    fd = None
    while True:
        candidate = tree.open_file(path, os.O_RDWR | os.O_CREAT)
        acquired = False
        try:
            st = os.fstat(candidate)
            require(st.st_nlink == 1, 'path-unsafe', 'admission-hardlink')
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            if acquired:
                try:
                    tree.named(path, candidate)
                    fd = candidate
                    break
                except (OSError, RestoreError):
                    fcntl.flock(candidate, fcntl.LOCK_UN)
                    acquired = False
        finally:
            if fd != candidate:
                os.close(candidate)
        require(time.monotonic() < deadline, 'admission-busy', code=75)
        time.sleep(0.05)
    try:
        yield
    finally:
        try:
            # 같은 inode만 지운다. successor/경로 drift는 보존한다.
            remove_owned(tree, path, fd, maximum_links=1)
        except (OSError, RestoreError):
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def validate_targets(request, tree, records=None, require_present=False):
    result = []
    root = request['root']['path']
    for index, file in enumerate(request['files']):
        prepared = records.prepared.get(index) if records else None
        state = target_state(tree, root + '/' + file['target_locator'], file, prepared)
        if require_present:
            require(state['disposition'] != 'missing', 'target-conflict', 'missing-target')
        if prepared:
            require(isinstance(prepared['ancestors'], dict), 'record-conflict', 'ancestors')
            for path, expected in prepared['ancestors'].items():
                require(path in tree.dirs, 'record-conflict', 'unknown-ancestor-reference')
                require(isinstance(expected, list) and len(expected) == 2 and
                        all(type(x) is int and x >= 0 for x in expected),
                        'record-conflict', 'ancestor-identity')
                require(list(devino(os.fstat(tree.directory(path)))) == expected,
                        'record-conflict', 'ancestor-drift:' + path)
            try:
                stage = tree.open_file(prepared['stage_path'])
            except FileNotFoundError:
                require(state['disposition'] != 'missing' and state.get('nlink') == 1,
                        'record-conflict', 'prepared-stage-absent')
            else:
                try:
                    tree.named(prepared['stage_path'], stage)
                    st = os.fstat(stage)
                    h, size, _ = fd_content(stage)
                    links = 2 if state['disposition'] == 'owned-published' else 1
                    require(devino(st) == (prepared['dev'], prepared['ino']) and
                            h == file['sha256'] and size == file['size'] and
                            stat.S_IMODE(st.st_mode) == file['mode'] and st.st_nlink == links,
                            'record-conflict', 'prepared-stage-drift')
                finally:
                    os.close(stage)
        result.append(dict(target_locator=file['target_locator'], **state))
    return result


def publish_one(index, request, payloads, snapshot, records, context):
    tree, root = snapshot.tree, request['root']['path']
    file = request['files'][index]
    target = root + '/' + file['target_locator']
    p = records.prepared.get(index)
    state = target_state(tree, target, file, p)
    stage_fd = None
    if p:
        try:
            stage_fd = tree.open_file(p['stage_path'])
        except FileNotFoundError:
            require(state['disposition'] != 'missing', 'record-conflict', 'prepared-stage-absent')
            state['disposition'] = 'verified-existing'
        if stage_fd is not None:
            st = os.fstat(stage_fd)
            h, size, _ = fd_content(stage_fd)
            require(devino(st) == (p['dev'], p['ino']) and 1 <= st.st_nlink <= 2 and
                    h == file['sha256'] and size == file['size'] and
                    stat.S_IMODE(st.st_mode) == file['mode'], 'record-conflict', 'stage-drift')
    elif state['disposition'] == 'missing':
        _checkpoint('before-stage', context)
        stage_path = records.base + '/staging/' + str(index) + '.' + secrets.token_hex(12) + '.tmp'
        stage_fd = tree.open_file(stage_path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        write_bytes_fd(stage_fd, payloads[file['target_locator']])
        os.fchmod(stage_fd, file['mode'])
        os.fsync(stage_fd)
        tree.named(stage_path, stage_fd)
        st = os.fstat(stage_fd)
        require(fd_content(stage_fd)[0] == file['sha256'], 'record-conflict', 'stage-bytes')
        _checkpoint('after-stage', context)
        p = dict(index=index, **{k: file[k] for k in
                 ('target_locator', 'member', 'sha256', 'size', 'mode')},
                 stage_path=stage_path, dev=st.st_dev, ino=st.st_ino,
                 ancestors={path: list(devino(os.fstat(fd))) for path, fd in tree.dirs.items()})
        records.append('prepared', p, index)
        _checkpoint('after-prepared', context)
    try:
        if state['disposition'] == 'missing':
            snapshot.verify(); records.verify()
            check_work(request, records.binding, tree)
            _checkpoint('before-link', context)
            # test seam 이후에도 실제 source/stage/ancestor 검증을 생략하지 않는다.
            snapshot.verify(); records.verify(); tree.named(p['stage_path'], stage_fd)
            require(fd_content(stage_fd)[0] == file['sha256'], 'record-conflict', 'stage-drift')
            require(stat.S_IMODE(os.fstat(stage_fd).st_mode) == file['mode'],
                    'record-conflict', 'stage-mode-drift')
            parent, leaf = target.rsplit('/', 1)
            pfd = tree.directory(parent, {root + '/' + x for x in request['parents']})
            sfd = tree.directory(records.base + '/staging')
            require(os.fstat(pfd).st_dev == os.fstat(stage_fd).st_dev,
                    'primitive-unsupported', 'EXDEV', 69)
            link_error = None
            try:
                _publish_link(p['stage_path'].rsplit('/', 1)[1], leaf, sfd, pfd)
            except OSError as exc:
                link_error = exc
            _checkpoint('after-link', context)
            try:
                tree.named(p['stage_path'], stage_fd)
                state = target_state(tree, target, file, p)
                require(state['disposition'] != 'missing', 'publish-ambiguous', 'target-absent', 74)
                os.fsync(pfd)
            except (OSError, RestoreError) as exc:
                if link_error is not None and link_error.errno in (errno.EXDEV, errno.ENOSYS, errno.ENOTSUP):
                    raise RestoreError('primitive-unsupported', str(link_error), 69) from exc
                raise RestoreError('publish-ambiguous', str(exc), 74) from exc
            try:
                snapshot.verify(); records.verify()
                check_work(request, records.binding, tree)
            except RestoreError as exc:
                # 게시된 파일은 보존하며 source drift의 이유를 모호성으로 숨기지 않는다.
                raise RestoreError(exc.reason, exc.detail, 74) from exc
        observed = dict(index=index, phase='published-observed', **state)
        records.append('observations', observed)
        _checkpoint('before-cleanup', context)
        if stage_fd is not None:
            if state['disposition'] == 'owned-published':
                anchor = tree.open_file(target)
                try:
                    require(devino(os.fstat(anchor)) == devino(os.fstat(stage_fd)),
                            'target-conflict', 'cleanup-anchor-drift')
                    os.close(stage_fd)
                    stage_fd = None
                    remove_owned(tree, p['stage_path'], anchor, expected_sha=file['sha256'])
                finally:
                    os.close(anchor)
            else:
                # 같은 bytes의 별개 기존 target은 보존하며 미게시 stage도 남긴다.
                records.inert_staging = sorted(set(records.inert_staging + [p['stage_path']]))
        _checkpoint('after-cleanup', context)
        return dict(target_locator=file['target_locator'],
                    disposition='verified-created' if state['disposition'] == 'owned-published'
                    else state['disposition'])
    finally:
        if stage_fd is not None:
            os.close(stage_fd)


def run(args):
    primitives()
    with contextlib.closing(Snapshot()) as initial:
        raw, _ = initial.source('request', absolute(args.request))
        document = decode(raw)
        if args.command == 'plan':
            request, _ = build(document, initial)
            validate_targets(request, initial.tree)
            initial.verify()
            output = canonical(request)
            sys.stdout.buffer.write(output)
            print(digest(output), file=sys.stderr)
            return 0
        request_schema(document)
        require(raw == canonical(document), 'input-schema', 'noncanonical-request', 64)
        if args.command == 'apply':
            require(digest(raw) == sha(args.expect_request_digest),
                    'request-digest-mismatch', code=64)
        payloads = validate_request(document, initial)
        binding = records = None
        if args.command == 'apply':
            binding = work_binding(document, args.work_cycle, initial.tree)
            records = Records(document, raw, binding, initial.tree)
        states = validate_targets(document, initial.tree, records, args.command == 'verify')
        initial.verify()
        if args.command == 'verify':
            return dict(status='verified', files=states)
        if args.dry_run:
            with contextlib.closing(Snapshot()) as second:
                validate_request(document, second)
                require(work_binding(document, args.work_cycle, second.tree) == binding,
                        'work-cycle-not-open', 'drift')
                current = Records(document, raw, binding, second.tree)
                validate_targets(document, second.tree, current)
                second.verify()
            initial.verify(); records.verify()
            return dict(status='validated-dry-run', request_sha256=digest(raw), files=states,
                        inert_staging=records.inert_staging, writes=0)
        context = dict(request=document, work_cycle=args.work_cycle, records=records,
                       snapshot=initial, index=None)
        with admission_lock(initial.tree, document['root']['path']):
            with contextlib.closing(Snapshot()) as second:
                validate_request(document, second)
                second.verify()
            initial.verify(); records.verify(); check_work(document, binding, initial.tree)
            validate_targets(document, initial.tree, records)
            _checkpoint('validated', context)
            records.initialize()
            results = []
            try:
                for index in range(len(document['files'])):
                    context['index'] = index
                    initial.verify(); records.verify(); check_work(document, binding, initial.tree)
                    results.append(publish_one(index, document, payloads, initial, records, context))
                _checkpoint('before-result', context)
                initial.verify(); records.verify(); check_work(document, binding, initial.tree)
                validate_targets(document, initial.tree, records, True)
                records.append('results', dict(status='verified', files=results,
                                               inert_staging=records.inert_staging))
                _checkpoint('after-result', context)
                initial.verify(); records.verify()
                validate_targets(document, initial.tree, records, True)
                return dict(status='verified', request_sha256=digest(raw), files=results,
                            work_cycle_id=binding['cycle_id'], records=records.base,
                            inert_staging=records.inert_staging)
            except (OSError, RestoreError) as exc:
                try:
                    records.append('observations', dict(phase='failed', reason=str(exc), files=results))
                except (OSError, RestoreError):
                    pass
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'apply', 'verify'):
        command = sub.add_parser(name)
        command.add_argument('--request', required=True)
        if name == 'apply':
            command.add_argument('--expect-request-digest', required=True)
            command.add_argument('--work-cycle', required=True)
            command.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run(args)
        if isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except RestoreError as exc:
        print(json.dumps(dict(status='blocked', reason=exc.reason, detail=exc.detail),
                         ensure_ascii=False), file=sys.stderr)
        return exc.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError) as exc:
        code, reason = 65, 'input-or-filesystem-invalid'
        if isinstance(exc, OSError):
            if exc.errno in (errno.EXDEV, errno.ENOSYS, errno.ENOTSUP):
                code, reason = 69, 'primitive-unsupported'
            elif not isinstance(exc, FileNotFoundError):
                code, reason = 74, 'verification-io'
        print(json.dumps(dict(status='blocked', reason=reason,
                              detail=str(exc)), ensure_ascii=False), file=sys.stderr)
        return code


if __name__ == '__main__':
    raise SystemExit(main())
