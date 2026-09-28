#!/usr/bin/env python3
"""Select expensive Checks jobs; uncertain changes retain the full suite."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_plan", ROOT / "tools/release/plan.py")
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
DOCUMENTS = {
    "AGENTS.md", "CLAUDE.md", "INSTALL_LAYOUT.md", "LICENSE", "MANUAL.md",
    "README.md", "README.ko.md", "RELEASE_POLICY.md",
}


def documentation_path(path):
    return path in DOCUMENTS or (path.startswith("docs/") and path.endswith(".md"))


def published_tag(context):
    repository = context.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository unavailable")
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/releases/latest", "--jq", ".tag_name"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    tag = result.stdout.strip()
    PLAN.parse_version(tag, stable_only=True)
    return tag


def select(repo, event, context, *, released_tag=None):
    kind = context.get("GITHUB_EVENT_NAME")
    head = context.get("GITHUB_SHA") or "HEAD"
    try:
        if kind == "push" and context.get("GITHUB_REF") == "refs/heads/main":
            # Last-push-only diffs could hide code from a failed/cancelled CI.
            # A released baseline has already passed the full release checks.
            # A newly pushed manual tag may still be awaiting validation.
            # Use the published release, not simply the newest local tag.
            base = released_tag or published_tag(context)
            PLAN.parse_version(base, stable_only=True)
            subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head],
                           check=True, capture_output=True)
        elif kind == "pull_request":
            base = event["pull_request"]["base"]["sha"]
            if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
                raise ValueError("invalid PR base")
            base = PLAN.git(repo, "merge-base", base, head).strip()
        else:
            # Tags, manual runs and reusable release validation stay full.
            return True, "explicit-full-validation"
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            check=True, capture_output=True, text=True,
        )
        paths = [path for path in result.stdout.split("\0") if path]
        if paths and all(documentation_path(path) for path in paths):
            return False, "documentation-only"
        return True, "runtime-test-or-unclassified-change"
    except (PLAN.PlanError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return True, "comparison-unavailable"


def main():
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        full, reason = select(ROOT, event, os.environ)
    except (OSError, KeyError, ValueError):
        full, reason = True, "event-unavailable"
    print(f"full_tests={str(full).lower()}")
    print(f"reason={reason}")


if __name__ == "__main__":
    main()
