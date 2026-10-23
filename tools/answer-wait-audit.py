#!/usr/bin/env python3
"""Read-only before/after answer-wait audit for the real dispatch registry."""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))

import route_authority
from dispatch_registry_cache import registry_read_scope, registry_snapshot
from fleet.collectors import dispatch
import human_gate_observation


def _stat(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(path: Path):
    as_of = datetime.now(timezone.utc).timestamp()
    before_stat = _stat(path)
    before_hash = _hash(path)
    with registry_read_scope():
        snap = registry_snapshot(path)
    if not snap.complete:
        print(f"registry_complete=false reason={snap.reason}")
        return 2
    rows = snap.view.answer_wait_owner_candidates()
    latest, first = {}, {}
    for row in rows:
        meta = row.completion_metadata()
        attempt = meta.get("attempt_id")
        if attempt:
            first.setdefault(attempt, row.order)
            latest[attempt] = (row.status, meta, row.fields, row.order)
    blocked, fail = {}, {}
    for attempt, (status, meta, fields, order) in latest.items():
        kind = route_authority.answerable_owner_end(status, meta)
        if kind:
            (blocked if kind == "BLOCKED" else fail)[attempt] = (meta, fields, order)
    after = human_gate_observation.read_answer_waits(jobs_paths=[path], now=as_of)
    after_ids = {item.get("attempt_id"): item for item in after.get("items", ())
                 if item.get("source") == "S3"}

    reasons = {"non-BLOCKED": 0, "unbound": 0, "existing-closure": 0,
               "later-same-route-owner": 0, ">7d-drop": 0, "72h-7d-fold": 0,
               "current": 0}
    after_details = []
    for attempt, (meta, fields, order) in blocked.items():
        route_file = meta.get("owner_route_file") or meta.get("route_file")
        route_id = meta.get("owner_route_id") or meta.get("route_id")
        digest = meta.get("owner_route_hash") or meta.get("route_hash")
        if not route_file or not route_id or not digest:
            reasons["unbound"] += 1
            continue
        if attempt in after_ids:
            item = after_ids[attempt]
            if item.get("stale"):
                reasons["72h-7d-fold"] += 1
            else:
                reasons["current"] += 1
                after_details.append((attempt, item))
            continue
        finished = meta.get("ended_at") or fields[0]
        try:
            epoch = float(finished)
        except (ValueError, TypeError):
            try:
                epoch = datetime.fromisoformat(str(finished).replace("Z", "+00:00")).timestamp()
            except Exception:
                epoch = as_of
        age = as_of - epoch
        if age > 7 * 86400:
            reasons[">7d-drop"] += 1
            continue
        later = any(other != attempt and first.get(other, info[3]) > first.get(attempt, order)
                    and (info[1].get("owner_route_id") or info[1].get("route_id")) == route_id
                    and (info[1].get("owner_route_hash") or info[1].get("route_hash")) == digest
                    and (info[1].get("owner_route_file") or info[1].get("route_file")) == route_file
                    for other, info in latest.items())
        reasons["later-same-route-owner" if later else "existing-closure"] += 1

    for _ in fail:
        reasons["non-BLOCKED"] += 1
    after_stat = _stat(path)
    after_hash = _hash(path)
    print("Answer-wait S3 audit (read-only)")
    print(f"as_of_utc={datetime.fromtimestamp(as_of, timezone.utc).isoformat()}")
    print(f"registry={path} dev={before_stat[0]} ino={before_stat[1]} bytes={before_stat[2]} "
          f"mtime_ns={before_stat[3]} sha256={before_hash} rows={snap.view.line_count}")
    print("stage\tcount")
    print(f"before-answerable-owner\t{len(blocked) + len(fail)}")
    print(f"before-unbound-unknown\t{reasons['unbound']}")
    for reason in ("non-BLOCKED", "unbound", "existing-closure", "later-same-route-owner",
                   ">7d-drop", "72h-7d-fold", "current"):
        print(f"{reason}\t{reasons[reason]}")
    print(f"after-reconciliation\t{sum(reasons[k] for k in ('current','72h-7d-fold','>7d-drop','existing-closure','later-same-route-owner'))}")
    print("remaining_current\tattempt\troute\tage_hours\towner\tblocker")
    for attempt, item in after_details:
        meta = latest[attempt][1]
        owner = item.get("owner") or {}
        age = max(0, (as_of - float(item.get("since") or as_of)) / 3600)
        blocker = " ".join(str(item.get("question") or "BLOCKED owner").split())[:160]
        print(f"S3\t{attempt[:16]}\t{item.get('route_id')}\t{age:.2f}\t"
              f"{owner.get('harness')}:{owner.get('session_id')}\t{blocker}")
    print(f"registry_after dev={after_stat[0]} ino={after_stat[1]} bytes={after_stat[2]} "
          f"mtime_ns={after_stat[3]} sha256={after_hash} unchanged={before_stat == after_stat and before_hash == after_hash}")
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="?", help="optional registry path (normally the real jobs.log)")
    args = parser.parse_args(argv)
    paths = dispatch.human_gate_registry_paths(args.jobs) if args.jobs else dispatch.human_gate_registry_paths()
    if not paths:
        print("registry unavailable")
        return 2
    return audit(Path(paths[0]))


if __name__ == "__main__":
    raise SystemExit(main())
