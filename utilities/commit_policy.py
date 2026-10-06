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
from pathlib import Path

from worker_bootstrap import stage_commit_enabled

PROMPT_CLAUSE = (
    "No-commit worker (SD-69):\n"
    "- You are a no-commit worker: produce source diff, tests, and evidence; do NOT `git commit`.\n"
    "- Your stage owner commits after this stage's own PASS gate and confirms diff attribution.\n\n"
)
REGISTRY_FRAGMENT = ",no_commit=1"
# The commands a no-commit worker is refused where its runtime can refuse a command.
COMMIT_COMMANDS = ("git commit",)


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


def may_commit(args) -> bool:
    """An owner, or a stage whose sealed node is commit-expected."""
    return getattr(args, "worker_type", None) == "owner" or stage_commit_enabled(args)


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
    return PROMPT_CLAUSE if no_commit_stage(args) else ""


def registry_fragment(args) -> str:
    return REGISTRY_FRAGMENT if no_commit_stage(args) else ""
