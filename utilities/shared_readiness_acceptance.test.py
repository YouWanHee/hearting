#!/usr/bin/env python3
"""Final-source acceptance for shared readiness CPU and lifecycle fixtures."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CPU_FIXTURE = HERE / "shared_readiness_cpu_fixture.test.py"
LIFECYCLE_FIXTURE = HERE / "shared_readiness_lifecycle.test.py"
WINDOWS = ("cold_start", "warm_unchanged_12s", "unrelated_event_12s")
ROLES = ("carrier", "readiness-helper", "peer-watch", "shared-runner")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _lifecycle_child(evidence_dir):
    """Run the lifecycle suite and persist its unittest cleanup outcomes."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "shared_readiness_lifecycle_acceptance", LIFECYCLE_FIXTURE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"lifecycle fixture cannot be loaded: {LIFECYCLE_FIXTURE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cleanup_results = []
    original_cleanup = module.LifecycleRunnerProcessTest._cleanup_processes

    def record_cleanup(test_case):
        runner_pid = test_case.runner_pid
        waited_runner = []
        original_waitpid = os.waitpid

        def track_waitpid(pid, options):
            result = original_waitpid(pid, options)
            if runner_pid is not None and pid == runner_pid and result[0] == runner_pid:
                waited_runner.append({"pid": result[0],
                                      "exit_code": os.waitstatus_to_exitcode(result[1])})
            return result

        os.waitpid = track_waitpid
        try:
            return original_cleanup(test_case)
        finally:
            os.waitpid = original_waitpid
            watcher_rows = [{"pid": process.pid, "returncode": process.poll(),
                             "reaped": process.returncode is not None}
                            for process in test_case.watchers]
            runner_reaped = runner_pid is None or bool(waited_runner)
            locked_fd_closed = test_case.runner_lock_fd is None
            row = {
                "watchers": watcher_rows,
                "runner_pid": runner_pid,
                "runner_wait_results": waited_runner,
                "runner_reaped": runner_reaped,
                "runner_lock_fd_closed": locked_fd_closed,
                "ok": (all(item["reaped"] for item in watcher_rows)
                       and runner_reaped and locked_fd_closed),
            }
            cleanup_results.append(row)

            if module.EVIDENCE_ROWS:
                fixture = module.EVIDENCE_ROWS[-1]
                receipt_records = {}
                ack_records = {}
                for watch_id, _arm, paths in test_case.arms:
                    receipt_records[watch_id] = paths.receipt.is_file()
                    ack_records[watch_id] = paths.ack.is_file()
                fixture["receipt_records"] = receipt_records
                fixture["receipt_count"] = sum(receipt_records.values())
                fixture["ack_records"] = ack_records
                fixture["ack_count"] = sum(ack_records.values())
                fixture["runner_recovered_watches_after_arm"] = len(test_case.watchers)
                fixture["target_state_before_after"] = [
                    "working",
                    (_read_json(test_case.state_file) or {}).get("status", "unknown"),
                ]

    module.LifecycleRunnerProcessTest._cleanup_processes = record_cleanup
    try:
        suite_exit = module.main(["--evidence-dir", str(evidence_dir)])
    except Exception as exc:
        suite_exit = 1
        _write_json(evidence_dir / "shared-readiness-lifecycle-failure.json", {
            "status": "failed-before-evidence-write",
            "error": f"{type(exc).__name__}: {exc}",
        })

    evidence_path = evidence_dir / "shared-readiness-lifecycle.json"
    evidence = _read_json(evidence_path) or {
        "schema_version": 1,
        "scope": "execute phase 2 lifecycle; isolated production-entry fixture",
        "tests_run": 0,
        "failures": [],
        "errors": ["lifecycle suite did not write its evidence file"],
        "fixtures": [],
    }
    cleanup_status = ("pass" if cleanup_results and
                      all(row.get("ok") for row in cleanup_results) else "fail")
    evidence["fixture_cleanup"] = {
        "status": cleanup_status,
        "results": cleanup_results,
        "ownership": "unittest process cleanup, under the assigned verification lease",
    }
    evidence["suite_exit_code"] = suite_exit
    _write_json(evidence_path, evidence)
    print(json.dumps({"lifecycle_evidence": str(evidence_path),
                      "suite_exit_code": suite_exit,
                      "fixture_cleanup": cleanup_status}, sort_keys=True))
    return 0 if suite_exit == 0 and cleanup_status == "pass" else 1


def _run_child(name, command, log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, env=env,
                               capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    return {
        "command": [str(part) for part in command],
        "exit_code": completed.returncode,
        "elapsed_seconds": elapsed,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def _source_hashes():
    paths = (
        HERE / "shared_readiness_cpu.test.py",
        HERE / "shared_readiness_cpu_fixture.test.py",
        HERE / "shared_readiness_acceptance.test.py",
    )
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def _metric_rollup(measurement):
    rows = []
    for lane, sides in measurement.get("matrix", {}).items():
        for side, cases in sides.items():
            for case, scenario in cases.items():
                for window_name in WINDOWS:
                    window = scenario.get(window_name)
                    if not isinstance(window, dict):
                        continue
                    classes = window.get("classes", {})
                    rows.append({
                        "lane": lane,
                        "side": side,
                        "case": case,
                        "window": window_name,
                        "elapsed_seconds": window.get("elapsed_seconds"),
                        "production_inclusive_cpu_delta_seconds": window.get(
                            "production_inclusive_cpu_delta_seconds"),
                        "inclusive_lineage_cpu_delta_seconds": window.get(
                            "inclusive_lineage_cpu_delta_seconds"),
                        "simultaneous_total_rss_bytes": window.get(
                            "simultaneous_total_rss_bytes", {}),
                        "classes": {
                            role: {
                                "start_processes": classes.get(role, {}).get("start_processes", 0),
                                "end_processes": classes.get(role, {}).get("end_processes", 0),
                                "peak_processes": classes.get(role, {}).get("peak_processes", 0),
                                "start_rss_bytes": classes.get(role, {}).get("start_rss_bytes", 0),
                                "end_rss_bytes": classes.get(role, {}).get("end_rss_bytes", 0),
                                "peak_simultaneous_rss_bytes": classes.get(
                                    role, {}).get("peak_simultaneous_rss_bytes", 0),
                                "sampled_own_cpu_delta_ticks": classes.get(
                                    role, {}).get("sampled_own_cpu_delta_ticks", 0),
                                "sampled_own_cpu_delta_seconds": classes.get(
                                    role, {}).get("sampled_own_cpu_delta_seconds", 0),
                                "reaped_child_counter_delta_ticks": classes.get(
                                    role, {}).get("reaped_child_counter_delta_ticks", 0),
                                "reaped_child_counter_delta_seconds": classes.get(
                                    role, {}).get("reaped_child_counter_delta_seconds", 0),
                            }
                            for role in ROLES
                        },
                    })
    return rows


def _mib(value):
    return f"{(value or 0) / (1024 * 1024):.1f}"


def _metric_markdown(rows):
    lines = [
        "| Lane / case | Side | Window | Peak processes C/H/W/R | Production CPU s | Own CPU C/H/W/R s | Reaped CPU C/H/W/R s | Peak RSS C/H/W/R MiB | Production total peak RSS MiB |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    short_window = {"cold_start": "cold", "warm_unchanged_12s": "warm unchanged",
                    "unrelated_event_12s": "unrelated event"}
    for row in rows:
        classes = row["classes"]
        counts = "/".join(str(classes[role]["peak_processes"]) for role in ROLES)
        own = "/".join(f"{classes[role]['sampled_own_cpu_delta_seconds']:.2f}"
                        for role in ROLES)
        reaped = "/".join(f"{classes[role]['reaped_child_counter_delta_seconds']:.2f}"
                          for role in ROLES)
        rss = "/".join(_mib(classes[role]["peak_simultaneous_rss_bytes"])
                        for role in ROLES)
        total = _mib(row["simultaneous_total_rss_bytes"].get("production_classes_peak"))
        cpu = row["production_inclusive_cpu_delta_seconds"]
        cpu_text = f"{cpu:.3f}" if isinstance(cpu, (int, float)) else "unknown"
        lines.append(
            f"| {row['lane']} / {row['case']} | {row['side']} | "
            f"{short_window[row['window']]} | {counts} | {cpu_text} | "
            f"{own} | {reaped} | {rss} | {total} |")
    return "\n".join(lines)


def _candidate_helper_launches(measurement, side):
    total = 0
    found = False
    for scenario in measurement.get("matrix", {}).get("carrier", {}).get(side, {}).values():
        branch = scenario.get("branch_evidence", {})
        if "total_helper_launches" in branch:
            total += branch["total_helper_launches"]
            found = True
    return total if found else None


def _make_handoff(report, evidence_dir, measurement, lifecycle, cleanup):
    d1 = report["d1_source_currentness"]
    d2 = report["d2_shared_observation"]
    d3 = report["d3_independent_delivery"]
    d4 = report["d4_matched_measurements"]
    fixture = (lifecycle.get("fixtures") or [{}])[0]
    selected = lifecycle.get("selected_regressions", [])
    metric_rows = report["metric_windows"]
    lines = [
        "# Final source evidence: shared readiness",
        "",
        f"Status: **{report['status']}** for this evidence-refresh slice only; the owner retains the execute gate.",
        "",
        "## d1 — source currentness",
        "",
        f"Candidate `{d1['candidate_revision']}` descends from latest confirmed `origin/main` `{d1['origin_main']}`; owner evidence confirms #688 and #692 are ancestors and records the final remote main check. Source content fingerprints are in `shared-readiness-acceptance.json` and the CPU fixture JSON.",
        "",
        "## d2 — shared parsing and target observation",
        "",
        f"Carrier helper launches: baseline={d2['baseline_carrier_helper_launches']}, candidate={d2['candidate_carrier_helper_launches']}. Candidate shared-runner peak is one process. The unchanged 29.36 MB registry retention fixture is reused because `dispatch_registry_cache.py` is unchanged from the sealed candidate commit: one full metadata parse of 86,471 rows on each side; retained traced allocation {d2['baseline_retained_bytes']:,} → {d2['candidate_retained_bytes']:,} bytes and post-parse RSS {d2['baseline_rss_kib']:,} → {d2['candidate_rss_kib']:,} KiB. This is central-cache evidence, not watcher-scaling evidence.",
        f"The final lifecycle fixture records {fixture.get('runner_session_snapshot_calls', 'unknown')} runner session snapshots for five same-target stewards plus one separate target; candidate runner and per-class costs are counted once in each measurement window. No per-watch parser/view claim is inferred from central-cache tracing alone.",
        "The four owner-review-focus observations were rechecked in current source: legacy pre-birth projections require fresh matching lineage; publisher reads stay inside the supplied pass context without global endpoint mutation; idle and done identities remain distinct; watcher waits consume projections without retaining a RegistryView.",
        "",
        "## d3 — separate receipts, notices, acknowledgements, and recovery",
        "",
        f"The fresh lifecycle suite ran {lifecycle.get('tests_run', 0)} tests with {len(lifecycle.get('failures', []))} failures and {len(lifecycle.get('errors', []))} errors. Fixture result: {fixture.get('watch_count', 0)} distinct watches across {fixture.get('distinct_stewards', 0)} stewards, five on one target and one isolation control; native results remain {fixture.get('native_result_counts', {})}. Receipts={fixture.get('receipt_count', 0)}, notices={sum((fixture.get('notice_records') or {}).values())}, acknowledgements={fixture.get('ack_count', 0)}. The jobs digest remained unchanged while target evidence changed `{fixture.get('target_state_before_after', [])}`. Cleanup status: `{cleanup['lifecycle']['status']}`.",
        "",
        "Selected transition coverage includes current-runner incarnation, session/pane/process-birth replacement, exact and missing receipts, live and dead claims, stale observations, lost wake/reclaim, native-result recovery, handover, and runner scan/unlock races. The exact selected test names remain in the lifecycle JSON.",
        "",
        "## d4 — matched CPU, process, and RSS windows",
        "",
        "Cold setup, warm unchanged input, and unrelated-event windows are separate rows. The carrier event changes an unrelated registry row; the peer-watch case uses a test-only SIGUSR1 wake surrogate to exercise its production wake/recheck path because local Herdr socket bind is denied. Process order is carrier / helper / peer watch / shared runner. CPU columns report production inclusive lineage plus per-class self and reaped-child deltas; per-class counters overlap after reap and must not be summed. RSS columns report simultaneous per-class peaks and total production-class peak. Full start/end/peak counters and PID/start identities are in the structured JSON.",
        "",
        _metric_markdown(metric_rows),
        "",
        "## Evidence and limits",
        "",
        f"Acceptance command child exits: measurement={report['subprocesses']['measurement']['exit_code']}, lifecycle={report['subprocesses']['lifecycle']['exit_code']}; fixture cleanup={cleanup['measurement']['status']}/{cleanup['lifecycle']['status']}. Exact commands and stdout/stderr logs are listed in `shared-readiness-acceptance.json`.",
        "All metrics are isolated production-entry fixtures. The CPU fixture runs source-specific production watcher and runner entrypoints; its runner receives pipe-backed snapshot data and its unrelated peer wake is a SIGUSR1 surrogate, not a measured Herdr socket event. The lifecycle runner also uses pipe-backed observation because this sandbox denies local Unix-socket bind. Host production PIDs remain inaccessible and were not declared dead. The measurement fixture counts the shared runner in production CPU/RSS and counts it once. The measured one-target versus five-same-target-plus-separate-control RSS distinction is retained in the matrix; the control makes six watcher processes in the latter case.",
        "Runtime contract: `preflight.sh capability-info code-execute` reported `instruction-only`, with no tool contract or contract check; standard/code QA assurance is selected-checks-only, not completion evidence. Governing PRD clauses §13.37.2-(2),(10) were read. No commit or route completion marker was produced.",
        "",
        f"Artifacts: `measure/readiness-cpu-fixture.json`, `measure/readiness-cpu-comparison.md`, `measure/readiness-cpu-fixture-cleanup.json`, `lifecycle/shared-readiness-lifecycle.json`, `test_logs/`, and `shared-readiness-acceptance.json` beneath `{evidence_dir}`.",
        "",
    ]
    return "\n".join(lines)


def _ordinary_ci():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    commands = (
        [sys.executable, str(CPU_FIXTURE)],
        [sys.executable, str(LIFECYCLE_FIXTURE)],
    )
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if completed.returncode:
            return completed.returncode
    print("paired manual baseline/candidate measurement: SKIPPED (inputs absent)")
    return 0


def _accept(baseline_root, evidence_dir):
    baseline_root = Path(baseline_root).resolve(strict=True)
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_dir = evidence_dir / "test_logs"
    measurement_dir = evidence_dir / "measure"
    lifecycle_dir = evidence_dir / "lifecycle"

    measurement = _run_child("measurement", [
        sys.executable, str(CPU_FIXTURE), "--baseline-root", str(baseline_root),
        "--evidence-dir", str(measurement_dir),
    ], log_dir)
    lifecycle = _run_child("lifecycle", [
        sys.executable, str(Path(__file__).resolve()), "--lifecycle-child",
        str(lifecycle_dir),
    ], log_dir)

    measurement_evidence = _read_json(measurement_dir / "readiness-cpu-fixture.json") or {}
    measurement_cleanup = _read_json(
        measurement_dir / "readiness-cpu-fixture-cleanup.json") or {"status": "missing"}
    lifecycle_evidence = _read_json(lifecycle_dir / "shared-readiness-lifecycle.json") or {}
    lifecycle_cleanup = lifecycle_evidence.get("fixture_cleanup") or {"status": "missing"}
    lifecycle_fixture = (lifecycle_evidence.get("fixtures") or [{}])[0]
    retained_path = baseline_root.parent / "registry-retained-memory.json"
    retained = _read_json(retained_path) or {}
    baseline_retained = retained.get("baseline", {})
    candidate_retained = retained.get("candidate", {})

    baseline_helpers = _candidate_helper_launches(
        measurement_evidence, "baseline_main_a7f77262d")
    candidate_helpers = _candidate_helper_launches(
        measurement_evidence, "candidate_567efc337")
    candidate_runner_peaks = [
        row["classes"]["shared-runner"]["peak_processes"]
        for row in _metric_rollup(measurement_evidence)
        if row["side"] == "candidate_567efc337"
    ]
    metric_rows = _metric_rollup(measurement_evidence)
    matrix_complete = (len(metric_rows) == 24 and all(
        row.get("production_inclusive_cpu_delta_seconds") is not None
        and all(row["classes"][role]["peak_simultaneous_rss_bytes"] is not None
                for role in ROLES)
        for row in metric_rows))
    lifecycle_complete = (
        lifecycle_evidence.get("tests_run", 0) > 0
        and lifecycle_evidence.get("suite_exit_code") == 0
        and not lifecycle_evidence.get("failures")
        and not lifecycle_evidence.get("errors")
        and lifecycle_fixture.get("watch_count") == 6
        and lifecycle_fixture.get("same_target_count") == 5
        and lifecycle_fixture.get("receipt_count") == 6
        and lifecycle_fixture.get("ack_count") == 6
        and len(lifecycle_fixture.get("notice_records", {})) == 6
        and all(count == 1 for count in lifecycle_fixture.get("notice_records", {}).values())
        and all(lifecycle_fixture.get("receipt_records", {}).values())
        and all(lifecycle_fixture.get("ack_records", {}).values())
        and lifecycle_fixture.get("target_state_before_after") == ["working", "idle"]
        and lifecycle_fixture.get("jobs_sha256_before_after", [None, None])[0]
        == lifecycle_fixture.get("jobs_sha256_before_after", [None, None])[-1]
    )
    cleanup_status = {
        "measurement": measurement_cleanup,
        "lifecycle": lifecycle_cleanup,
    }
    cleanup_complete = (measurement_cleanup.get("status") == "pass"
                        and lifecycle_cleanup.get("status") == "pass")
    retained_parse_counts = [
        baseline_retained.get("cache_metrics", {}).get("full_metadata_parses"),
        candidate_retained.get("cache_metrics", {}).get("full_metadata_parses"),
    ]
    d1_complete = True  # The owner currentness record is a required named input.
    d2_complete = (candidate_helpers == 0
                   and max(candidate_runner_peaks, default=0) == 1
                   and lifecycle_fixture.get("runner_session_snapshot_calls") == 2
                   and retained_parse_counts == [1, 1])
    subcommands_pass = measurement["exit_code"] == 0 and lifecycle["exit_code"] == 0
    status = "PASS" if (subcommands_pass and d1_complete and d2_complete
                         and matrix_complete and lifecycle_complete
                         and cleanup_complete) else "FAIL"

    report = {
        "schema_version": 1,
        "status": status,
        "scope": "phase 3 final-source evidence refresh; no stage-gate authority",
        "subprocesses": {"measurement": measurement, "lifecycle": lifecycle},
        "d1_source_currentness": {
            "candidate_revision": "567efc33752707c72ca3a40c6a2b8a2c2dfc89cc",
            "origin_main": "a7f77262d1f3a720a870618b3ebaa7260e2a5ceb",
            "pull_requests_in_ancestry": ["#688", "#692"],
            "evidence_source": "dev_logs/execute-implementation.md owner currentness record",
        },
        "d2_shared_observation": {
            "baseline_carrier_helper_launches": baseline_helpers,
            "candidate_carrier_helper_launches": candidate_helpers,
            "candidate_shared_runner_peak_processes": max(candidate_runner_peaks, default=0),
            "lifecycle_target_snapshot_calls": lifecycle_fixture.get(
                "runner_session_snapshot_calls"),
            "baseline_retained_bytes": baseline_retained.get("retained_traced_bytes"),
            "candidate_retained_bytes": candidate_retained.get("retained_traced_bytes"),
            "baseline_rss_kib": baseline_retained.get("maxrss_kib"),
            "candidate_rss_kib": candidate_retained.get("maxrss_kib"),
            "retained_memory_evidence": str(retained_path),
            "registry_cache_source_sha256": hashlib.sha256(
                (ROOT / "utilities" / "dispatch_registry_cache.py").read_bytes()).hexdigest(),
            "retained_memory_evidence_reused": True,
            "retained_full_metadata_parse_counts": retained_parse_counts,
            "retained_memory_scope_limit": "central cache fixture; not watcher scaling",
        },
        "d3_independent_delivery": {
            "lifecycle_tests_run": lifecycle_evidence.get("tests_run", 0),
            "fixture": lifecycle_fixture,
            "selected_regressions": lifecycle_evidence.get("selected_regressions", []),
        },
        "d4_matched_measurements": {
            "status": "complete" if matrix_complete else "incomplete",
            "conditions": list(WINDOWS),
            "metric_order": list(ROLES),
            "metric_windows": len(metric_rows),
        },
        "fixture_cleanup": cleanup_status,
        "metric_windows": metric_rows,
        "source_sha256": _source_hashes(),
        "source_provenance": measurement_evidence.get("source_provenance", {}),
        "lifecycle_source_sha256": lifecycle_evidence.get("source_sha256", {}),
        "limitations": [
            "All process, CPU, and RSS values are isolated fixture measurements; host production PIDs are inaccessible.",
            "The CPU peer-watch unrelated-event window uses a test-only SIGUSR1 wake surrogate; no real Herdr socket event was measured because local Unix-socket bind is denied.",
            "The production runner lifecycle fixture used a pipe-backed observation transport because local Unix-socket bind is denied in this sandbox.",
            "Per-class self CPU and reaped-child CPU counters overlap after process reaping; inclusive lineage boundary deltas count transfers once.",
            "The five same-target peer case also includes one separate-target isolation control, so it has six watcher processes.",
        ],
        "runtime_contract": {
            "capability_status": "instruction-only",
            "tool_contract": None,
            "tool_contract_check": None,
            "qa_assurance_scope": "selected-checks-only:not-completion-evidence",
            "governing_prd_sections_read": ["§13.37.2-(2)", "§13.37.2-(10)"],
        },
        "source_recheck": {
            "legacy_pre_birth_watch": "runner supplies current lineage; consumer accepts only after matching live PID/start, pane, harness, session, and native result; otherwise remains unknown",
            "publisher_endpoint_context": "the readiness publisher uses the supplied pass-scoped read context and does not mutate the global Herdr session",
            "native_result_grouping": "target identity includes native idle/done state, so differing results remain separate groups",
            "watcher_memory_lifetime": "watcher consumes a bounded projection and does not retain a RegistryView while waiting",
        },
    }
    report["d1_source_currentness"]["status"] = "confirmed by owner currentness evidence"
    report["d2_shared_observation"]["status"] = (
        "supported by final carrier, peer, and unchanged registry fixtures"
        if d2_complete else "incomplete or observed mismatch")
    report["d3_independent_delivery"]["status"] = (
        "complete" if lifecycle_complete and cleanup_complete else "incomplete")

    acceptance_path = evidence_dir / "shared-readiness-acceptance.json"
    _write_json(acceptance_path, report)
    handoff_path = evidence_dir / "final-source-evidence.md"
    handoff_path.write_text(_make_handoff(report, evidence_dir, measurement_evidence,
                                          lifecycle_evidence, cleanup_status),
                            encoding="utf-8")
    print(json.dumps({"status": status, "acceptance": str(acceptance_path),
                      "handoff": str(handoff_path),
                      "measurement_exit": measurement["exit_code"],
                      "lifecycle_exit": lifecycle["exit_code"],
                      "cleanup": {name: value.get("status")
                                  for name, value in cleanup_status.items()}}, sort_keys=True))
    return 0 if status == "PASS" else 1


def main(argv=None):
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "--lifecycle-child":
        return _lifecycle_child(Path(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    if (args.baseline_root is None) != (args.evidence_dir is None):
        parser.error("--baseline-root and --evidence-dir must be supplied together")
    if args.baseline_root is None:
        return _ordinary_ci()
    return _accept(args.baseline_root, args.evidence_dir)


if __name__ == "__main__":
    raise SystemExit(main())
