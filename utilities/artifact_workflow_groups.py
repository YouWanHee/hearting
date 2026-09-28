#!/usr/bin/env python3
"""Producer-owned, evidence-bound campaign workflow group declarations.

This is metadata about cycles, never a change to route lineage or a cycle
manifest.  `prepare` is read-only; `apply` is one checked atomic replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_admission as admission  # noqa: E402
import artifact_identity as identity  # noqa: E402
import artifact_lifecycle as lifecycle  # noqa: E402
import artifact_producer as producer  # noqa: E402

CONTRACT = "artifact-workflow-groups/v1"
NAME = "workflow-groups.json"
MAX_DECLARATION = 256 * 1024
MAX_EVIDENCE = 16 * 1024 * 1024
MAX_EVIDENCE_TOTAL = 64 * 1024 * 1024
GROUP_ID = re.compile(r"wgrp_[0-9a-f]{32}\Z")
ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}\Z")
REVISION_ID = re.compile(r"arev_[0-9a-f]{32}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
KINDS = frozenset(("precedes", "followup", "retry", "parallel"))
TOP_FIELDS = frozenset(("schema_version", "contract", "artifact_root_id", "repository_id", "campaign_id", "groups"))
GROUP_FIELDS = frozenset(("group_id", "title", "members", "relations"))
MEMBER_FIELDS = frozenset(("cycle_id", "stage_label"))
RELATION_FIELDS = frozenset(("from_cycle_id", "to_cycle_id", "kind", "rationale", "evidence_refs"))
EVIDENCE_FIELDS = frozenset(("path", "artifact_id", "artifact_revision_id", "sha256"))


class WorkflowGroupError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise WorkflowGroupError("json-duplicate-key", key)
        out[key] = value
    return out


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except WorkflowGroupError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise WorkflowGroupError("json-invalid", str(exc)) from exc
    if not isinstance(value, dict):
        raise WorkflowGroupError("json-object-required")
    return value


def _closed(value: Any, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise WorkflowGroupError(code, str(sorted(value.keys())) if isinstance(value, dict) else type(value).__name__)
    return value


def _text(value: Any, maximum: int, code: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or unicodedata.normalize("NFC", value) != value
            or len(value.splitlines()) != 1
            or any(ord(char) <= 31 or ord(char) == 127 for char in value)):
        raise WorkflowGroupError(code)
    return value


def stage_label_from_title(title: Any) -> str:
    """Make a safe, bounded display label from the existing cycle title."""
    normalized = unicodedata.normalize("NFC", str(title or ""))
    clean = "".join(char for char in normalized if ord(char) > 31 and ord(char) != 127)
    return clean[:40].strip() or "진행"


def _regular(path: Path, *, missing: bool = False, cap: int = MAX_DECLARATION) -> bytes | None:
    try:
        meta = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise WorkflowGroupError("file-missing", str(path))
    if not stat.S_ISREG(meta.st_mode):
        raise WorkflowGroupError("regular-file-required", str(path))
    if meta.st_size > cap:
        raise WorkflowGroupError("file-size-limit", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorkflowGroupError("file-read-failed", str(path)) from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or
                (opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)):
            raise WorkflowGroupError("file-changed-during-read", str(path))
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(cap + 1)
        after = os.fstat(fd)
        if len(raw) > cap:
            raise WorkflowGroupError("file-size-limit", str(path))
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise WorkflowGroupError("file-changed-during-read", str(path))
        return raw
    finally:
        os.close(fd)


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str):
        raise WorkflowGroupError("evidence-path-invalid", str(value))
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise WorkflowGroupError("evidence-path-invalid", str(value)) from exc
    if (length > 512 or "\\" in value
            or value.startswith("/") or any(ord(char) <= 31 or ord(char) == 127 for char in value)):
        raise WorkflowGroupError("evidence-path-invalid", str(value))
    parts = value.split("/")
    if len(parts) < 5 or parts[0] != "campaigns" or any(part in ("", ".", "..") for part in parts):
        raise WorkflowGroupError("evidence-path-invalid", value)
    return Path(*parts)


def _no_symlink(root: Path, relative: Path) -> None:
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise WorkflowGroupError("evidence-path-unsafe", str(cursor))


def _evidence_fd(root: Path, relative: Path) -> int:
    """Open every path component with O_NOFOLLOW, including the final file."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags_dir)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        result = os.open(relative.parts[-1], flags_file, dir_fd=fd)
        return result
    except FileNotFoundError as exc:
        raise WorkflowGroupError("file-missing", str(relative)) from exc
    except OSError as exc:
        raise WorkflowGroupError("evidence-path-unsafe", str(relative)) from exc
    finally:
        os.close(fd)


def _evidence_bytes(root: Path, relative: Path, *, remaining: int | None = None) -> bytes:
    fd = _evidence_fd(root, relative)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WorkflowGroupError("evidence-path-unsafe", str(relative))
        if before.st_size > MAX_EVIDENCE:
            raise WorkflowGroupError("evidence-file-size-limit", str(relative))
        if remaining is not None and before.st_size > remaining:
            raise WorkflowGroupError("read-budget", str(relative))
        chunks = []
        total = 0
        limit = MAX_EVIDENCE + 1 if remaining is None else min(MAX_EVIDENCE + 1, remaining)
        while total < limit:
            chunk = os.read(fd, min(1024 * 1024, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > MAX_EVIDENCE:
            raise WorkflowGroupError("evidence-file-size-limit", str(relative))
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise WorkflowGroupError("file-changed-during-read", str(relative))
        return b"".join(chunks)
    finally:
        os.close(fd)


def _evidence_stat(root: Path, relative: Path) -> os.stat_result:
    fd = _evidence_fd(root, relative)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            raise WorkflowGroupError("evidence-path-unsafe", str(relative))
        return meta
    finally:
        os.close(fd)


def _context(root: Path, campaign_id: str) -> tuple[dict[str, Any], Path, str, str]:
    if not identity.is_well_formed(campaign_id, "campaign"):
        raise WorkflowGroupError("campaign-id-invalid", campaign_id)
    root_identity = lifecycle.read_root_identity(root)
    if root_identity is None:
        raise WorkflowGroupError("root-identity-missing")
    campaign = producer.read_campaign(root, campaign_id)
    if campaign is None:
        raise WorkflowGroupError("campaign-unknown", campaign_id)
    directory = producer.campaign_dir(root, campaign_id, campaign)
    if directory.is_symlink() or not directory.is_dir():
        raise WorkflowGroupError("campaign-dir-invalid", str(directory))
    _no_symlink(root, directory.relative_to(root))
    return campaign, directory, root_identity.artifact_root_id, root_identity.repository_id


def declaration_path(root: Path, campaign_id: str) -> Path:
    return _context(Path(root).resolve(), campaign_id)[1] / NAME


def _member_dir(root: Path, campaign: Mapping[str, Any], cycle_id: str) -> Path:
    if not identity.is_well_formed(cycle_id, "cycle") or cycle_id not in campaign.get("cycles", []):
        raise WorkflowGroupError("cycle-not-member", cycle_id)
    record = producer.read_cycle_record(root, cycle_id)
    if record is None or record.get("campaign_id") != campaign["campaign_id"]:
        raise WorkflowGroupError("cycle-record-mismatch", cycle_id)
    directory = producer.cycle_dir(root, campaign["campaign_id"], cycle_id, record)
    _no_symlink(root, directory.relative_to(root))
    marker = _json(_regular(directory / ".cycle.json", cap=4096) or b"")
    if marker.get("cycle_id") != cycle_id or marker.get("campaign_id") != campaign["campaign_id"]:
        raise WorkflowGroupError("cycle-binding-mismatch", cycle_id)
    return directory


def _manifest(root: Path, campaign: Mapping[str, Any], cycle_id: str, directory: Path) -> dict[str, Any]:
    record = producer.read_cycle_record(root, cycle_id)
    path = directory / "manifest.json" if record and record.get("state") == "sealed" else (
        producer.producer_dir(root) / "open-manifests" / f"{cycle_id}.json")
    value = _json(_regular(path, cap=32 * 1024 * 1024) or b"")
    root_identity = lifecycle.read_root_identity(root)
    if (not isinstance(value.get("cycle"), dict) or not isinstance(value.get("campaign"), dict)
            or value["cycle"].get("cycle_id") != cycle_id
            or value["campaign"].get("campaign_id") != campaign["campaign_id"]
            or root_identity is None or value.get("artifact_root_id") != root_identity.artifact_root_id
            or value.get("repository_id") != root_identity.repository_id):
        raise WorkflowGroupError("manifest-identity-mismatch", cycle_id)
    return value


def _evidence_owner(root: Path, path: Any, endpoints: tuple[Path, Path]) -> tuple[Path, Path]:
    relative = _safe_relative(path)
    _no_symlink(root, relative)
    absolute = root / relative
    for directory in endpoints:
        try:
            inside = absolute.relative_to(directory)
        except ValueError:
            continue
        if len(inside.parts) >= 2 and inside.parts[0] == "artifacts":
            return absolute, directory
    raise WorkflowGroupError("evidence-endpoint-mismatch", str(path))


def _bind_evidence(root: Path, campaign: Mapping[str, Any], ref: Mapping[str, Any],
                   endpoints: tuple[tuple[str, Path], tuple[str, Path]],
                   *, remaining: int | None = None) -> dict[str, str]:
    if not isinstance(ref, dict) or set(ref) not in ({"path"}, EVIDENCE_FIELDS):
        raise WorkflowGroupError("evidence-fields-invalid")
    absolute, directory = _evidence_owner(root, ref["path"], (endpoints[0][1], endpoints[1][1]))
    raw = _evidence_bytes(root, absolute.relative_to(root), remaining=remaining)
    digest = _digest(raw)
    cycle_id = next(cid for cid, cdir in endpoints if cdir == directory)
    manifest = _manifest(root, campaign, cycle_id, directory)
    rel = str(absolute.relative_to(directory)).replace(os.sep, "/")
    rows = [row for row in manifest.get("artifact_revisions", [])
            if isinstance(row, dict) and isinstance(row.get("locator"), dict)
            and row["locator"].get("path") == rel and row.get("content_digest") == digest]
    if len(rows) != 1:
        raise WorkflowGroupError("evidence-not-current-manifest", str(ref["path"]))
    row = rows[0]
    artifact_id = row.get("artifact_id")
    revision_id = row.get("artifact_revision_id")
    if not ARTIFACT_ID.fullmatch(str(artifact_id)) or not REVISION_ID.fullmatch(str(revision_id)):
        raise WorkflowGroupError("evidence-artifact-id-invalid", str(ref["path"]))
    if not any(isinstance(item, dict) and item.get("artifact_id") == artifact_id
               for item in manifest.get("artifacts", [])):
        raise WorkflowGroupError("evidence-artifact-missing", str(ref["path"]))
    bound = {"path": str(ref["path"]), "artifact_id": artifact_id,
             "artifact_revision_id": revision_id, "sha256": digest}
    if set(ref) == EVIDENCE_FIELDS and dict(ref) != bound:
        raise WorkflowGroupError("evidence-binding-stale", str(ref["path"]))
    return bound


def _validate_document(root: Path, campaign: Mapping[str, Any], directory: Path,
                       root_id: str, repo_id: str, value: Any) -> dict[str, Any]:
    doc = dict(_closed(value, TOP_FIELDS, "declaration-fields-invalid"))
    if (type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or doc["contract"] != CONTRACT
            or doc["artifact_root_id"] != root_id or doc["repository_id"] != repo_id
            or doc["campaign_id"] != campaign["campaign_id"]):
        raise WorkflowGroupError("declaration-identity-mismatch")
    groups = doc["groups"]
    if not isinstance(groups, list) or len(groups) > 32:
        raise WorkflowGroupError("group-count-invalid")
    group_ids: set[str] = set()
    all_members: set[str] = set()
    relation_total = evidence_total = 0
    for group in groups:
        _closed(group, GROUP_FIELDS, "group-fields-invalid")
        gid = group["group_id"]
        if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid) or gid in group_ids:
            raise WorkflowGroupError("group-id-invalid", str(gid))
        group_ids.add(gid)
        _text(group["title"], 120, "group-title-invalid")
        members = group["members"]
        relations = group["relations"]
        if not isinstance(members, list) or not 1 <= len(members) <= 64:
            raise WorkflowGroupError("member-count-invalid", gid)
        if not isinstance(relations, list) or len(relations) > 128:
            raise WorkflowGroupError("relation-count-invalid", gid)
        local_members: dict[str, Path] = {}
        for member in members:
            _closed(member, MEMBER_FIELDS, "member-fields-invalid")
            cid = member["cycle_id"]
            if not isinstance(cid, str) or not identity.is_well_formed(cid, "cycle"):
                raise WorkflowGroupError("cycle-id-invalid", str(cid))
            if cid in all_members:
                raise WorkflowGroupError("member-duplicate", str(cid))
            _text(member["stage_label"], 40, "stage-label-invalid")
            local_members[cid] = _member_dir(root, campaign, cid)
            all_members.add(cid)
        pairs: set[frozenset[str]] = set()
        directed: dict[str, set[str]] = {cid: set() for cid in local_members}
        parallel: list[tuple[str, str]] = []
        for relation in relations:
            _closed(relation, RELATION_FIELDS, "relation-fields-invalid")
            source, target = relation["from_cycle_id"], relation["to_cycle_id"]
            kind = relation["kind"]
            if (not isinstance(source, str) or not isinstance(target, str) or not isinstance(kind, str)
                    or source not in local_members or target not in local_members or source == target
                    or kind not in KINDS):
                raise WorkflowGroupError("relation-endpoint-invalid", gid)
            pair = frozenset((source, target))
            if pair in pairs:
                raise WorkflowGroupError("relation-pair-duplicate", gid)
            pairs.add(pair)
            if kind == "parallel":
                if source >= target:
                    raise WorkflowGroupError("parallel-order-invalid", gid)
                parallel.append((source, target))
            else:
                directed[source].add(target)
            _text(relation["rationale"], 280, "relation-rationale-invalid")
            refs = relation["evidence_refs"]
            if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
                raise WorkflowGroupError("evidence-count-invalid", gid)
            seen_refs: set[str] = set()
            for ref in refs:
                _closed(ref, EVIDENCE_FIELDS, "evidence-fields-invalid")
                path = ref["path"]
                if not isinstance(path, str):
                    raise WorkflowGroupError("evidence-path-invalid", str(path))
                if path in seen_refs:
                    raise WorkflowGroupError("evidence-duplicate", str(path))
                seen_refs.add(path)
                _evidence_owner(root, path, (local_members[source], local_members[target]))
                if (not ARTIFACT_ID.fullmatch(str(ref["artifact_id"]))
                        or not REVISION_ID.fullmatch(str(ref["artifact_revision_id"]))
                        or not DIGEST.fullmatch(str(ref["sha256"]))):
                    raise WorkflowGroupError("evidence-id-invalid", str(path))
            evidence_total += len(refs)
        relation_total += len(relations)
        def reachable(start: str, end: str) -> bool:
            pending, visited = [start], set()
            while pending:
                node = pending.pop()
                if node == end:
                    return True
                if node in visited:
                    continue
                visited.add(node)
                pending.extend(directed[node])
            return False
        for source in directed:
            for target in directed[source]:
                if reachable(target, source):
                    raise WorkflowGroupError("relation-cycle", gid)
        for source, target in parallel:
            if reachable(source, target) or reachable(target, source):
                raise WorkflowGroupError("parallel-direction-conflict", gid)
    if len(all_members) > 256 or relation_total > 256 or evidence_total > 512:
        raise WorkflowGroupError("declaration-count-limit")
    if len(_bytes(doc)) > MAX_DECLARATION:
        raise WorkflowGroupError("declaration-size-limit")
    return doc


def _load_existing(path: Path) -> dict[str, Any] | None:
    raw = _regular(path, missing=True)
    return None if raw is None else _json(raw)


def _proposal_groups(root: Path, campaign: Mapping[str, Any], value: Any,
                     *, allow_missing_id: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != {"groups"} or not isinstance(value["groups"], list):
        raise WorkflowGroupError("proposal-fields-invalid")
    new_refs: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    bound_paths: dict[str, tuple[dict[str, str], Path]] = {}
    unique_bytes = 0
    for proposed in value["groups"]:
        if not isinstance(proposed, dict) or set(proposed) not in (GROUP_FIELDS, GROUP_FIELDS - {"group_id"}):
            raise WorkflowGroupError("group-fields-invalid")
        gid = proposed.get("group_id")
        if gid is None and allow_missing_id:
            gid = "wgrp_" + secrets.token_hex(16)
        if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid):
            raise WorkflowGroupError("group-id-invalid", str(gid))
        if not isinstance(proposed["members"], list) or not isinstance(proposed["relations"], list):
            raise WorkflowGroupError("group-array-invalid", gid)
        members = [dict(_closed(item, MEMBER_FIELDS, "member-fields-invalid")) for item in proposed["members"]]
        dirs = {item["cycle_id"]: _member_dir(root, campaign, item["cycle_id"]) for item in members}
        relations = []
        for relation in proposed["relations"]:
            _closed(relation, RELATION_FIELDS, "relation-fields-invalid")
            if not isinstance(relation["evidence_refs"], list):
                raise WorkflowGroupError("evidence-count-invalid", gid)
            source, target = relation["from_cycle_id"], relation["to_cycle_id"]
            if source not in dirs or target not in dirs:
                raise WorkflowGroupError("relation-endpoint-invalid", gid)
            refs = []
            for ref in relation["evidence_refs"]:
                if not isinstance(ref, dict) or set(ref) not in ({"path"}, EVIDENCE_FIELDS):
                    raise WorkflowGroupError("evidence-fields-invalid")
                path = ref["path"]
                _safe_relative(path)
                if path in bound_paths:
                    bound, owner = bound_paths[path]
                    _evidence_owner(root, path, (dirs[source], dirs[target]))
                    if owner not in (dirs[source], dirs[target]) or (
                            set(ref) == EVIDENCE_FIELDS and dict(ref) != bound):
                        raise WorkflowGroupError("evidence-binding-stale", str(path))
                else:
                    relative = _safe_relative(path)
                    size = _evidence_stat(root, relative).st_size
                    if size > MAX_EVIDENCE:
                        raise WorkflowGroupError("evidence-file-size-limit", str(path))
                    if unique_bytes + size > MAX_EVIDENCE_TOTAL:
                        raise WorkflowGroupError("evidence-total-size-limit")
                    bound = _bind_evidence(root, campaign, ref, ((source, dirs[source]), (target, dirs[target])),
                                           remaining=MAX_EVIDENCE_TOTAL - unique_bytes)
                    owner = _evidence_owner(root, path, (dirs[source], dirs[target]))[1]
                    bound_paths[path] = (bound, owner)
                    unique_bytes += size
                refs.append(bound)
                new_refs.append({"from_cycle_id": source, "to_cycle_id": target, **bound})
            relations.append({**relation, "evidence_refs": refs})
        groups.append({"group_id": gid, "title": proposed["title"],
                       "members": members, "relations": relations})
    return groups, new_refs


def _merge(existing: list[dict[str, Any]], proposed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = json.loads(json.dumps(existing))
    positions = {group["group_id"]: index for index, group in enumerate(result)}
    for group in proposed:
        gid = group["group_id"]
        if gid not in positions:
            positions[gid] = len(result)
            result.append(group)
            continue
        current = result[positions[gid]]
        if current["title"] != group["title"]:
            raise WorkflowGroupError("merge-title-conflict", gid)
        by_id = {item["cycle_id"]: item for item in current["members"]}
        for member in group["members"]:
            prior = by_id.get(member["cycle_id"])
            if prior is not None and prior != member:
                raise WorkflowGroupError("merge-member-conflict", member["cycle_id"])
            if prior is None:
                current["members"].append(member)
                by_id[member["cycle_id"]] = member
        by_pair = {frozenset((item["from_cycle_id"], item["to_cycle_id"])): item
                   for item in current["relations"]}
        for relation in group["relations"]:
            pair = frozenset((relation["from_cycle_id"], relation["to_cycle_id"]))
            prior = by_pair.get(pair)
            if prior is not None and prior != relation:
                raise WorkflowGroupError("merge-relation-conflict", gid)
            if prior is None:
                current["relations"].append(relation)
                by_pair[pair] = relation
    return result


def _new_evidence(existing: dict[str, Any] | None, desired: dict[str, Any],
                  *, replace: bool) -> list[dict[str, Any]]:
    prior = {} if replace or existing is None else {
        (group["group_id"], relation["from_cycle_id"], relation["to_cycle_id"]): relation
        for group in existing["groups"] for relation in group["relations"]
    }
    return [
        {"from_cycle_id": relation["from_cycle_id"], "to_cycle_id": relation["to_cycle_id"], **ref}
        for group in desired["groups"] for relation in group["relations"]
        if prior.get((group["group_id"], relation["from_cycle_id"], relation["to_cycle_id"])) != relation
        for ref in relation["evidence_refs"]
    ]


def _evidence_size(root: Path, refs: list[dict[str, Any]]) -> int:
    total = 0
    for rel in {ref["path"] for ref in refs}:
        relative = _safe_relative(rel)
        try:
            meta = _evidence_stat(root, relative)
        except WorkflowGroupError as exc:
            if exc.code != "file-missing":
                raise
            continue  # A historical reference may have become stale.
        if meta.st_size > MAX_EVIDENCE:
            raise WorkflowGroupError("evidence-file-size-limit", rel)
        total += meta.st_size
    return total


def _check_group_ids_elsewhere(root: Path, directory: Path, group_ids: set[str]) -> None:
    if not group_ids:
        return
    campaigns = root / "campaigns"
    for other in campaigns.iterdir():
        if other == directory or other.is_symlink() or not other.is_dir():
            continue
        path = other / NAME
        if not path.exists():
            continue
        try:
            doc = _load_existing(path)
        except WorkflowGroupError:
            continue  # A foreign invalid declaration cannot claim an ID.
        if isinstance(doc, dict) and isinstance(doc.get("groups"), list):
            for group in doc["groups"]:
                if isinstance(group, dict) and group.get("group_id") in group_ids:
                    raise WorkflowGroupError("group-id-cross-campaign", str(group["group_id"]))


def prepare(root: Path, campaign_id: str, proposal: Mapping[str, Any], *, replace: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    campaign, directory, root_id, repo_id = _context(root, campaign_id)
    path = directory / NAME
    before_raw = _regular(path, missing=True)
    existing = None if before_raw is None else _validate_document(
        root, campaign, directory, root_id, repo_id, _json(before_raw))
    groups, _ = _proposal_groups(root, campaign, proposal, allow_missing_id=True)
    if replace:
        desired_groups = groups
    else:
        desired_groups = _merge(existing["groups"] if existing else [], groups)
    _check_group_ids_elsewhere(root, directory, {group["group_id"] for group in groups})
    doc = {"schema_version": 1, "contract": CONTRACT, "artifact_root_id": root_id,
           "repository_id": repo_id, "campaign_id": campaign_id, "groups": desired_groups}
    _validate_document(root, campaign, directory, root_id, repo_id, doc)
    refs = _new_evidence(existing, doc, replace=replace)
    if _evidence_size(root, [ref for group in doc["groups"] for relation in group["relations"]
                             for ref in relation["evidence_refs"]]) > MAX_EVIDENCE_TOTAL:
        raise WorkflowGroupError("evidence-total-size-limit")
    desired_raw = _bytes(doc)
    return {"plan_schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id,
            "mode": "replace" if replace else "merge", "before_sha256": _digest(before_raw) if before_raw else None,
            "after_sha256": _digest(desired_raw), "document": doc, "new_evidence": refs}


def _validate_plan(value: Any) -> Mapping[str, Any]:
    fields = frozenset(("plan_schema_version", "contract", "campaign_id", "mode", "before_sha256",
                        "after_sha256", "document", "new_evidence"))
    plan = _closed(value, fields, "plan-fields-invalid")
    if (plan["plan_schema_version"] != 1 or plan["contract"] != CONTRACT
            or plan["mode"] not in ("merge", "replace") or not DIGEST.fullmatch(str(plan["after_sha256"]))
            or (plan["before_sha256"] is not None and not DIGEST.fullmatch(str(plan["before_sha256"])))
            or not isinstance(plan["new_evidence"], list)):
        raise WorkflowGroupError("plan-invalid")
    if _digest(_bytes(plan["document"])) != plan["after_sha256"]:
        raise WorkflowGroupError("plan-digest-mismatch")
    return plan


def apply(root: Path, plan_value: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(root).resolve()
    plan = _validate_plan(plan_value)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        campaign, directory, root_id, repo_id = _context(root, plan["campaign_id"])
        path = directory / NAME
        doc = _validate_document(root, campaign, directory, root_id, repo_id, plan["document"])
        _check_group_ids_elsewhere(root, directory, {group["group_id"] for group in doc["groups"]})
        before_raw = _regular(path, missing=True)
        current_digest = _digest(before_raw) if before_raw is not None else None
        if current_digest == plan["after_sha256"]:
            return {"status": "already-applied", **verify(root, plan["campaign_id"], expected=plan["after_sha256"])}
        if current_digest != plan["before_sha256"]:
            raise WorkflowGroupError("declaration-preimage-conflict")
        old = None if before_raw is None else _validate_document(root, campaign, directory, root_id, repo_id, _json(before_raw))
        if plan["mode"] == "merge":
            old_groups = {group["group_id"]: group for group in (old["groups"] if old else [])}
            for gid, group in old_groups.items():
                current = next((row for row in doc["groups"] if row["group_id"] == gid), None)
                if current is None or current["title"] != group["title"]:
                    raise WorkflowGroupError("merge-removal-forbidden", gid)
                current_members = {item["cycle_id"]: item for item in current["members"]}
                if any(current_members.get(item["cycle_id"]) != item for item in group["members"]):
                    raise WorkflowGroupError("merge-removal-forbidden", gid)
                current_relations = {
                    frozenset((item["from_cycle_id"], item["to_cycle_id"])): item
                    for item in current["relations"]
                }
                if any(current_relations.get(frozenset((item["from_cycle_id"], item["to_cycle_id"]))) != item
                       for item in group["relations"]):
                    raise WorkflowGroupError("merge-removal-forbidden", gid)
        expected_new: list[dict[str, Any]] = []
        old_relations = {} if plan["mode"] == "replace" else {
            (g["group_id"], r["from_cycle_id"], r["to_cycle_id"]): r
            for g in (old["groups"] if old else []) for r in g["relations"]}
        all_refs = [ref for group in doc["groups"] for relation in group["relations"]
                    for ref in relation["evidence_refs"]]
        if _evidence_size(root, all_refs) > MAX_EVIDENCE_TOTAL:
            raise WorkflowGroupError("evidence-total-size-limit")
        dirs = {member["cycle_id"]: _member_dir(root, campaign, member["cycle_id"])
                for group in doc["groups"] for member in group["members"]}
        bound_paths: dict[str, tuple[dict[str, str], Path]] = {}
        unique_bytes = 0
        for group in doc["groups"]:
            for relation in group["relations"]:
                key = (group["group_id"], relation["from_cycle_id"], relation["to_cycle_id"])
                if old_relations.get(key) == relation:
                    continue
                source, target = relation["from_cycle_id"], relation["to_cycle_id"]
                for ref in relation["evidence_refs"]:
                    evidence_path = ref["path"]
                    if evidence_path in bound_paths:
                        bound, owner = bound_paths[evidence_path]
                        _evidence_owner(root, evidence_path, (dirs[source], dirs[target]))
                        if owner not in (dirs[source], dirs[target]) or bound != ref:
                            raise WorkflowGroupError("evidence-binding-stale", str(evidence_path))
                    else:
                        size = _evidence_stat(root, _safe_relative(evidence_path)).st_size
                        if unique_bytes + size > MAX_EVIDENCE_TOTAL:
                            raise WorkflowGroupError("evidence-total-size-limit")
                        bound = _bind_evidence(root, campaign, ref, ((source, dirs[source]), (target, dirs[target])),
                                               remaining=MAX_EVIDENCE_TOTAL - unique_bytes)
                        owner = _evidence_owner(root, evidence_path, (dirs[source], dirs[target]))[1]
                        bound_paths[evidence_path] = (bound, owner)
                        unique_bytes += size
                    expected_new.append({"from_cycle_id": source, "to_cycle_id": target, **bound})
        if expected_new != plan["new_evidence"]:
            raise WorkflowGroupError("plan-evidence-mismatch")
        if _evidence_size(root, all_refs) > MAX_EVIDENCE_TOTAL:
            raise WorkflowGroupError("evidence-total-size-limit")
        latest = _regular(path, missing=True)
        if (_digest(latest) if latest is not None else None) != current_digest:
            raise WorkflowGroupError("declaration-preimage-conflict")
        # Evidence files are outside the producer admission lock. Rebind only
        # newly authored relations after the final size check, as close as
        # possible to the metadata replacement. Old historical refs may be
        # stale and must not block an unrelated merge.
        final_bytes = 0
        final_paths: set[str] = set()
        for expected in expected_new:
            evidence_path = expected["path"]
            if evidence_path in final_paths:
                continue
            source, target = expected["from_cycle_id"], expected["to_cycle_id"]
            ref = {key: expected[key] for key in EVIDENCE_FIELDS}
            size = _evidence_stat(root, _safe_relative(evidence_path)).st_size
            if final_bytes + size > MAX_EVIDENCE_TOTAL:
                raise WorkflowGroupError("evidence-total-size-limit")
            _bind_evidence(root, campaign, ref, ((source, dirs[source]), (target, dirs[target])),
                           remaining=MAX_EVIDENCE_TOTAL - final_bytes)
            final_bytes += size
            final_paths.add(evidence_path)
        latest = _regular(path, missing=True)
        if (_digest(latest) if latest is not None else None) != current_digest:
            raise WorkflowGroupError("declaration-preimage-conflict")
        producer._write_atomic(path, _bytes(doc))
        return {"status": "applied", "campaign_id": plan["campaign_id"], "sha256": plan["after_sha256"]}
    finally:
        admission._release_lock(root, lock)


def verify(root: Path, campaign_id: str, *, expected: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    campaign, directory, root_id, repo_id = _context(root, campaign_id)
    raw = _regular(directory / NAME)
    assert raw is not None
    digest = _digest(raw)
    if expected is not None and digest != expected:
        raise WorkflowGroupError("declaration-digest-mismatch")
    doc = _validate_document(root, campaign, directory, root_id, repo_id, _json(raw))
    stale: list[str] = []
    unverified: list[dict[str, str]] = []
    checked: dict[str, str] = {}
    read_bytes = 0
    for group in doc["groups"]:
        for relation in group["relations"]:
            for ref in relation["evidence_refs"]:
                rel = ref["path"]
                if rel not in checked:
                    relative = _safe_relative(rel)
                    try:
                        raw_file = _evidence_bytes(root, relative,
                                                   remaining=MAX_EVIDENCE_TOTAL - read_bytes)
                    except WorkflowGroupError as exc:
                        if exc.code == "file-missing":
                            checked[rel] = "stale"
                        elif exc.code == "evidence-file-size-limit":
                            checked[rel] = "size-limit"
                        elif exc.code == "read-budget":
                            checked[rel] = "read-budget"
                        elif exc.code == "file-changed-during-read":
                            checked[rel] = "changed-during-read"
                        else:
                            raise
                        continue
                    read_bytes += len(raw_file)
                    checked[rel] = _digest(raw_file)
                observed = checked[rel]
                if observed == "stale" or observed.startswith("sha256:") and observed != ref["sha256"]:
                    stale.append(rel)
                elif observed in ("read-budget", "size-limit", "changed-during-read"):
                    unverified.append({"path": rel, "reason": observed})
    return {"campaign_id": campaign_id, "sha256": digest, "groups": len(doc["groups"]),
            "stale_evidence": sorted(set(stale)), "unverified_evidence": unverified}


def group_for_cycle(root: Path, campaign_id: str, cycle_id: str) -> str | None:
    try:
        root = Path(root).resolve()
        campaign, directory, root_id, repo_id = _context(root, campaign_id)
        loaded = _load_existing(directory / NAME)
        doc = None if loaded is None else _validate_document(
            root, campaign, directory, root_id, repo_id, loaded)
    except (WorkflowGroupError, producer.ProducerError, OSError):
        return None
    if doc is None:
        return None
    for group in doc.get("groups", []):
        if any(member.get("cycle_id") == cycle_id for member in group.get("members", [])):
            return group.get("group_id")
    return None


def require_group_context(root: Path, campaign_id: str, group_id: str) -> None:
    """Validate an explicit group selection before begin changes a cycle."""
    root = Path(root).resolve()
    campaign, directory, root_id, repo_id = _context(root, campaign_id)
    raw = _regular(directory / NAME)
    assert raw is not None
    doc = _validate_document(root, campaign, directory, root_id, repo_id, _json(raw))
    if not any(group["group_id"] == group_id for group in doc["groups"]):
        raise WorkflowGroupError("group-unknown", group_id)


def preflight_join_locked(root: Path, campaign_id: str, group_id: str,
                          stage_label: str, *, cycle_id: str | None = None) -> None:
    """Check a prospective join before begin writes a cycle or campaign."""
    root = Path(root).resolve()
    if not admission.holds_lock(root):
        raise WorkflowGroupError("producer-lock-required")
    label = _text(stage_label, 40, "stage-label-invalid")
    campaign, directory, root_id, repo_id = _context(root, campaign_id)
    raw = _regular(directory / NAME)
    assert raw is not None
    doc = _validate_document(root, campaign, directory, root_id, repo_id, _json(raw))
    group = next((row for row in doc["groups"] if row["group_id"] == group_id), None)
    if group is None:
        raise WorkflowGroupError("group-unknown", group_id)
    if cycle_id is not None:
        if any(item["cycle_id"] == cycle_id for item in group["members"]):
            return
        if any(item["cycle_id"] == cycle_id for row in doc["groups"] for item in row["members"]):
            raise WorkflowGroupError("member-duplicate", cycle_id)
    if len(group["members"]) >= 64 or sum(len(row["members"]) for row in doc["groups"]) >= 256:
        raise WorkflowGroupError("member-count-invalid", group_id)
    # All cycle IDs have equal byte length. A placeholder checks the exact
    # projected declaration size without requiring a record that does not yet
    # exist; begin checks actual ID uniqueness before its first cycle write.
    group["members"].append({"cycle_id": cycle_id or "cyc_" + "0" * 32, "stage_label": label})
    if len(_bytes(doc)) > MAX_DECLARATION:
        raise WorkflowGroupError("declaration-size-limit")


def join_at_begin_locked(root: Path, campaign_id: str, cycle_id: str,
                         group_id: str, stage_label: str) -> None:
    """Append an explicitly inherited member while producer admission is held."""
    root = Path(root).resolve()
    if not admission.holds_lock(root):
        raise WorkflowGroupError("producer-lock-required")
    campaign, directory, root_id, repo_id = _context(root, campaign_id)
    path = directory / NAME
    raw = _regular(path)
    assert raw is not None
    doc = _validate_document(root, campaign, directory, root_id, repo_id, _json(raw))
    group = next((row for row in doc["groups"] if row["group_id"] == group_id), None)
    if group is None:
        raise WorkflowGroupError("group-unknown", group_id)
    if any(item["cycle_id"] == cycle_id for row in doc["groups"] for item in row["members"]):
        if any(item["cycle_id"] == cycle_id for item in group["members"]):
            return
        raise WorkflowGroupError("member-duplicate", cycle_id)
    group["members"].append({"cycle_id": cycle_id, "stage_label": _text(stage_label, 40, "stage-label-invalid")})
    _validate_document(root, campaign, directory, root_id, repo_id, doc)
    producer._write_atomic(path, _bytes(doc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "apply", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--artifact-root", required=True)
        if name != "apply":
            command.add_argument("--campaign-id", required=True)
        if name == "prepare":
            command.add_argument("--declaration", required=True)
            command.add_argument("--replace", action="store_true")
        if name == "apply":
            command.add_argument("--plan", required=True)
        if name == "verify":
            command.add_argument("--plan")
    args = parser.parse_args(argv)
    try:
        root = Path(args.artifact_root)
        if args.command == "prepare":
            proposal = _json(_regular(Path(args.declaration), cap=MAX_DECLARATION) or b"")
            result = prepare(root, args.campaign_id, proposal, replace=args.replace)
        elif args.command == "apply":
            result = apply(root, _json(_regular(Path(args.plan), cap=2 * MAX_DECLARATION) or b""))
        else:
            expected = None
            if args.plan:
                expected = _validate_plan(_json(_regular(Path(args.plan), cap=2 * MAX_DECLARATION) or b""))["after_sha256"]
            result = verify(root, args.campaign_id, expected=expected)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (WorkflowGroupError, producer.ProducerError, OSError) as exc:
        code = exc.code if isinstance(exc, (WorkflowGroupError, producer.ProducerError)) else "io-error"
        print(json.dumps({"status": "blocked", "code": code, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
