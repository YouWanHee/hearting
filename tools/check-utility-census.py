#!/usr/bin/env python3
"""Pre-push census completeness for utilities/* (generate.py battery member).

The projected/deferred decision for a new non-test utility is made by the agent
adding the file; enforcement lives in tools/check-adaptation-boundary.sh. That
guard, however, is not part of every session's habitual battery, so a forgotten
census row historically surfaced only on CI after push
(2026-07-23 dispatch_parent_context_conformance.test.py incident — and the same
class before it). This checker runs the SAME census membership rule in the
standard ``generate.py --check`` battery, so the acting agent is asked to record
its judgment BEFORE push. Nothing is duplicated here: the projected lists are
parsed from the boundary script and the deferred lists are read from
``tools/adaptation-census.tsv``, the same files the guard reads.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "tools" / "check-adaptation-boundary.sh"
CENSUS = ROOT / "tools" / "adaptation-census.tsv"
# Mirrors the boundary census: test files are DERIVED-deferred.
TEST_PATTERNS = (".test.py", ".test.sh")


def census_list(census: str, name: str) -> set[str]:
    members = set()
    for line in census.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == name:
            members.add(fields[1])
    return members


def census_scopes(text: str, census: str) -> list[tuple[str, set[str]]]:
    projected = re.findall(r'^\s*UTILITY_PROJECTED="([^"]*)"', text, re.M)
    if len(projected) != 2:
        raise SystemExit(
            "check-utility-census: expected exactly 2 UTILITY_PROJECTED lists in "
            f"{BOUNDARY.name}, found {len(projected)} — realign this parser with the guard"
        )
    deferred = census_list(census, "utility-deferred")
    shared_members = census_list(census, "shared-utility-deferred")
    if not deferred or not shared_members:
        raise SystemExit(
            f"check-utility-census: {CENSUS.name} has no utility-deferred or "
            "shared-utility-deferred rows — realign this parser with the guard"
        )
    scopes = []
    for label, p in (("codex", projected[0]), ("opencode", projected[1])):
        scopes.append((label, set(p.split()) | deferred | shared_members))
    return scopes


def main() -> int:
    # --check and write mode behave identically: this tool only verifies.
    scopes = census_scopes(BOUNDARY.read_text(encoding="utf-8"),
                           CENSUS.read_text(encoding="utf-8"))
    missing: list[str] = []
    for path in sorted((ROOT / "utilities").iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(TEST_PATTERNS):
            continue
        for label, members in scopes:
            if name not in members:
                missing.append(f"  utilities/{name} — {label} census")
    if missing:
        print("utility census rows missing (decide projected|deferred: add the name to", file=sys.stderr)
        print(f"UTILITY_PROJECTED in {BOUNDARY.relative_to(ROOT)} or a utility-deferred row in", file=sys.stderr)
        print(f"{CENSUS.relative_to(ROOT)}; *.test.py/*.test.sh auto-defer):", file=sys.stderr)
        for row in missing:
            print(row, file=sys.stderr)
        return 1
    print("utility census complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
