#!/usr/bin/env python3
"""Project the shared dispatch paragraphs into the three adapter bootstraps.

`core/fragments/bootstrap-dispatch.md` is the one source. Each bootstrap holds
it, byte for byte, between the BEGIN/END markers below; everything outside the
markers stays adapter-owned. `--check` reports a stale copy without writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "core/fragments/bootstrap-dispatch.md"
TARGETS = ("adapters/claude/CLAUDE.md", "adapters/codex/AGENTS.md", "adapters/opencode/AGENTS.md")
BEGIN = f"<!-- BEGIN generated from {SOURCE} by tools/sync-bootstrap-dispatch.py; edit the source -->"
END = f"<!-- END generated from {SOURCE} -->"


def render(text: str, body: str, target: str) -> str:
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
        raise ValueError(f"{target}: needs exactly one generated-block marker pair")
    head, rest = text.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n{body.rstrip()}\n{END}{tail}"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)
    body = (root / SOURCE).read_text(encoding="utf-8")
    stale = []
    for target in TARGETS:
        path = root / target
        text = path.read_text(encoding="utf-8")
        try:
            wanted = render(text, body, target)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if wanted == text:
            continue
        stale.append(target)
        if not args.check:
            path.write_text(wanted, encoding="utf-8")
    if args.check and stale:
        print("stale bootstrap dispatch block: " + ", ".join(stale), file=sys.stderr)
        return 1
    print(f"bootstrap dispatch block {'checked' if args.check else 'generated'} in {len(TARGETS)} bootstraps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
