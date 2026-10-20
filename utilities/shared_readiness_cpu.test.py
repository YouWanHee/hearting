#!/usr/bin/env python3
"""Matched 12-second production-carrier fixture for baseline/candidate sources.

The peer-watch readiness branch is exercised by the focused shared-readiness
runner tests; this fixture measures the carrier's real pending loop and reports
the missing host watcher measurement separately.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time


SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOW = 12.0


def _proc_stat(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if not tail or tail[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "ppid": int(tail[1]), "utime": int(tail[11]),
                "stime": int(tail[12]), "cutime": int(tail[13]),
                "cstime": int(tail[14]), "start": tail[19],
                "rss_pages": int(tail[21])}
    except (OSError, ValueError, IndexError):
        return None


def _command(pid):
    try:
        return [part.decode("utf-8", "replace") for part in
                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    except OSError:
        return []


def _tree(seed):
    rows = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            item = _proc_stat(int(name))
            if item is not None:
                rows[item["pid"]] = item
    selected = {seed}
    changed = True
    while changed:
        changed = False
        for pid, item in rows.items():
            if item["ppid"] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    result = {}
    for pid in selected:
        item = rows.get(pid)
        if item is None:
            continue
        argv = _command(pid)
        joined = " ".join(argv)
        role = ("carrier" if pid == seed else "readiness-helper"
                if "dispatch-attempt-ready.py" in joined else "shared-runner"
                if "peer-steward.py" in joined and "__obligation-runner" in joined
                else "other")
        item["role"] = role
        item["argv"] = joined[:240]
        result[(pid, item["start"])] = item
    return result


def _class_rows(snapshot):
    grouped = {}
    for (pid, start), item in snapshot.items():
        value = grouped.setdefault(item["role"], {"count": 0, "rss_bytes": 0,
                                                   "pids": [], "cpu_ticks": 0})
        value["count"] += 1
        value["rss_bytes"] += item["rss_pages"] * os.sysconf("SC_PAGE_SIZE")
        value["pids"].append({"pid": pid, "start": start, "argv": item["argv"]})
    return grouped


def _sample(pid, window):
    hz = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    initial = _tree(pid)
    previous = initial
    initial_classes = _class_rows(initial)
    peaks = {key: {"processes": value["count"], "rss_bytes": value["rss_bytes"]}
             for key, value in initial_classes.items()}
    identities = {key: set((item["pid"], item["start"]) for item in initial.values())
                  for key in initial_classes}
    sample_cpu = {}
    samples = 0
    while time.monotonic() - started < window:
        time.sleep(0.05)
        current = _tree(pid)
        current_classes = _class_rows(current)
        for role, current_row in current_classes.items():
            peak = peaks.setdefault(role, {"processes": 0, "rss_bytes": 0})
            peak["processes"] = max(peak["processes"], current_row["count"])
            peak["rss_bytes"] = max(peak["rss_bytes"], current_row["rss_bytes"])
            identities.setdefault(role, set()).update(
                (item["pid"], item["start"]) for item in current.values()
                if item["role"] == role)
        for key, item in current.items():
            prior = previous.get(key)
            if prior is not None:
                per_role = sample_cpu.setdefault(item["role"], {})
                per_role[key] = per_role.get(key, 0) + max(
                    0, item["utime"] + item["stime"] - prior["utime"] - prior["stime"])
        previous = current
        samples += 1
    elapsed = time.monotonic() - started
    final = _tree(pid)
    final_stat = _proc_stat(pid)
    live_cpu = {}
    for key, item in final.items():
        live_cpu[item["role"]] = live_cpu.get(item["role"], 0) + sample_cpu.get(
            item["role"], {}).get(key, 0)
    return {
        "elapsed_seconds": elapsed, "samples": samples, "clock_ticks_per_second": hz,
        "classes": {
            role: {"start_processes": initial_classes.get(role, {}).get("count", 0),
                   "end_processes": _class_rows(final).get(role, {}).get("count", 0),
                   "peak_processes": peaks.get(role, {}).get("processes", 0),
                   "peak_rss_bytes": peaks.get(role, {}).get("rss_bytes", 0),
                   "live_own_cpu_ticks_at_window_end": live_cpu.get(role, 0),
                   "live_own_cpu_seconds_at_window_end": live_cpu.get(role, 0) / hz,
                   "live_own_cpu_percent_one_core": 100.0 * live_cpu.get(role, 0) / (hz * elapsed),
                   "pid_start_identities": [list(value) for value in sorted(identities.get(role, set()))]}
            for role in sorted(set(initial_classes) | set(peaks) | set(sample_cpu))
        },
        "carrier_final_stat": ({"own_cpu_ticks": final_stat["utime"] + final_stat["stime"],
                                 "reaped_child_cpu_ticks": final_stat["cutime"] + final_stat["cstime"]}
                                if final_stat else None),
        "child_cpu_accounting": "live process own ticks at window end are exclusive; carrier cutime+cstime is reaped-child CPU at the same window boundary, so exited helpers are not added again",
    }


def _write_jobs(path, target_bytes, attempt):
    digest = hashlib.sha256()
    size = rows = 0
    with path.open("wb") as stream:
        while size < target_bytes:
            row = (f"2026-10-20T00:00:00Z\trunning\t/repo\t/wt\tworker\t"
                   f"attempt_id=att-unrelated-{rows:07d},note={'n' * 160}\n").encode()
            stream.write(row)
            digest.update(row)
            size += len(row)
            rows += 1
        target = (f"2026-10-20T00:00:01Z\trunning\t/repo\t/wt\towner\t"
                  f"attempt_id={attempt},note=pending\n").encode()
        stream.write(target)
        digest.update(target)
        size += len(target)
        rows += 1
    return {"bytes": size, "rows": rows, "sha256": digest.hexdigest()}


def _carrier_child(source_root, jobs, result_path, window):
    utilities = source_root / "utilities"
    spec = importlib.util.spec_from_file_location(
        "fixture_dispatch_owner_rewake", source_root / "hooks" / "dispatch-owner-rewake.py")
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)
    attempt = "att-readiness-cpu-fixture"
    launch = hook.Launch(attempt_id=attempt, jobs=Path(jobs), session_id="fixture-session")
    started = time.monotonic()
    outcome = hook.wait_for_attempt(
        launch, utilities / "dispatch-attempt-ready.py", deadline=started + window)
    final = _proc_stat(os.getpid())
    Path(result_path).write_text(json.dumps({
        "wait_state": outcome[0], "wait_reason": outcome[1],
        "wait_elapsed_seconds": time.monotonic() - started,
        "carrier_stat": ({"own_cpu_ticks": final["utime"] + final["stime"],
                          "reaped_child_cpu_ticks": final["cutime"] + final["cstime"]}
                         if final else None),
    }, sort_keys=True), encoding="utf-8")


def _run_child(source_root, jobs, state_root, window):
    result_path = state_root / "carrier-result.json"
    env = {**os.environ, "AGENT_HOME": str(source_root),
           "AGENT_DISPATCH_JOBS": str(jobs), "HARNESS_STATE_ROOT": str(state_root / "harness"),
           "AGENT_PEER_LEDGER_ROOT": str(state_root / "peer"),
           "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "5",
           "AGENT_CLAUDE_REWAKE_MAX_SECONDS": str(max(5, int(window))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen(
        [sys.executable, __file__, "--carrier-child", str(source_root), str(jobs),
         str(result_path), str(window)], cwd=source_root, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True), result_path, env


def run_window(source_root, *, target_bytes=28 * 1024 * 1024, window=DEFAULT_WINDOW,
               source_revision=None):
    attempt = "att-readiness-cpu-fixture"
    with tempfile.TemporaryDirectory(prefix="hearting-readiness-cpu-") as temporary:
        state_root = Path(temporary)
        jobs = state_root / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True)
        workload = _write_jobs(jobs, target_bytes, attempt)
        process, result_path, env = _run_child(source_root, jobs, state_root, window)
        sample = _sample(process.pid, window)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            raise RuntimeError("carrier fixture exceeded its fixed wait window")
        stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        if process.returncode != 0:
            raise RuntimeError(f"carrier fixture exited {process.returncode}: {stderr[-2000:]}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        runner_processes = [item for item in sample["classes"].get("shared-runner", {})
                            .get("pid_start_identities", [])]
        return {
            "source_root": str(source_root), "source_revision": source_revision or subprocess.run(
                ["git", "-C", str(source_root), "rev-parse", "HEAD"],
                check=False, capture_output=True, text=True).stdout.strip() or "archive",
            "host": os.uname().nodename, "python": sys.version.split()[0],
            "host_namespace": os.readlink("/proc/self/ns/pid"),
            "workload": workload, "window": sample,
            "branch_evidence": {"actual_wait_state": result["wait_state"],
                                "actual_wait_reason": result["wait_reason"],
                                "carrier_completed_window": result["wait_elapsed_seconds"] >= window - 0.25,
                                "readiness_helper_processes": sample["classes"].get(
                                    "readiness-helper", {}).get("peak_processes", 0),
                                "shared_runner_pids": runner_processes},
            "carrier_result": result,
            "reaped_child_cpu_ticks": (sample["carrier_final_stat"]["reaped_child_cpu_ticks"]
                                        if sample["carrier_final_stat"] else None),
            "post_window_completion_seconds": max(0.0, result["wait_elapsed_seconds"]
                                                   - sample["elapsed_seconds"]),
            "warnings": (["carrier returned after the fixed sample window; post-window CPU is excluded"]
                         if result["wait_elapsed_seconds"] > sample["elapsed_seconds"] + 0.25 else []),
        }


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "--carrier-child":
        _carrier_child(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), float(sys.argv[5]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path,
                        default=Path(os.environ.get("HEARTING_READINESS_SOURCE_ROOT", SOURCE_ROOT)))
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    parser.add_argument("--source-revision", default=None)
    evidence_path = os.environ.get("HEARTING_RUNTIME_OBSERVATION_EVIDENCE")
    parser.add_argument("--evidence", type=Path,
                        default=Path(evidence_path) if evidence_path else None)
    parser.add_argument("--bytes", type=int, default=28 * 1024 * 1024)
    args = parser.parse_args()
    evidence = run_window(args.source_root.resolve(), target_bytes=args.bytes,
                          window=args.window, source_revision=args.source_revision)
    evidence["peer_post_native_result_branch"] = {
        "status": "not-measured-by-carrier-window",
        "branch_unit_tests": ["SharedReadinessRunnerTest"],
        "production_watcher_window": "unavailable",
    }
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
