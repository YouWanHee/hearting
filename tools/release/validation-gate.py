#!/usr/bin/env python3
"""Bind release authority to a trusted Checks result or local validation.

Read only GitHub's event file and runner context; never fetch artifacts, poll
another workflow, or execute candidate code to decide whether it is trusted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Mapping


class Rejected(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Rejected(reason)


def _sha(value: object) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
             "release commit must be a full SHA")
    return str(value)


def admit(event: dict, context: Mapping[str, str]) -> dict[str, str]:
    repository = context.get("GITHUB_REPOSITORY")
    repository_id = context.get("GITHUB_REPOSITORY_ID")
    _require(bool(repository) and bool(repository_id), "missing repository identity")

    def trusted(value: object) -> bool:
        return (isinstance(value, dict) and value.get("full_name") == repository
                and str(value.get("id")) == repository_id)

    _require(trusted(event.get("repository")), "foreign event repository")
    kind = context.get("GITHUB_EVENT_NAME")
    if kind == "workflow_run":
        run = event.get("workflow_run") or {}
        _require(event.get("action") == "completed" and run.get("status") == "completed",
                 "Checks has not completed")
        _require(run.get("name") == "Checks" and run.get("path") == ".github/workflows/checks.yml",
                 "unexpected validation workflow")
        _require(run.get("conclusion") == "success", "Checks did not succeed")
        _require(run.get("event") == "push" and run.get("head_branch") == "main",
                 "Checks must validate a main push")
        _require(trusted(run.get("repository")) and trusted(run.get("head_repository")),
                 "foreign Checks repository")
        head = _sha(run.get("head_sha"))
        _require((run.get("head_commit") or {}).get("id") == head,
                 "Checks commit identity mismatch")
        return {"head": head, "validation_required": "false"}

    # These paths validate within Release through the same reusable Checks.
    # A manual feature branch is not automatic release authority.
    ref = context.get("GITHUB_REF", "")
    version_tag = ref.startswith("refs/tags/v")
    _require((kind == "push" and version_tag)
             or (kind == "workflow_dispatch" and (ref == "refs/heads/main" or version_tag)),
             "release requires a version tag or manual main invocation")
    head = _sha(context.get("GITHUB_SHA"))
    if kind == "push":
        _require(event.get("deleted") is False and event.get("ref") == ref,
                 "tag push identity mismatch")
        # `after` may identify an annotated tag object. The runner's commit
        # SHA is the validation input; Release also checks the peeled tag.
    return {"head": head, "validation_required": "true"}


def main() -> int:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        result = admit(event, os.environ)
    except (Rejected, KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"Release denied: {exc}", file=sys.stderr)
        return 1
    for name, value in result.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
