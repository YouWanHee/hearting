"""Cell, hit, VM, and ledger comparisons for reference replay."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.fleet.tests.freeze_explorer import capture, digest, load_reference, sanitize_reference
from tools.fleet.tests.reference_injection import extract_view_model, inject_view_model


def diff_view_models(expected, actual):
    """Return ordered path-level differences; lists remain ordered and are never key-sorted."""
    differences = []
    def walk(path, left, right):
        if type(left) is not type(right):
            differences.append({"path": path, "expected": left, "actual": right})
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left or key not in right:
                    differences.append({"path": f"{path}/{key}", "expected": left.get(key),
                                        "actual": right.get(key)})
                else:
                    walk(f"{path}/{key}", left[key], right[key])
        elif isinstance(left, (list, tuple)):
            if len(left) != len(right):
                differences.append({"path": path + "/length", "expected": len(left), "actual": len(right)})
            for index, (a, b) in enumerate(zip(left, right)):
                walk(f"{path}/{index}", a, b)
        elif left != right:
            differences.append({"path": path, "expected": left, "actual": right})
    walk("", expected, actual)
    return differences


def _hits(frame):
    hits = frame.get("hits", ()) if isinstance(frame, dict) else getattr(frame, "hits", ())
    return [[hit.y, hit.x0, hit.x1, list(hit.action)] if hasattr(hit, "y") else list(hit)
            for hit in hits]


def _cells(frame):
    cells = frame.get("cells", ()) if isinstance(frame, dict) else getattr(frame, "cells", ())
    return [[list(cell) for cell in row] for row in cells]


def diff_frames(expected, actual, exclusions=()):
    """Report every cell/hit mismatch, with optional (row, start, end) exclusions."""
    differences = []
    left, right = _cells(expected), _cells(actual)
    for y in range(max(len(left), len(right))):
        a = left[y] if y < len(left) else []
        b = right[y] if y < len(right) else []
        for x in range(max(len(a), len(b))):
            if any(ey == y and lo <= x < hi for ey, lo, hi in exclusions):
                continue
            av = a[x] if x < len(a) else None
            bv = b[x] if x < len(b) else None
            if av != bv:
                differences.append({"kind": "cell", "row": y, "column": x,
                                    "expected": av, "actual": bv})
    expected_hits, actual_hits = _hits(expected), _hits(actual)
    if expected_hits != actual_hits:
        differences.append({"kind": "hits", "expected": expected_hits, "actual": actual_hits})
    return differences


def validate_ledger(ledger):
    """Validate difference rows and recompute summary values instead of trusting counters."""
    errors = []
    differences = ledger.get("differences")
    if not isinstance(differences, list):
        return ["differences must be a list"]
    seen = set()
    unclassified = class1 = 0
    for index, row in enumerate(differences):
        if not isinstance(row, dict):
            errors.append(f"differences[{index}] must be an object")
            continue
        ident = row.get("id")
        if not ident or ident in seen:
            errors.append(f"differences[{index}] has missing or duplicate id")
        seen.add(ident)
        if row.get("classification") not in (1, 2, 3):
            unclassified += 1
        if row.get("classification") == 1 and row.get("status") != "fixed":
            class1 += 1
        for required in ("axis", "entity", "field", "states", "widths", "source", "reason"):
            if required not in row:
                errors.append(f"{ident or index}: missing {required}")
    ledger["summary"] = {"unclassified": unclassified, "class1_residue": class1,
                          "differences": len(differences)}
    return errors


def _state_args(label):
    if label in ("OVERVIEW", "ROUTES", "QUESTIONS", "HISTORY", "PEERS", "COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
        return {"view": label}
    if label.startswith("repo-page"):
        return {"scope": "repo:hearting"}
    if label.startswith("session-page"):
        return {"scope": "f5"}
    if label.startswith("route-page"):
        return {"scope": "route:f5"}
    if label.startswith("ROUTES-open"):
        return {"view": "ROUTES", "ui": {"ropen:ROUTES": "f5"}}
    if label.startswith("QUESTIONS-fold"):
        return {"view": "QUESTIONS", "ui": {"qshut:QUESTIONS": "bb"}}
    if label.startswith("HISTORY-open"):
        ui = {"hopen:HISTORY": "0c"}
        if "picked-generation" in label:
            ui["gen:f5"] = 0
        return {"view": "HISTORY", "ui": ui}
    if label.startswith("PEERS-pick"):
        return {"view": "PEERS", "ui": {"pfocus:PEERS": "f5"}}
    return {"view": "OVERVIEW"}


def round_trip(args):
    approved, reference = args.approved.resolve(), args.reference.resolve()
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    baseline = load_reference(approved)
    baseline.path = approved
    sanitize_reference(baseline)
    vm = extract_view_model(baseline)
    states = list(manifest.get("states", ()))
    if args.states == "approved":
        states = states[:16]
    states = [name for name in states if name not in args.except_view]
    results, total = [], 0
    for width in args.widths:
        for name in states:
            expected = capture(approved, width, **_state_args(name))
            candidate = load_reference(reference)
            candidate.path = reference
            inject_view_model(candidate, vm)
            actual = capture(reference, width, renderer=candidate, **_state_args(name))
            diffs = diff_frames(expected, actual)
            total += len(diffs)
            results.append({"state": name, "width": width, "mismatch_count": len(diffs),
                            "mismatches": diffs[:100]})
    output = {"schema": "fleet-round-trip-v1", "approved_sha256": digest(approved / "mock13.py"),
              "reference_sha256": digest(reference / "mock13.py"), "state_count": len(states),
              "widths": args.widths, "diff_count": total, "results": results,
              "claim": "reference extraction/injection round trip; production live VM is not evaluated"}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "round-trip.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"round-trip: {len(results)} state-width captures, {total} mismatch records; {args.out / 'round-trip.json'}")
    return 0 if total == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    rt = sub.add_parser("round-trip")
    rt.add_argument("--approved", type=Path, required=True)
    rt.add_argument("--reference", type=Path, required=True)
    rt.add_argument("--widths", type=lambda s: [int(x) for x in s.split(",")], default=[140, 168])
    rt.add_argument("--rows", type=int, default=45)
    rt.add_argument("--states", choices=("approved", "all"), default="approved")
    rt.add_argument("--except-view", action="append", default=[])
    rt.add_argument("--out", type=Path, required=True)
    rt.set_defaults(func=round_trip)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
