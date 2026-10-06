#!/usr/bin/env python3
"""Resume a route paused at a usage limit once its reset time passes.

A `start` that stops because the owner's harness is at a usage limit returns
`waiting-capacity` with a `retry_at`. The parent used to have to remember that
time and run `resume_command` itself; nothing woke it. `arm()` now leaves one
record per route and reset time and starts one detached process that sleeps
until then and runs the same `start` once, from the parent's own environment,
so the resumed owner is the parent's as before.

Bounded on purpose: one resume per record, a sleep of at most `MAX_SLEEP_SECONDS`,
and at most `MAX_CHAIN` automatic resumes in a row for one route (a resume that
pauses again arms the next one). A pause without a known reset time arms
nothing; its receipt keeps asking for the manual resume.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_SLEEP_SECONDS = 7 * 24 * 3600
MAX_CHAIN = 3
SLACK_SECONDS = 60
POLL_SECONDS = 60
CHAIN_ENV = "AGENT_CAPACITY_RESUME_CHAIN"


def _epoch(retry_at: str) -> float | None:
    try:
        return datetime.strptime(retry_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def record_path(jobs: Path, route_id: str, retry_at: str) -> Path:
    key = hashlib.sha256(f"{route_id}\0{retry_at}".encode()).hexdigest()[:16]
    return Path(jobs).resolve().parent / "capacity-resume" / f"{route_id}-{key}.json"


def arm(result: dict, route_file, jobs, *, environ=None, spawn=subprocess.Popen, now=time.time) -> dict | None:
    """Arm one automatic resume for a usage-limit pause; `None` when it does not apply."""
    env = dict(os.environ if environ is None else environ)
    if (result.get("state") != "waiting-capacity" or result.get("reason") != "owner-capacity-wait"
            or result.get("required_action") != "resume-after-capacity"):
        return None
    retry_at = result.get("retry_at")
    epoch = _epoch(retry_at)
    try:
        chain = int(env.get(CHAIN_ENV) or 0)
    except ValueError:
        chain = MAX_CHAIN
    if epoch is None or chain >= MAX_CHAIN or epoch - now() > MAX_SLEEP_SECONDS:
        return None
    path = record_path(Path(jobs), result.get("route_id") or "", retry_at)
    record = {"schema": "capacity-resume-v1", "route_id": result.get("route_id"),
              "route_file": str(Path(route_file).resolve()), "jobs": str(Path(jobs).resolve()),
              "retry_at": retry_at, "chain": chain + 1, "state": "armed",
              "armed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
    except FileExistsError:
        return {"record": str(path), "resume_at": retry_at, "state": "already-armed"}
    except OSError:
        return None
    log = path.with_suffix(".log")
    with open(os.devnull, "rb") as stdin, log.open("ab") as out:
        spawn([sys.executable, str(Path(__file__).resolve()), "run", "--record", str(path)],
              stdin=stdin, stdout=out, stderr=subprocess.STDOUT, env={**env, CHAIN_ENV: str(chain + 1)},
              start_new_session=True, close_fds=True)
    return {"record": str(path), "resume_at": retry_at, "state": "armed"}


def _write(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run(path: Path, *, sleep=time.sleep, now=time.time, call=subprocess.run) -> int:
    record = json.loads(path.read_text(encoding="utf-8"))
    epoch = _epoch(record.get("retry_at"))
    if record.get("state") != "armed" or epoch is None:
        return 0
    deadline = now() + min(MAX_SLEEP_SECONDS, max(0.0, epoch + SLACK_SECONDS - now()))
    while now() < deadline:
        # Short steps: a record removed (or disarmed) meanwhile ends the wait.
        sleep(min(POLL_SECONDS, max(0.0, deadline - now())))
        if not path.is_file():
            return 0
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("state") != "armed":
        return 0
    # The installed release at resume time, not the one that armed this wait hours
    # earlier (the printed-command rule, `parent_next_directive.entrypoint`).
    from parent_next_directive import entrypoint
    argv = [sys.executable, entrypoint(ROOT, "utilities/capability-route.py"), "start",
            "--route", record["route_file"], "--jobs", record["jobs"]]
    done = call(argv, text=True, capture_output=True, check=False)
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    try:
        receipt = json.loads(lines[-1]) if lines else {}
    except ValueError:
        receipt = {}
    record.update(state="resumed", resumed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  exit_code=done.returncode, result_state=receipt.get("state"),
                  owner_attempt_id=receipt.get("owner_attempt_id"))
    _write(path, record)
    _notify(record, receipt)
    return 0


def _notify(record: dict, receipt: dict) -> None:
    """One notice to the session that owns the route; a resume that paused again says so."""
    try:
        from session_identity import identity
        from session_notice import notify
        caller = identity()
        if not caller.known or not caller.session_id:
            return
        state = receipt.get("state") or "unknown"
        again = state == "waiting-capacity"
        text = (f"route {record['route_id']} resumed after the usage limit reset: state {state}"
                + (f", owner {receipt['owner_attempt_id']}" if receipt.get("owner_attempt_id") else "")
                + (f"; paused again until {receipt.get('retry_at', 'an unknown time')}" if again else ""))
        notify(caller.harness, caller.session_id, key=f"capacity-resume:{record['route_id']}:{record['retry_at']}",
               subject="capacity resume", text=text, route_id=record["route_id"], jobs=record["jobs"],
               required_action="report-to-user" if not again else "report-pause")
    except Exception:  # noqa: BLE001 -- the resume itself already happened and is recorded
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="sleep until the record's retry_at, then start the route once")
    runner.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.record)


if __name__ == "__main__":
    raise SystemExit(main())
