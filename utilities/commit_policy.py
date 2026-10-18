#!/usr/bin/env python3
"""Who may commit in a dispatched launch, decided once for every harness.

A registered owner commits its route's work, and so does a single-session
stage whose sealed route node is commit-expected (`worker_bootstrap.
stage_commit_enabled`). A stage that edits source in a linked worktree without
that seal is a no-commit worker (SD-69): it leaves the diff for its owner to
commit after the stage's own gate.

This used to live in the Codex wrapper only; the Claude wrapper re-derived
part of it for its allowlist and the OpenCode wrapper had none, so the same
stage was told not to commit on one harness and nothing on the others (audit
§4 #8, A6). Each wrapper now asks here and translates the answer into its own
means -- a sandbox profile, permission rules, or nothing beyond the prompt.
"""
from __future__ import annotations

import subprocess
import json
import os
import re
from pathlib import Path

from worker_bootstrap import stage_commit_enabled

PROMPT_CLAUSE = (
    "No-commit worker (SD-69):\n"
    "- You are a no-commit worker: produce source diff, tests, and evidence; do NOT `git commit`.\n"
    "- Your stage owner commits after this stage's own PASS gate and confirms diff attribution.\n\n"
)
MERGE_OBSERVATION_CLAUSE = (
    "Git operation in progress (merge/resolve state):\n"
    "- Observe the bound worktree and keep responsibility for the existing route.\n"
    "- Leave source, index, and merge metadata untouched; do not dispatch stages, commit, or publish.\n"
    "- Do not abort or complete the merge. Resume the existing route after the worktree is settled.\n\n"
)
REGISTRY_FRAGMENT = ",no_commit=1"
# The commands a no-commit worker is refused where its runtime can refuse a command.
COMMIT_COMMANDS = ("git commit",)
MERGE_MUTATION_COMMANDS = (
    "git add", "git commit", "git push", "git merge", "git rebase", "git cherry-pick",
    "git reset", "git checkout", "git switch", "git restore", "git apply",
    "git stash", "git clean", "git update-index", "gh pr create", "gh pr edit",
)
RUNTIME_PERMISSION_CONTEXT_ENV = "AGENT_OWNER_RUNTIME_PERMISSION_CONTEXT"


def worktree_mutating_write_scope(write_scope: str | None) -> bool:
    if not write_scope:
        return False
    return any(
        part.strip() in ("source/**", "source") or part.strip().startswith("source/")
        for part in write_scope.split(";")
    )


def worktree_git_dirs(worktree) -> tuple[Path, Path] | None:
    """Resolve (git-dir, git-common-dir) for a worktree, or None if unprovable."""
    try:
        root = Path(worktree).resolve()
        values = []
        for flag in ("--git-dir", "--git-common-dir"):
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", flag],
                text=True, capture_output=True, check=True,
            )
            value = Path(result.stdout.strip())
            values.append(value.resolve() if value.is_absolute() else (root / value).resolve())
        return values[0], values[1]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def is_linked_worktree(worktree, agent_home=None) -> bool:
    """Identify a real linked worktree from Git metadata, not path inequality."""
    dirs = worktree_git_dirs(worktree)
    if dirs is None:
        # A mutating stage whose Git topology cannot be proved is treated as
        # linked/protected, preserving the no-commit safety boundary.
        return True
    git_dir, common_dir = dirs
    return git_dir != common_dir


def git_operation(worktree) -> str | None:
    """Read the current Git operation without changing the worktree."""
    operation, known = git_operation_state(worktree)
    return operation if known else None


def git_operation_state(worktree) -> tuple[str | None, bool]:
    """Return the observed operation and whether Git state was readable.

    Permission projection must fail closed when Git metadata cannot be read:
    treating an unreadable ``MERGE_HEAD`` as an idle worktree would widen the
    same owner session while its safety state is unknown.
    """
    dirs = worktree_git_dirs(worktree)
    if dirs is None:
        return None, False
    git_dir, _common = dirs
    for operation, marker in (("merge", "MERGE_HEAD"), ("rebase", "rebase-merge"),
                              ("rebase", "rebase-apply"), ("cherry-pick", "CHERRY_PICK_HEAD")):
        try:
            (git_dir / marker).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return None, False
        return operation, True
    return None, True


def runtime_permission_projection(args) -> dict[str, object]:
    """Project shared mutation/publication state for launch and same-session resume."""
    operation, known = git_operation_state(getattr(args, "worktree", ""))
    observation_only = not known or operation is not None
    authorized_commit = (
        getattr(args, "worker_type", None) == "owner" or stage_commit_enabled(args)
    )
    return {
        "git_operation": operation,
        "git_state_known": known,
        "observation_only": observation_only,
        "may_commit": authorized_commit and not observation_only,
        "task_publish_scope": (
            task_publish_scope(args)
            if not observation_only and getattr(args, "worker_type", None) == "owner"
            else None
        ),
    }


def runtime_permission_context_env(args, **adapter_context) -> dict[str, str]:
    """Pass existing owner binding facts to its supervised same-session runtime.

    This is runtime transport for reprojection, not an authorization record:
    route authority remains the already-bound route and the shared projection
    above rechecks current Git state whenever the session resumes.
    """
    if (getattr(args, "worker_type", None) != "owner"
            or getattr(args, "dispatch_depth", None) != 1):
        return {RUNTIME_PERMISSION_CONTEXT_ENV: ""}
    binding = getattr(args, "owner_route_binding", None)
    route_file = (getattr(args, "route_file", None)
                  or getattr(binding, "route_file", None)
                  or os.environ.get("AGENT_OWNER_ROUTE_FILE"))
    route_id = (getattr(args, "route_id", None)
                or getattr(binding, "route_id", None)
                or os.environ.get("AGENT_OWNER_ROUTE_ID"))
    route_hash = (getattr(args, "route_hash", None)
                  or getattr(binding, "route_hash", None)
                  or os.environ.get("AGENT_OWNER_ROUTE_HASH"))
    if not route_file or not route_id or not route_hash:
        return {RUNTIME_PERMISSION_CONTEXT_ENV: ""}
    context = {
        "worker_type": "owner",
        "dispatch_depth": 1,
        "capability": str(getattr(args, "capability", "") or ""),
        "worktree": str(Path(str(getattr(args, "worktree", ""))).resolve()),
        "route_file": str(Path(str(route_file)).resolve()),
        "route_id": str(route_id),
        "route_hash": str(route_hash),
        "artifact_root": str(getattr(args, "artifact_root", "") or ""),
        "artifact_output_dir": str(
            getattr(args, "artifact_output_dir", None)
            or os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR", "")
        ),
        "attempt_id": str(getattr(args, "attempt_id", "") or ""),
    }
    context.update(adapter_context)
    return {RUNTIME_PERMISSION_CONTEXT_ENV: json.dumps(
        context, sort_keys=True, separators=(",", ":"))}


def read_runtime_permission_context(environ=None) -> dict[str, object] | None:
    """Read the internal owner-session projection context, rejecting corruption."""
    source = os.environ if environ is None else environ
    raw = source.get(RUNTIME_PERMISSION_CONTEXT_ENV, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("owner-runtime-permission-context-invalid") from exc
    required = ("worker_type", "dispatch_depth", "capability", "worktree",
                "route_file", "route_id", "route_hash", "attempt_id")
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(key), str) for key in required if key != "dispatch_depth")
            or value.get("worker_type") != "owner"
            or value.get("dispatch_depth") != 1
            or not value.get("route_file") or not value.get("route_id")
            or not value.get("route_hash") or not value.get("attempt_id")):
        raise ValueError("owner-runtime-permission-context-invalid")
    return value


def runtime_permission_context_is_current(context: dict[str, object]) -> bool:
    """Confirm that a resumed session still carries its exact sealed route."""
    try:
        route = json.loads(Path(str(context["route_file"])).read_text(encoding="utf-8"))
        from route_identity import route_hash as canonical_route_hash, route_id_from_hash
        actual_hash = canonical_route_hash(route)
        return (
            route.get("route_hash") == context.get("route_hash") == actual_hash
            and route.get("route_id") == context.get("route_id") == route_id_from_hash(actual_hash)
            and Path(str(route.get("cwd") or "")).resolve() == Path(str(context["worktree"])).resolve()
            and route.get("capability") == context.get("capability")
            and Path(str(route.get("artifact_root") or "")).resolve()
                == Path(str(context.get("artifact_root") or "")).resolve()
        )
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        return False


def may_commit(args) -> bool:
    """An owner or commit-expected stage, when Git has no operation in progress."""
    authorized = getattr(args, "worker_type", None) == "owner" or stage_commit_enabled(args)
    operation, known = git_operation_state(getattr(args, "worktree", ""))
    return authorized and known and operation is None


def task_publish_scope(args) -> dict[str, str] | None:
    """Resolve the existing approval for a code owner to publish this task PR.

    The source of authority is the sealed complete entry scope. The approved
    task itself determines whether the owner performs a publication command;
    request wording is not a second authorization check. Stage/review workers,
    other capabilities, and protected/default refs receive no scope.
    """
    if (getattr(args, "worker_type", None) != "owner"
            or getattr(args, "dispatch_depth", None) != 1):
        return None
    binding = getattr(args, "owner_route_binding", None)
    route_file = (getattr(args, "route_file", None)
                  or getattr(binding, "route_file", None))
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    try:
        from route_identity import route_hash as canonical_route_hash, route_id_from_hash
        actual_route_hash = canonical_route_hash(route)
        if (route.get("route_hash") != actual_route_hash
                or route.get("route_id") != route_id_from_hash(actual_route_hash)):
            return None
    except (ImportError, TypeError, ValueError):
        return None
    route_id = getattr(args, "route_id", None) or getattr(binding, "route_id", None)
    route_hash = getattr(args, "route_hash", None) or getattr(binding, "route_hash", None)
    worktree = Path(str(getattr(args, "worktree", ""))).resolve(strict=False)
    artifact_root = route.get("artifact_root")
    if (route.get("capability") != "autopilot-code"
            or not isinstance(artifact_root, str) or not artifact_root
            or (route_id and route.get("route_id") != route_id)
            or (route_hash and route.get("route_hash") != route_hash)
            or Path(str(route.get("cwd") or "")).resolve(strict=False) != worktree
            or route.get("entry_scope_contract_version") != 1
            or route.get("entry_execution_scope") != "complete"):
        return None
    output_value = (getattr(args, "artifact_output_dir", None)
                    or os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR"))
    if not output_value and os.environ.get("AGENT_ARTIFACT_CYCLE_DIR"):
        output_value = str(Path(os.environ["AGENT_ARTIFACT_CYCLE_DIR"]) / "artifacts")
    if not isinstance(output_value, str) or not output_value:
        return None
    output_dir = Path(output_value).resolve(strict=False)
    artifact_root_path = Path(artifact_root).resolve(strict=False)
    try:
        output_dir.relative_to(artifact_root_path)
    except ValueError:
        return None
    try:
        operation, known = git_operation_state(worktree)
        if not known or operation is not None:
            return None
        symbolic = subprocess.run(["git", "-C", str(worktree), "symbolic-ref", "-q", "HEAD"],
                                  text=True, capture_output=True, check=True, timeout=5).stdout.strip()
        if not symbolic.startswith("refs/heads/"):
            return None
        branch = symbolic[len("refs/heads/"):]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch):
            return None
        default_ref = subprocess.run(
            ["git", "-C", str(worktree), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
            text=True, capture_output=True, check=False, timeout=5,
        )
        base = (default_ref.stdout.strip()[len("origin/"):]
                if default_ref.returncode == 0 and default_ref.stdout.strip().startswith("origin/")
                else "main")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", base):
            return None
        if branch in {"HEAD", "main", "master", "develop", "trunk", base}:
            return None
        origin = subprocess.run(["git", "-C", str(worktree), "remote", "get-url", "origin"],
                                text=True, capture_output=True, check=True, timeout=5)
        head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                              text=True, capture_output=True, check=True, timeout=5).stdout.strip()
        branch_head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", f"refs/heads/{branch}"],
            text=True, capture_output=True, check=True, timeout=5,
        ).stdout.strip()
        if not origin.stdout.strip() or branch_head != head:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return {"branch": branch, "base": base, "route_id": str(route["route_id"]),
            "artifact_root": str(artifact_root_path),
            "body_file": str(output_dir / "dev_logs" / "pr-description.md")}


def task_publish_commands(scope: dict[str, str]) -> tuple[str, ...]:
    """Exact non-force task-branch push and current-branch PR/CI commands."""
    branch = scope["branch"]
    base = scope["base"]
    return (
        f"git push -u origin HEAD:refs/heads/{branch}",
        f"git push origin HEAD:refs/heads/{branch}",
        f"gh pr list --head {branch} --json number,url,headRefOid",
        "gh pr view",
        "gh pr view --json url,number,headRefOid,statusCheckRollup",
        "gh pr checks --watch",
        f"gh pr create --base {base} --head {branch} --body-file {scope['body_file']}",
        f"gh pr edit --body-file {scope['body_file']}",
    )


def no_commit_stage(args) -> bool:
    """A source-editing stage in a linked worktree that its route did not let commit."""
    return (
        getattr(args, "worker_type", None) == "stage" and not stage_commit_enabled(args)
        and worktree_mutating_write_scope(getattr(args, "write_scope", None))
        and is_linked_worktree(getattr(args, "worktree", ""), getattr(args, "agent_home", None))
    )


def commit_git_metadata_dirs(args) -> tuple[Path, ...]:
    """Primary Git metadata dirs a committing linked-worktree run writes.

    The per-worktree git dir plus the common dir's ``objects``/``refs``/``logs``.
    The common-dir root stays out so ``hooks/`` and ``config`` remain read-only:
    a worker must not plant code a later unsandboxed session would execute.
    Nothing for a launch that may not commit or for a primary checkout.
    """
    if not may_commit(args):
        return ()
    dirs = worktree_git_dirs(getattr(args, "worktree", ""))
    if dirs is None:
        return ()
    git_dir, common_dir = dirs
    if git_dir == common_dir:
        return ()
    return (git_dir, common_dir / "objects", common_dir / "refs", common_dir / "logs")


def prompt_clause(args) -> str:
    operation, known = git_operation_state(getattr(args, "worktree", ""))
    if not known or operation is not None:
        return MERGE_OBSERVATION_CLAUSE
    return PROMPT_CLAUSE if no_commit_stage(args) else ""


def registry_fragment(args) -> str:
    return REGISTRY_FRAGMENT if no_commit_stage(args) else ""
