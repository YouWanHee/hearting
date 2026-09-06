#!/usr/bin/env python3
"""Assert a test suite leaves the operator's live dispatch ledger untouched.

Why this exists: on 2026-09-05 a fixture change made `capability_route.test.py`
append **660 fake attempt rows** to the operator's live `jobs.log` (1,194 lines
at the time). The cause was not exotic -- the suite's `setUp` *unset*
`AGENT_DISPATCH_JOBS`, and with it unset the dispatch state root resolves from
`XDG_STATE_HOME`/`HOME`, not from the fixture `AGENT_HOME`. Unsetting an
environment variable is not isolation; it selects the default, and the default
is production.

`tools/run-tests.py` is already safe here: it redirects `HOME`, `XDG_STATE_HOME`
and `XDG_DATA_HOME` into a tmpdir, so a suite run under the runner cannot reach
the live ledger at all. But that is not how a suite is run while it is being
written -- `python3 utilities/capability_route.test.py` inherits the real
environment, and that is where the 660 rows came from. This check runs suites
the way a developer does, deliberately WITHOUT the runner's isolation, and
asserts the ledger is byte-identical afterwards.

It never writes. It reads the ledger before and after and reports.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def live_jobs_path() -> Path:
    """The ledger the harness itself would resolve, with no override."""
    from dispatch_contract import (  # noqa: E402
        resolve_agent_home,
        resolve_dispatch_state_root,
    )

    return resolve_dispatch_state_root(resolve_agent_home(), None) / "jobs.log"


def snapshot(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, ""
    data = path.read_bytes()
    return data.count(b"\n"), hashlib.sha256(data).hexdigest()


def added_lines(before_text: str, after_text: str, limit: int = 5) -> list[str]:
    before = before_text.splitlines()
    after = after_text.splitlines()
    if after[: len(before)] == before:          # pure append, the common case
        return after[len(before):][:limit]
    return [line for line in after if line not in set(before)][:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="*", help="suite paths; default: the dispatch set")
    parser.add_argument("--jobs", help="ledger to watch (default: the live one)")
    args = parser.parse_args()

    jobs = Path(args.jobs) if args.jobs else live_jobs_path()
    suites = [Path(s) for s in args.suites] or [
        ROOT / "utilities" / name for name in (
            "capability_route.test.py",
            "worker_route_guard.test.py",
            "dispatch_contract.test.py",
            "dispatch_completion_join.test.py",
            "dispatch_completion_marker.test.py",
            "stage_session_subdivision.test.py",
            "subsession_chain_head_moved.test.py",
            "worker_capacity_contract.test.py",
        )
    ]

    print(f"ledger: {jobs}")
    before_lines, before_digest = snapshot(jobs)
    before_text = jobs.read_text(encoding="utf-8", errors="replace") if jobs.is_file() else ""
    print(f"before: {before_lines} lines sha256={before_digest[:16] or '(absent)'}")

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    violations = []
    for suite in suites:
        if not suite.is_file():
            print(f"  skip   {suite} (absent)")
            continue
        subprocess.run(
            [sys.executable, str(suite)], cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        lines, digest = snapshot(jobs)
        if digest != before_digest:
            after_text = jobs.read_text(encoding="utf-8", errors="replace")
            violations.append((suite, lines - before_lines, added_lines(before_text, after_text)))
            print(f"  WROTE  {suite.name}  {lines - before_lines:+d} lines")
            before_lines, before_digest, before_text = lines, digest, after_text
        else:
            print(f"  clean  {suite.name}")

    if violations:
        print(
            "\nledger-invariant: FAIL — a suite modified the live dispatch ledger.\n"
            "A test must bind AGENT_DISPATCH_JOBS to its own temp directory. Unsetting\n"
            "it is not isolation: the default resolves from XDG_STATE_HOME/HOME, which\n"
            "is production.",
            file=sys.stderr,
        )
        for suite, delta, sample in violations:
            print(f"  {suite}: {delta:+d} lines", file=sys.stderr)
            for line in sample:
                print(f"      {line[:160]}", file=sys.stderr)
        return 1

    print("\nledger-invariant: PASS — every suite left the live ledger byte-identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
