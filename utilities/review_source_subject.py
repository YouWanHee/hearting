#!/usr/bin/env python3
"""Automatic content identity for implementation-review attempts.

The content digest describes the effective source tree. Attempt, route and
publication identity live in a separate immutable binding beside jobs.log.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import time

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"att-[A-Za-z0-9._-]{1,240}\Z")
_EXCLUDED_PARTS = frozenset({
    ".git", ".agent_reports", ".claude_reports", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", "dist",
    "build", "coverage", "target", ".next", "dev_logs", "test_logs", "evidence",
    "_internal", "shards", "analysis_project",
})
_ARTIFACT_ROOT_FILES = frozenset({
    "plan.md", "checklist.md", "pipeline_summary.md", "final_report.md",
})
_SEMANTIC_MARKDOWN_ROOTS = frozenset({"core", "capabilities", "roles", "adapters"})
_SOURCE_SCOPE_TOKENS = frozenset({"source/**", "source-alternative/**", "tests/**"})


class SourceSubjectError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason + (":" + detail if detail else ""))
        self.reason = reason
        self.detail = detail


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    env = dict(os.environ)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, check=False)
    if check and result.returncode:
        raise SourceSubjectError("review-source-git-unavailable",
                                 result.stderr.decode("utf-8", "replace")[-400:])
    return result.stdout


def is_implementation_review(route: dict, node: dict) -> bool:
    if node.get("kind") != "review-worker":
        return False
    derived_impl_review = (
        node.get("unit") == "qa/code-review"
        and (node.get("id") == "impl-review"
             or node.get("parallel_anchor") == "impl-review"
             or (isinstance(node.get("plan_stage"), dict)
                 and node["plan_stage"].get("template") == "impl-review"))
    )
    return (node.get("completion_gate") == "code-impl-review"
            or derived_impl_review
            or node.get("source_review_kind") == "implementation")


def _ancestor_ids(route: dict, node: dict) -> list[str]:
    by_id = {str(item.get("id")): item for item in route.get("nodes", []) if isinstance(item, dict)}
    seen: set[str] = set()
    ordered: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        current = by_id.get(node_id)
        if current is None:
            return
        for parent in current.get("depends_on", ()) or ():
            visit(str(parent))
        if node_id != str(node.get("id")):
            ordered.append(node_id)

    visit(str(node.get("id")))
    return ordered


def source_scopes(route: dict, node: dict) -> tuple[str, ...]:
    """Resolve the source-producing ancestor scopes sealed by the route."""
    by_id = {str(item.get("id")): item for item in route.get("nodes", []) if isinstance(item, dict)}
    ancestors = _ancestor_ids(route, node)
    candidates = [by_id[item] for item in ancestors if item in by_id]
    candidates.append(node)
    if is_implementation_review(route, node):
        candidates.extend(item for item in route.get("nodes", [])
                          if item.get("completion_gate") == "code-execute" or item.get("id") == "execute")
    scopes = sorted({str(scope) for item in candidates for scope in (item.get("write_scope") or ())
                     if isinstance(scope, str) and scope
                     and str(scope) not in _ARTIFACT_ROOT_FILES
                     and PurePosixPath(str(scope).split("*", 1)[0].rstrip("/")).parts[:1]
                     not in (("dev_logs",), ("test_logs",), ("evidence",), ("_internal",), ("shards",))})
    if is_implementation_review(route, node) and not scopes:
        # Historical implementation-review gates predate explicit source scopes.
        return ("source/**",)
    return tuple(scopes)


def has_source_subject(route: dict, node: dict) -> bool:
    if is_implementation_review(route, node):
        return True
    if node.get("completion_gate") == "code-execute" or node.get("id") == "execute":
        return True
    return any(scope in _SOURCE_SCOPE_TOKENS
               for scope in node.get("write_scope") or () if isinstance(scope, str))


def _explicit_match(path: str, scopes: tuple[str, ...]) -> bool:
    import fnmatch
    for scope in scopes:
        if scope in _SOURCE_SCOPE_TOKENS:
            return True
        pattern = scope.rstrip("/")
        if fnmatch.fnmatchcase(path, pattern) or path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _excluded_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in _EXCLUDED_PARTS for part in parts)


def _pure_document(path: str, scopes: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if name in _ARTIFACT_ROOT_FILES:
        return True
    if _explicit_match(path, tuple(scope for scope in scopes if scope not in _SOURCE_SCOPE_TOKENS)):
        return False
    if parts and parts[0] in _SEMANTIC_MARKDOWN_ROOTS:
        return False
    if len(parts) >= 2 and parts[0] in {"doc", "docs"}:
        return True
    if parts and parts[0] not in _SEMANTIC_MARKDOWN_ROOTS and name.endswith((".md", ".rst")):
        return True
    if name.startswith(("readme", "changelog", "changes")) and name.endswith(".txt"):
        return True
    return False


def _candidate(path: str, scopes: tuple[str, ...]) -> bool:
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        return False
    if _excluded_path(path) or _pure_document(path, scopes):
        return False
    return _explicit_match(path, scopes) or any(scope in _SOURCE_SCOPE_TOKENS for scope in scopes)


def _index_entries(root: Path) -> tuple[dict[str, tuple[int, str]], set[str]]:
    raw = _git(root, "ls-files", "--stage", "-z")
    staged: dict[str, tuple[int, str]] = {}
    tracked: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        head, sep, raw_path = record.partition(b"\t")
        if not sep:
            raise SourceSubjectError("review-source-index-invalid")
        fields = head.split()
        if len(fields) != 3:
            raise SourceSubjectError("review-source-index-invalid")
        if fields[2] != b"0":
            raise SourceSubjectError("review-source-index-unmerged", os.fsdecode(raw_path))
        path = os.fsdecode(raw_path)
        mode = int(fields[0], 8)
        oid = fields[1].decode("ascii", "strict")
        staged[path] = (mode, oid)
        tracked.add(path)
    untracked = {os.fsdecode(item) for item in _git(
        root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if item}
    return staged, tracked | untracked


def _index_tree_digest(root: Path, staged: dict[str, tuple[int, str]]) -> str:
    """Hash the index tree without `git write-tree` or an index/object write."""
    object_format = _git(root, "rev-parse", "--show-object-format").decode("ascii", "strict").strip()
    if object_format not in {"sha1", "sha256"}:
        raise SourceSubjectError("review-source-index-object-format-unsupported", object_format)
    hash_size = 20 if object_format == "sha1" else 32
    tree: dict = {}
    for relative, (mode, oid) in staged.items():
        parts = PurePosixPath(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise SourceSubjectError("review-source-index-path-invalid", relative)
        branch = tree
        for part in parts[:-1]:
            existing = branch.setdefault(part, {})
            if not isinstance(existing, dict):
                raise SourceSubjectError("review-source-index-path-conflict", relative)
            branch = existing
        if parts[-1] in branch:
            raise SourceSubjectError("review-source-index-path-conflict", relative)
        try:
            object_id = bytes.fromhex(oid)
        except ValueError as exc:
            raise SourceSubjectError("review-source-index-oid-invalid", relative) from exc
        if len(object_id) != hash_size:
            raise SourceSubjectError("review-source-index-oid-invalid", relative)
        branch[parts[-1]] = (mode, object_id)

    def encode_tree(entries: dict) -> bytes:
        ordered = []
        for name, value in entries.items():
            is_directory = isinstance(value, dict)
            if is_directory:
                mode, object_id = 0o40000, bytes.fromhex(encode_tree(value).decode("ascii"))
            else:
                mode, object_id = value
            raw_name = os.fsencode(name)
            ordered.append((raw_name + (b"/" if is_directory else b"\0"),
                            raw_name, mode, object_id))
        body = b"".join(
            format(mode, "o").encode("ascii") + b" " + name + b"\0" + object_id
            for _key, name, mode, object_id in sorted(ordered, key=lambda item: item[0])
        )
        return hashlib.new(object_format,
            b"tree " + str(len(body)).encode("ascii") + b"\0" + body).hexdigest().encode("ascii")

    return encode_tree(tree).decode("ascii")


def _safe_checkout_path(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise SourceSubjectError("review-source-parent-symlink", relative)
        except FileNotFoundError:
            return path
    return path


def _stat_key(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path: Path) -> tuple[bytes, tuple[int, ...]]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SourceSubjectError("review-source-file-kind-changed", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_key(before) != _stat_key(opened):
            raise SourceSubjectError("review-source-file-changed", str(path))
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after_fd = os.fstat(fd)
    finally:
        os.close(fd)
    after = path.lstat()
    if _stat_key(opened) != _stat_key(after_fd) or _stat_key(after_fd) != _stat_key(after):
        raise SourceSubjectError("review-source-file-changed", str(path))
    return b"".join(chunks), _stat_key(after)


def _snapshot_once(root: Path, scopes: tuple[str, ...], *, depth: int = 0) -> tuple[list[dict], dict]:
    if depth > 8:
        raise SourceSubjectError("review-source-submodule-depth")
    try:
        head_before = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        status_before = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        staged, paths = _index_entries(root)
        index_before = _index_tree_digest(root, staged)
    except SourceSubjectError:
        raise
    manifest: list[dict] = []
    observations: list[tuple[Path, tuple[int, ...] | None]] = []
    for relative in sorted(paths, key=os.fsencode):
        if not _candidate(relative, scopes):
            continue
        path = _safe_checkout_path(root, relative)
        index_mode, index_oid = staged.get(relative, (0, ""))
        try:
            info = path.lstat()
        except FileNotFoundError:
            manifest.append({"path": relative, "kind": "deleted", "mode": index_mode,
                             "content_sha256": None})
            observations.append((path, None))
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target_before = os.readlink(path)
            after = path.lstat()
            if _stat_key(info) != _stat_key(after) or target_before != os.readlink(path):
                raise SourceSubjectError("review-source-file-changed", relative)
            data = os.fsencode(target_before)
            manifest.append({"path": relative, "kind": "symlink", "mode": mode,
                             "content_sha256": _sha(data)})
            observations.append((path, _stat_key(after)))
        elif index_mode == 0o160000 or stat.S_ISDIR(info.st_mode):
            if not stat.S_ISDIR(info.st_mode) or not (path / ".git").exists():
                raise SourceSubjectError("review-source-submodule-unreadable", relative)
            nested, provenance = _snapshot_once(path, ("source/**",), depth=depth + 1)
            nested_digest = _sha(_canonical(nested))
            manifest.append({"path": relative, "kind": "gitlink", "mode": mode,
                             "gitlink_oid": index_oid, "content_sha256": nested_digest})
            observations.append((path, _stat_key(info)))
        elif stat.S_ISREG(info.st_mode):
            data, observed = _read_regular(path)
            manifest.append({"path": relative, "kind": "file", "mode": mode,
                             "content_sha256": _sha(data)})
            observations.append((path, observed))
        else:
            raise SourceSubjectError("review-source-file-kind-unsupported", relative)
    for path, expected in observations:
        try:
            actual = _stat_key(path.lstat())
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise SourceSubjectError("review-source-file-changed", str(path))
    head_after = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    staged_after, _paths_after = _index_entries(root)
    index_after = _index_tree_digest(root, staged_after)
    status_after = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if (head_before, index_before, status_before) != (head_after, index_after, status_after):
        raise SourceSubjectError("review-source-snapshot-raced")
    provenance = {"head": head_after, "index_tree": index_after,
                  "status_sha256": _sha(status_after)}
    return manifest, provenance


def observe(route: dict, node: dict, *, worktree: str | Path | None = None) -> dict:
    """Return a stable effective-tree manifest and content-only digest."""
    root = Path(worktree or route.get("cwd", "")).resolve()
    if not root.is_dir():
        raise SourceSubjectError("review-source-worktree-unavailable", str(root))
    scopes = source_scopes(route, node)
    if not scopes:
        raise SourceSubjectError("review-source-scope-unavailable", str(node.get("id", "")))
    last_error: SourceSubjectError | None = None
    for attempt in range(3):
        try:
            manifest, provenance = _snapshot_once(root, scopes)
            digest = _sha(_canonical({"schema_version": SCHEMA_VERSION, "manifest": manifest}))
            return {"schema_version": SCHEMA_VERSION, "digest": digest, "manifest": manifest,
                    "scope": list(scopes), "provenance": provenance}
        except SourceSubjectError as exc:
            last_error = exc
            if exc.reason not in {"review-source-file-changed", "review-source-snapshot-raced"}:
                raise
            if attempt < 2:
                time.sleep(0.005)
    raise last_error or SourceSubjectError("review-source-snapshot-raced")


def _subject_directory(jobs: str | Path) -> Path:
    return Path(jobs).resolve().parent / "review-subjects"


def binding_path(jobs: str | Path, attempt_id: str) -> Path:
    if not _SAFE_ID.fullmatch(str(attempt_id)):
        raise SourceSubjectError("review-source-attempt-id-invalid", str(attempt_id))
    return _subject_directory(jobs) / f"{attempt_id}.json"


def _binding_digest(record: dict) -> str:
    return _sha(_canonical({k: v for k, v in record.items() if k != "binding_digest"}))


def read_binding_record(jobs: str | Path, attempt_id: str, *, route_id=None,
                        route_hash=None, node_id=None) -> dict | None:
    """Read a sidecar for exact replacement lineage without inferring legacy rows."""
    path = binding_path(jobs, attempt_id)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceSubjectError("review-source-binding-unreadable", attempt_id) from exc
    manifest_digest = _sha(_canonical({"schema_version": SCHEMA_VERSION, "manifest": record.get("manifest")}))
    if (record.get("schema_version") != SCHEMA_VERSION
            or record.get("attempt_id") != attempt_id
            or (route_id is not None and record.get("route_id") != route_id)
            or (route_hash is not None and record.get("route_hash") != route_hash)
            or (node_id is not None and record.get("node_id") != node_id)
            or record.get("binding_digest") != _binding_digest(record)
            or record.get("subject_digest") != manifest_digest):
        raise SourceSubjectError("review-source-binding-identity-mismatch", attempt_id)
    return record


def _write_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical(payload) + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceSubjectError("review-source-binding-unreadable", str(path)) from exc
        if existing != payload:
            raise SourceSubjectError("review-source-binding-conflict", str(path))
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def seal_attempt(jobs: str | Path, route: dict, node: dict, attempt_id: str, *,
                 worktree: str | Path | None = None, predecessor_attempt_id: str | None = None,
                 expected_subject_digest: str | None = None) -> dict:
    """Bind a new implementation-review attempt, inheriting exact retry lineage."""
    if not is_implementation_review(route, node):
        return {}
    path = binding_path(jobs, attempt_id)
    if predecessor_attempt_id:
        predecessor = read_binding_record(jobs, predecessor_attempt_id,
            route_id=route["route_id"], route_hash=route["route_hash"], node_id=node["id"])
        if predecessor is None:
            return {}  # A legacy unknown attempt stays unknown through replacement.
        current = observe(route, node, worktree=worktree)
        if expected_subject_digest and current["digest"] != expected_subject_digest:
            raise SourceSubjectError("review-source-admission-subject-changed", attempt_id)
        expected_epoch = source_epoch(route, node, current["digest"],
                                      Path(jobs).resolve().parent / "completion" / route["route_id"])
        if current["digest"] != predecessor["subject_digest"] or expected_epoch != predecessor["source_epoch"]:
            raise SourceSubjectError("review-source-replacement-subject-changed", predecessor_attempt_id)
        record = {k: v for k, v in predecessor.items() if k not in {"attempt_id", "binding_digest"}}
        record.update(attempt_id=attempt_id, predecessor_attempt_id=predecessor_attempt_id)
    else:
        current = observe(route, node, worktree=worktree)
        if expected_subject_digest and current["digest"] != expected_subject_digest:
            raise SourceSubjectError("review-source-admission-subject-changed", attempt_id)
        completion_dir = Path(jobs).resolve().parent / "completion" / route["route_id"]
        record = {
            "schema_version": SCHEMA_VERSION,
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "node_id": node["id"], "attempt_id": attempt_id,
            "subject_digest": current["digest"],
            "source_epoch": source_epoch(route, node, current["digest"], completion_dir),
            "scope": current["scope"], "manifest": current["manifest"],
            "provenance": current["provenance"],
        }
    record["binding_digest"] = _binding_digest(record)
    _write_once(path, record)
    return {"review_subject_digest": record["subject_digest"],
            "review_subject_binding_digest": record["binding_digest"]}


def read_binding(jobs: str | Path, metadata: dict, *, required: bool = True) -> dict | None:
    """Read and validate the exact write-once attempt binding; legacy absence is unknown."""
    attempt_id = str(metadata.get("attempt_id", ""))
    digest = metadata.get("review_subject_digest")
    binding_digest = metadata.get("review_subject_binding_digest")
    if not digest and not binding_digest:
        if required:
            raise SourceSubjectError("review-source-binding-missing", attempt_id)
        return None
    if not digest or not binding_digest:
        raise SourceSubjectError("review-source-binding-incomplete", attempt_id)
    record = read_binding_record(jobs, attempt_id, route_id=metadata.get("route_id"),
        route_hash=metadata.get("route_hash"), node_id=metadata.get("route_node"))
    if (record is None or record.get("subject_digest") != digest
            or record.get("binding_digest") != binding_digest):
        raise SourceSubjectError("review-source-binding-identity-mismatch", attempt_id)
    return record


def annotate_rows(jobs: str | Path, rows):
    """Add verified subject labels to an in-memory round census only."""
    result = []
    for status, metadata in rows:
        binding = read_binding(jobs, metadata, required=False)
        result.append((status, metadata if binding is None else {
            **metadata, "_review_subject_digest": binding["subject_digest"]
        }))
    return result


def validate_attempt(jobs: str | Path, fields: list[str], metadata: dict, *,
                     require_new: bool = False) -> dict | None:
    """Registration/launch fence: exact binding integrity and effective bytes."""
    likely_review = (metadata.get("completion_gate") == "code-impl-review"
                     or metadata.get("unit") == "qa/code-review")
    if not likely_review:
        return None
    if (not metadata.get("review_subject_digest")
            and not metadata.get("review_subject_binding_digest")
            and not metadata.get("route_file") and not require_new):
        return None  # A legacy unknown row is never inferred from HEAD or route position.
    route_file = Path(str(metadata.get("route_file", "")))
    if not route_file.is_absolute() or not route_file.is_file():
        raise SourceSubjectError("review-source-route-unavailable", str(route_file))
    try:
        route = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceSubjectError("review-source-route-unavailable", str(route_file)) from exc
    if (route.get("route_id") != metadata.get("route_id")
            or route.get("route_hash") != metadata.get("route_hash")):
        raise SourceSubjectError("review-source-route-identity-mismatch")
    node = next((item for item in route.get("nodes", [])
                 if item.get("id") == metadata.get("route_node")), None)
    if node is None or not is_implementation_review(route, node):
        return None
    record = read_binding(jobs, metadata, required=require_new)
    if record is None:
        return None  # A pre-feature registry row remains unknown, never inferred.
    current = observe(route, node, worktree=fields[3])
    current_epoch = source_epoch(route, node, current["digest"],
                                  Path(jobs).resolve().parent / "completion" / route["route_id"])
    if current["digest"] != record["subject_digest"] or current_epoch != record["source_epoch"]:
        raise SourceSubjectError("review-source-subject-changed-before-launch", metadata.get("attempt_id", ""))
    return record


def source_epoch(route: dict, node: dict, digest: str, completion_dir: str | Path) -> int:
    """Predict the route-wide source epoch from append-only marker history.

    Epoch changes only when source content changes. Marker sequence, report
    publication and identical-content commits therefore do not create a new
    source generation; returning to earlier bytes after a different source
    does create a new generation and cannot revive an old PASS. The maximum
    epoch across source-consuming markers carries across sibling branches too.
    """
    directory = Path(completion_dir)
    generations = [route]
    if route.get("continuation_contract_version") == 1:
        from route_lineage import RouteLineageError, verified_route_lineage
        try:
            generations = verified_route_lineage(route, artifact_root=route.get("artifact_root"))
        except RouteLineageError as exc:
            raise SourceSubjectError("review-source-lineage-unverifiable", exc.code) from exc
    completion_root = directory.parent
    observations = []
    for generation in generations:
        generation_dir = completion_root / str(generation.get("route_id", ""))
        for candidate in generation.get("nodes", []):
            node_id = str(candidate.get("id", ""))
            if not node_id:
                continue
            marker_path = generation_dir / f"{node_id}.json"
            try:
                info = marker_path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise SourceSubjectError("review-source-marker-kind-invalid", node_id)
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                subject = marker.get("source_subject")
                if not isinstance(subject, dict):
                    continue  # Legacy marker: preserve it as unknown history.
                if (marker.get("route_id") != generation.get("route_id")
                        or marker.get("route_hash") != generation.get("route_hash")
                        or marker.get("node_id") != node_id
                        or marker.get("completion_gate") != candidate.get("completion_gate")):
                    raise SourceSubjectError("review-source-marker-identity-invalid", node_id)
                sequence = marker.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                    raise SourceSubjectError("review-source-marker-sequence-invalid", node_id)
                history_path = generation_dir / f"{node_id}.{sequence}.json"
                history = json.loads(history_path.read_text(encoding="utf-8"))
                if history != marker:
                    raise SourceSubjectError("review-source-marker-history-conflict", node_id)
                if (isinstance(subject.get("epoch"), int)
                        and not isinstance(subject.get("epoch"), bool)
                        and isinstance(subject.get("digest"), str)):
                    observations.append((int(subject["epoch"]), subject["digest"]))
                else:
                    raise SourceSubjectError("review-source-marker-subject-invalid", node_id)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, SourceSubjectError):
                    raise
                raise SourceSubjectError("review-source-marker-unreadable", node_id) from exc
    if not observations:
        return 0
    maximum = max(epoch for epoch, _subject in observations)
    latest_subjects = {subject for epoch, subject in observations if epoch == maximum}
    if len(latest_subjects) != 1:
        raise SourceSubjectError("review-source-generation-conflict", str(maximum))
    latest = next(iter(latest_subjects))
    return maximum + (digest != latest)


def marker_subject(route: dict, node: dict, *, attempt_id: str | None = None,
                   attempt_metadata: dict | None = None, jobs: str | Path | None = None,
                   worktree: str | Path | None = None, completion_dir: str | Path) -> dict | None:
    """Subject fields to put on a newly published completion marker."""
    if not has_source_subject(route, node):
        return None
    if is_implementation_review(route, node):
        if not jobs or not attempt_metadata:
            return None
        record = read_binding(jobs, attempt_metadata, required=False)
        if record is None:
            return None
        current = observe(route, node, worktree=worktree)
        epoch = source_epoch(route, node, current["digest"], completion_dir)
        if current["digest"] != record["subject_digest"] or epoch != record["source_epoch"]:
            raise SourceSubjectError("review-source-subject-changed-before-completion", str(attempt_id or ""))
        return {"schema_version": SCHEMA_VERSION, "digest": record["subject_digest"],
                "epoch": record["source_epoch"], "binding_digest": record["binding_digest"]}
    current = observe(route, node, worktree=worktree)
    epoch = source_epoch(route, node, current["digest"], completion_dir)
    return {"schema_version": SCHEMA_VERSION, "digest": current["digest"], "epoch": epoch,
            "manifest_sha256": _sha(_canonical(current["manifest"]))}


def marker_currency(route: dict, node: dict, marker: dict, completion_dir: str | Path) -> tuple[str, str] | None:
    """Current source identity for a marker, or None for routes outside this contract."""
    # Other scoped markers contribute source generations to the append-only
    # epoch history, but only implementation reviews consume that source as
    # evidence. Invalidating plan or execute markers here would make an
    # unrelated empty Markdown scope stale when a later implementation epoch
    # advances, and can block normal replacement/replay.
    if not is_implementation_review(route, node):
        return None
    subject = marker.get("source_subject")
    if not isinstance(subject, dict):
        return "superseded", "completion-source-subject-unknown"
    try:
        current = observe(route, node)
        epoch = source_epoch(route, node, current["digest"], completion_dir)
    except SourceSubjectError as exc:
        return "completion-evidence-unreadable", exc.reason
    if (subject.get("schema_version") != SCHEMA_VERSION
            or subject.get("digest") != current["digest"]
            or subject.get("epoch") != epoch):
        reason = "completion-review-source-stale" if is_implementation_review(route, node) else "completion-source-stale"
        return "superseded", reason
    if is_implementation_review(route, node):
        attempt_id = marker.get("attempt_id")
        binding_digest = subject.get("binding_digest")
        if not attempt_id or not binding_digest:
            return "superseded", "completion-review-source-binding-unknown"
        state_root = Path(completion_dir).parent.parent
        jobs_path = Path(os.environ.get("AGENT_DISPATCH_JOBS") or state_root / "jobs.log").resolve()
        exact_rows = []
        try:
            for line in jobs_path.read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
                if metadata.get("attempt_id") == attempt_id:
                    exact_rows.append(metadata)
        except OSError:
            return "integrity-broken:identity-mismatch", "completion-review-source-registry-missing"
        if len(exact_rows) != 1:
            return "integrity-broken:identity-mismatch", "completion-review-source-row-not-exact"
        metadata = exact_rows[0]
        if (metadata.get("route_id") != route.get("route_id")
                or metadata.get("route_hash") != route.get("route_hash")
                or metadata.get("route_node") != node.get("id")
                or metadata.get("review_subject_digest") != subject.get("digest")
                or metadata.get("review_subject_binding_digest") != binding_digest):
            return "integrity-broken:identity-mismatch", "completion-review-source-row-binding-mismatch"
        try:
            record = read_binding_record(jobs_path, attempt_id,
                route_id=route.get("route_id"), route_hash=route.get("route_hash"),
                node_id=node.get("id"))
        except SourceSubjectError:
            return "integrity-broken:identity-mismatch", "completion-review-source-binding-missing"
        if (record.get("binding_digest") != binding_digest or _binding_digest(record) != binding_digest
                or record.get("subject_digest") != subject.get("digest")
                or record.get("attempt_id") != attempt_id
                or record.get("route_id") != route.get("route_id")
                or record.get("route_hash") != route.get("route_hash")
                or record.get("node_id") != node.get("id")):
            return "integrity-broken:identity-mismatch", "completion-review-source-binding-invalid"
    return None
