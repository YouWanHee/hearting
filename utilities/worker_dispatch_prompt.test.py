#!/usr/bin/env python3

import csv
import io
import json
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import frame_interview as FI
import workflow_state as WS
import worker_bootstrap as WB

ROOT = Path(__file__).resolve().parents[1]
LIVE_O1_VALUES = (1, 2)
LIVE_O1_RESULT = sum(LIVE_O1_VALUES)
LIVE_O1_COMMAND = "python3 -c 'print(sum([" + ",".join(map(str, LIVE_O1_VALUES)) + "]))'"
LIVE_CSV_FIXTURE = (
    "category,value\n"
    "A,2\nB,5\nA,7\nC,4\nB,1\nC,8\n"
)
LIVE_TODO_FIXTURES = {
    "first.txt": "TODO: first item\nReviewed\n",
    "second.txt": "TODO: second item\nTODO: third item\n",
    "third.txt": "No marker found\n",
}


def _live_csv_expectations():
    rows = [(row["category"], int(row["value"]))
            for row in csv.DictReader(io.StringIO(LIVE_CSV_FIXTURE))]
    groups = {}
    for category, value in rows:
        groups[category] = groups.get(category, 0) + value
    return rows, {"count": len(rows), "total": sum(value for _, value in rows),
                  "groups": groups}


LIVE_CSV_ROWS, LIVE_CSV_TOTALS = _live_csv_expectations()
LIVE_TODO_COUNTS = {
    name: len(re.findall(r"\bTODO\b", body))
    for name, body in LIVE_TODO_FIXTURES.items()
}


def _live_csv_records_from_output(output):
    records = []
    for line in str(output or "").splitlines():
        try:
            row = next(csv.reader([line], strict=True))
        except csv.Error:
            continue
        if len(row) == 2:
            records.append(tuple(cell.strip() for cell in row))
    return records


def _live_coverage_rows(coverage):
    lines = str(coverage or "").splitlines()
    header = None
    header_line = None
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        normalized = [cell.casefold() for cell in cells]
        if "file" in normalized and "todo count" in normalized:
            header = (normalized.index("file"), normalized.index("todo count"),
                      normalized.index("evidence") if "evidence" in normalized else None)
            header_line = index
            break
    if header is None:
        return None, {}, []

    file_column, count_column, evidence_column = header
    file_rows = {}
    total_rows = []
    for line in lines[header_line + 1:]:
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(not cell or re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if max(file_column, count_column) >= len(cells):
            continue
        filename = cells[file_column]
        raw_count = cells[count_column]
        count = int(raw_count) if re.fullmatch(r"\d+", raw_count) else None
        evidence = cells[evidence_column] if evidence_column is not None and evidence_column < len(cells) else ""
        if filename.casefold() == "total":
            total_rows.append(count)
        elif filename:
            file_rows.setdefault(filename, []).append((count, evidence))
    return header, file_rows, total_rows


def _clean_test_env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_REVIEW_",
                                  "AGENT_ARTIFACT_", "AGENT_MODEL_GOVERNOR_"))
           and key not in {"AGENT_HOME", "CODEX_HOME"}}
    env["AGENT_HOME"] = str(ROOT)
    return env


ADAPTERS = {
    "codex": (
        ROOT / "adapters/codex/bin/dispatch-headless.py",
        ["--model", "gpt-test", "--reasoning", "low"],
        "codex.prompt.txt",
    ),
    "claude": (
        ROOT / "adapters/claude/bin/dispatch-headless.py",
        ["--model", "claude-test", "--effort", "low"],
        "claude.prompt.txt",
    ),
    "opencode": (
        ROOT / "adapters/opencode/bin/dispatch-headless.py",
        ["--model", "provider/test", "--variant", "low"],
        "opencode.prompt.txt",
    ),
}


def _live_solo_findings(workdir, final_text, events):
    """Compare a live run's files, tool observations, and final status to O1–O3."""
    findings = []
    tools = []
    for index, event in enumerate(events):
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = str(item.get("command") or "")
        output = str(item.get("aggregated_output") or item.get("output") or "")
        tools.append((index, command, output, item.get("exit_code"), item.get("status")))

    successful = lambda row: row[3] == 0 or (row[3] is None and row[4] == "completed")
    o1 = [(i, cmd, out) for i, cmd, out, code, status in tools
          if successful((i, cmd, out, code, status))
          and LIVE_O1_COMMAND in cmd and str(LIVE_O1_RESULT) in out.split()]
    if not o1:
        findings.append(f"O1 command/result event for sum([1,2]) -> {LIVE_O1_RESULT} is missing")
    o1_index = o1[-1][0] if o1 else len(events)

    csv_path = Path(workdir) / "rows.csv"
    later_csv = [(i, cmd, out) for i, cmd, out, code, status in tools
                 if i > o1_index and csv_path.name in cmd
                 and successful((i, cmd, out, code, status))]
    if not later_csv:
        findings.append("O2 CSV inspection event after O1 is missing")
    else:
        expected_csv_records = [("category", "value")]
        expected_csv_records.extend((category, str(value)) for category, value in LIVE_CSV_ROWS)
        if not any(_live_csv_records_from_output(out) == expected_csv_records
                   for _, _, out in later_csv):
            findings.append("O2 successful rows.csv output does not establish all six fixture records")

    totals_path = Path(workdir) / "totals.json"
    try:
        totals = json.loads(totals_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        totals = None
        findings.append(f"O2 totals.json is missing or invalid: {exc}")
    if totals is not None and totals != LIVE_CSV_TOTALS:
        findings.append(f"O2 totals.json differs from fixture-derived {LIVE_CSV_TOTALS!r}: {totals!r}")

    coverage_path = Path(workdir) / "coverage.md"
    try:
        coverage = coverage_path.read_text(encoding="utf-8")
    except OSError as exc:
        coverage = ""
        findings.append(f"O3 coverage.md is missing: {exc}")
    coverage_header, coverage_rows, total_rows = _live_coverage_rows(coverage)
    if coverage_header is None:
        findings.append("O3 coverage.md exact File/TODO count table header is missing")
    counts = LIVE_TODO_COUNTS
    for filename, count in counts.items():
        observations = []
        for i, command, output, code, status in tools:
            if i <= o1_index or filename not in command or not successful((i, command, output, code, status)):
                continue
            for line in output.splitlines():
                match = re.fullmatch(rf"\s*{re.escape(filename)}\s*:\s*(\d+)\s*", line)
                if match:
                    observations.append(int(match.group(1)))
        if not observations:
            findings.append(f"O3 successful TODO count observation for {filename} after O1 is missing")
        elif set(observations) != {count}:
            findings.append(f"O3 observed TODO count for {filename} differs from fixture-derived {count}: {observations!r}")

        reported = coverage_rows.get(filename, [])
        if not reported:
            findings.append(f"O3 exact coverage table row for {filename} is missing")
        else:
            if len(reported) != 1:
                findings.append(f"O3 coverage table must contain exactly one row for {filename}")
            for reported_count, evidence in reported:
                if reported_count is None:
                    findings.append(f"O3 coverage table count cell for {filename} is not an exact integer")
                elif reported_count != count or (observations and reported_count not in observations):
                    findings.append(
                        f"O3 coverage count for {filename}={reported_count} does not match "
                        f"its observation and fixture-derived count {count}")
                if coverage_header is not None and not evidence:
                    findings.append(f"O3 coverage evidence cell for {filename} is empty")
    expected_todo_total = sum(LIVE_TODO_COUNTS.values())
    if total_rows != [expected_todo_total]:
        findings.append(f"O3 exact Total table cell must report {expected_todo_total}; got {total_rows!r}")
    if not re.search(r"\b(command|evidence)\b|```|`(?:grep|python3|cat|sed|rg)\b", coverage, re.I):
        findings.append("O3 command/evidence reference is missing from coverage.md")

    # A finite machine-readable report avoids interpreting words such as "not complete".
    final = str(final_text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", final, re.S | re.I)
    if fenced:
        final = fenced.group(1)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate report key: {key}")
            result[key] = value
        return result

    try:
        report = json.loads(final, object_pairs_hook=unique_object)
    except ValueError:
        report = None
    expected_report = {
        "O1": {"status": "complete", "result": LIVE_O1_RESULT},
        "O2": {"status": "complete", **LIVE_CSV_TOTALS},
        "O3": {"status": "complete", "counts": LIVE_TODO_COUNTS,
               "total": expected_todo_total},
    }
    if not isinstance(report, dict) or set(report) != set(expected_report):
        findings.append("Final report must contain exactly O1, O2, and O3 JSON results")
    else:
        for marker, expected in expected_report.items():
            # JSON serialization also distinguishes counts from booleans.
            if json.dumps(report[marker], sort_keys=True) != json.dumps(expected, sort_keys=True):
                findings.append(f"Final {marker} status/results contradict fixture-backed completion")
    return findings


def _live_solo_judgment(returncode, findings, events, error=None):
    """Keep runtime refusal distinct from observed incomplete or wrong behavior."""
    if error:
        return "BLOCKED"
    runtime_text = "\n".join(
        str(item.get("aggregated_output") or item.get("output") or "")
        for event in events if isinstance(event, dict)
        for item in [event.get("item")]
        if isinstance(item, dict) and item.get("type") == "command_execution"
    ).lower()
    if any(marker in runtime_text for marker in (
        "app-server socket directory", "error building bubblewrap command",
        "not inside a trusted directory", "not logged in", "authentication failed",
    )):
        return "BLOCKED"
    return "PASS" if returncode == 0 and not findings else "FAIL"


class WorkerDispatchPromptTest(unittest.TestCase):
    def setUp(self):
        self.parents = []

    def tearDown(self):
        for proc in self.parents:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_custom_assignment_is_wrapped_for_every_adapter(self):
        for harness, (wrapper, model, suffix) in ADAPTERS.items():
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.email", "fixture@example.com"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                    check=True,
                )
                (repo / "x").write_text("x", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
                artifact_root = root / ".agent_reports"
                artifact_root.mkdir()
                logs = root / "logs"
                jobs = root / "jobs.log"
                if harness in {"codex", "claude"}:
                    parent = subprocess.Popen(["sleep", "60"])
                    self.parents.append(parent)
                    start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
                    jobs.write_text(
                        f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                        "execution_surface=registered-headless,registered_worker=1,"
                        "fallback_hop=same-harness-headless,worker_type=owner,"
                        f"harness={harness},runtime_sandbox=workspace-write,"
                        f"attempt_id=att-prompt-parent,pid={parent.pid},pid_start={start}\n"
                    )
                slug = f"{harness}-typed"
                if harness == "opencode":
                    topology = [
                        "--dispatch-depth", "1",
                        "--worker-type", "owner",
                        "--unit", "_kernel/owner",
                        "--assigned-contract", "autopilot-code",
                    ]
                    mode_axes = ["--capability-mode", "dev"]
                    expected_worker_type = "Owner"
                    expected_contract = "autopilot-code"
                else:
                    topology = [
                        "--dispatch-depth", "2",
                        "--parent", "owner",
                        "--worker-type", "stage",
                        "--assigned-contract", "code-test",
                    ]
                    mode_axes = [
                        "--capability-mode", "dev",
                        "--worker-mode", "dev/backend",
                    ]
                    expected_worker_type = "Stage"
                    expected_contract = "code-test"
                command = [
                    sys.executable,
                    str(wrapper),
                    "--register",
                    "--worktree",
                    str(repo),
                    "--slug",
                    slug,
                    "--capability",
                    "autopilot-code",
                    *mode_axes,
                    "--intensity",
                    "standard",
                    *topology,
                    *(
                        [
                            "--parent-harness", harness,
                            "--parent-transport", "headless",
                            "--parent-sandbox", "workspace-write",
                            "--nested-eligibility", "supported",
                            "--eligibility-source", "fixture",
                        ]
                        if harness in {"codex", "claude"}
                        else []
                    ),
                    "--prompt-text",
                    "CUSTOM ASSIGNMENT",
                    "--jobs",
                    str(jobs),
                    "--log-dir",
                    str(logs),
                    *model,
                ]
                env = {
                    **_clean_test_env(),
                    "AGENT_HOME": str(ROOT),
                    "AGENT_ARTIFACT_ROOT": str(artifact_root),
                    "AGENT_ARTIFACT_CYCLE_ID": "cyc-concrete-prompt",
                    "AGENT_ARTIFACT_CYCLE_DIR": str(artifact_root / "campaigns" / "stream" / "current"),
                    "AGENT_ARTIFACT_OUTPUT_DIR": "",
                    "OPENCODE_CONFIG_CONTENT": "{}",
                }
                if harness in {"codex", "claude"}:
                    env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-prompt-parent"
                result = subprocess.run(command, text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prompts = list(logs.glob(f"{slug}.*.{suffix}"))
                self.assertEqual(len(prompts), 1, prompts)
                prompt = prompts[0].read_text(encoding="utf-8")
                self.assertEqual(prompt.count("# Portable Worker Kernel"), 1)
                self.assertEqual(prompt.count("# Worker Type:"), 1)
                self.assertIn(f"# Worker Type: {expected_worker_type}", prompt)
                self.assertIn(f"- assigned_contract: {expected_contract}", prompt)
                self.assertNotIn("- worker_role:", prompt)
                self.assertIn("CUSTOM ASSIGNMENT", prompt)
                self.assertIn("- artifact_cycle_id: cyc-concrete-prompt", prompt)
                self.assertIn(f"- artifact_output_dir: {artifact_root}/campaigns/stream/current/artifacts", prompt)
                self.assertIn("artifact: <canonical path | ->", prompt)
                self.assertIn("verdict: PASS | FAIL | BLOCKED", prompt)
                self.assertIn("blocker: none | <one line>", prompt)
                self.assertNotIn("Read $AGENT_HOME/adapters/codex/AGENTS.md first", prompt)
                self.assertNotIn("Return a concise report with changed files", prompt)
                if harness in {"codex", "claude"}:
                    # SD-45 guard-identity parity (round_1 finding 4): the
                    # dispatch metadata's guard_session_id must equal the
                    # exact attempt id the wrapper minted for this row — the
                    # same identity embedded in the prompt filename itself —
                    # and the field name/position must match across adapters.
                    prompt_attempt_id = prompts[0].name.split(".")[1]
                    self.assertIn(f"- guard_session_id: {prompt_attempt_id}\n", prompt)
                    self.assertNotIn("- guard_session_id: codex-headless", prompt)
                    self.assertNotIn("codex-headless", prompt)

    def test_three_adapters_leave_progress_bookkeeping_to_runtime(self):
        for harness, (wrapper, model, _suffix) in ADAPTERS.items():
            with self.subTest(harness=harness):
                spec=importlib.util.spec_from_file_location(f"dispatch_{harness}",wrapper)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                args=module.parser().parse_args([
                    "--worktree","/work/repo","--slug","stage","--capability","autopilot-code",
                    "--capability-mode","dev","--worker-mode","dev/backend",
                    "--unit","dev/backend","--intensity","standard","--dispatch-depth","2",
                    "--parent","owner","--worker-type","stage",
                    "--assigned-contract","code-test","--prompt-text","TASK",*model,
                ])
                args.attempt_id="att-promptheartbeat01";args.route_id="rt-prompt";args.route_node="test"
                args.artifact_root="/artifacts"
                render=module.prompt if harness=="opencode" else module.dispatch_prompt
                prompt,_=render(args)
                self.assertIn("The runtime observes tool progress",prompt)
                self.assertIn("No per-tool heartbeat command is required",prompt)
                self.assertNotIn("--phase analysis",prompt)
                self.assertNotIn("Stage progress contract",prompt)
                self.assertIn("TASK",prompt)


    def test_completion_scope_and_original_request_reach_all_adapter_prompts(self):
        for harness, (wrapper, model, _suffix) in ADAPTERS.items():
            spec = importlib.util.spec_from_file_location("completion_" + harness, wrapper)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for worker_type, intensity, depth, unit in (
                ("owner", "quick", "1", "_kernel/owner"),
                ("stage", "standard", "2", "dev/backend"),
            ):
                for endpoint in ("complete", "report"):
                    for with_done_when in (False, True):
                        with self.subTest(harness=harness, worker_type=worker_type,
                                          endpoint=endpoint, done_when=with_done_when), \
                                tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            node_id = "one-shot" if worker_type == "owner" else "execute"
                            output_dir = root / "cycle" / "artifacts"
                            output_dir.mkdir(parents=True)
                            route = {
                                "route_id": "rt-prompt-completion",
                                "cwd": str(root),
                                "artifact_root": str(root / "reports"),
                                "nodes": [{
                                    "id": node_id, "unit": unit, "worker_type": worker_type,
                                    "outputs": ["dev_logs/result.md"],
                                    "write_scope": ["source/**", "dev_logs/**"],
                                }],
                            }
                            route_file = root / "route.json"
                            route_file.write_text(json.dumps(route), encoding="utf-8")
                            task = ("Original request: O1 calculate and record the first result; "
                                    "O2 calculate every group total; O3 inspect every named file "
                                    f"and report all results. Execution scope: {endpoint}.")
                            axes = ["--worktree", str(root), "--slug", "completion",
                                    "--capability", "autopilot-code", "--capability-mode", "dev",
                                    "--intensity", intensity, "--dispatch-depth", depth,
                                    "--worker-type", worker_type, "--unit", unit,
                                    "--prompt-text", task, "--jobs", str(root / "jobs.log"), *model]
                            args = module.parser().parse_args(axes)
                            args.route_file = str(route_file)
                            args.route_node = node_id
                            args.artifact_root = str(root / "reports")
                            args.agent_home = module.resolve_agent_home()
                            render = module.prompt if harness == "opencode" else module.dispatch_prompt
                            plan = ("Plan for this leg (approved in the frame interview):\n"
                                    "Done when (sealed ids, in this order):\n- fixture: retain optional plan duties\n\n") \
                                if with_done_when else ""
                            # Exercise both optional plan shapes at the existing rendering seam.
                            with mock.patch.object(WB, "plan_leg_prompt", return_value=plan), \
                                    mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": str(output_dir)}):
                                prompt, _ = render(args)
                            self.assertIn(task, prompt)
                            self.assertIn("Completion precedence:", prompt)
                            self.assertIn("approved endpoint and this node's assigned scope", prompt)
                            self.assertIn("Stop at an approved report endpoint", prompt)
                            self.assertIn("outputs=[", prompt)
                            self.assertIn(str(output_dir / "dev_logs" / "result.md"), prompt)
                            self.assertEqual("Done when (sealed ids" in prompt, with_done_when)
                            if endpoint == "report":
                                self.assertIn("Execution scope: report.", prompt)
                                self.assertIn("bounded stage completes only its assigned portion", prompt)


class FrameAssignmentPromptTest(unittest.TestCase):
    def test_real_adapter_prompts_distinguish_goal_report_from_current_frame_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route = root / "route.json"
            route.write_text(json.dumps({"nodes":[{"id":"frame-alternative",
                "outputs":["shards/frame-alternative/direction-brief.md"]}]}))
            for harness, (wrapper, model, _) in ADAPTERS.items():
                with self.subTest(harness=harness), mock.patch.dict(os.environ,
                        {"AGENT_ARTIFACT_OUTPUT_DIR":str(root / "artifacts")}):
                    spec = importlib.util.spec_from_file_location("frame_assignment_"+harness, wrapper)
                    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                    args = module.parser().parse_args(["--worktree",str(root),"--slug","frame-task",
                        "--capability","autopilot-code","--capability-mode","dev","--worker-mode","plan/frame",
                        "--worker-type","frame","--unit","plan/frame","--intensity","standard",
                        "--dispatch-depth","1","--prompt-text","Write the final task results to final_report.md.",*model])
                    args.artifact_root=str(root);args.route_file=str(route);args.route_node="frame-alternative"
                    args.route_id="rt-frame"
                    # SD-156: `dispatch_prompt`/`prompt` now call
                    # `diff_attribution_prompt`, which reads `args.agent_home` --
                    # `main()` sets this post-parse in real operation
                    # (`args.agent_home = resolve_agent_home()`); this fixture
                    # calls the prompt builder directly, so it must set it too.
                    args.agent_home=module.resolve_agent_home()
                    render=module.prompt if harness=="opencode" else module.dispatch_prompt
                    text,_=render(args)
                    self.assertIn("User goal to analyze (the later owner's task)",text)
                    self.assertIn("final_report.md",text)
                    self.assertIn(str(root / "artifacts/shards/frame-alternative/direction-brief.md"),text)
                    self.assertNotIn("Assignment:\nWrite the final task results",text)
                    self.assertIn("only its own declared brief",text)
                    self.assertEqual(args.assigned_contract,"plan/frame")
                    self.assertIn("frame unit contract is already included",text)
                    self.assertNotIn("Read only the assigned autopilot-code",text)
                    self.assertNotIn("Read only the exposed autopilot-code",text)
                    self.assertNotIn("skills/autopilot-code/SKILL.md",text)

    def test_legacy_frame_keeps_its_path_and_nonframe_assignment_gets_completion_precedence(self):
        frame=WB.assignment_prompt(SimpleNamespace(worker_type="frame"),"Analyze this work; output /tmp/brief.md",{})
        self.assertIn("/tmp/brief.md",frame)
        for kind in ("owner","stage","review","support"):
            text = WB.assignment_prompt(SimpleNamespace(worker_type=kind),"TASK",{})
            self.assertTrue(text.startswith("Assignment:\nTASK\n\nCompletion precedence:"))
            self.assertIn("approved endpoint and this node's assigned scope", text)


class ReleasedTaskPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "canonical" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.touch()
        self.route_id = "rt-released-context"
        self.interview = self.root / "issued-cycle" / "interview.json"
        self.interview.parent.mkdir()
        self.value = {
            "schema": FI.SCHEMA, "route_id": self.route_id, "round": 1,
            "understanding": "Run only sum([1,2]) and record the observed result.",
            "brief": {"constraints": "No source edits or repository audit."},
            "questions": [{"id": "record-failure", "topic": "Evidence",
                "question": "Which observations should be recorded?",
                "options": [{"label": "Both", "means": "Record the alias failure and python3 success."},
                            {"label": "Success", "means": "Record the successful command."}],
                "recommended": 0}],
        }
        self.interview.write_text(json.dumps(self.value))
        self.answers = FI.answers_template(self.value)
        self.answers["actor_kind"] = "user"
        self.answers["understanding_confirmed"] = True
        self.answers["answers"]["record-failure"] = {"choice": 0, "note": "Keep the failure."}
        self.ledger = WS.WorkflowLedger(self.route_id, jobs=self.jobs)
        self.ledger.root.mkdir(parents=True)
        self.raised = {"at": "2026-09-11T11:00:00Z", "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review", "artifact": str(self.interview),
                         "interview": True, "questions": 1}}
        self.released = {"at": "2026-09-11T11:01:00Z", "workflow_state": "READY",
            "evidence": {"released_gate": "frame-review", "decision": "proceed",
                         "answers": self.answers}}
        self.write_journal(self.raised, self.released)

    def write_journal(self, *entries):
        self.ledger.journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def args(self, **overrides):
        return SimpleNamespace(**{"worker_type": "stage", "route_id": self.route_id,
                                  "jobs": self.jobs, **overrides})

    def test_legacy_context_uses_only_recorded_actor_and_does_not_rewrite_history(self):
        legacy = dict(self.answers)
        del legacy["actor_kind"]
        for kind in ("user", "supervisor", None):
            with self.subTest(kind=kind):
                released = {**self.released, "evidence": {**self.released["evidence"], "answers": legacy,
                                                         "actor_kind": kind}}
                self.write_journal(self.raised, released)
                before = self.ledger.journal_path.read_bytes()
                context = WB.released_task_prompt(self.args())
                self.assertIn("actor_kind: " + (kind or "unknown"), context)
                self.assertIn("The recorded scope and decisions below govern this work.", context)
                self.assertIn("role and intensity defaults neither expand nor shrink it", context)
                self.assertIn("Quick examples and concise reporting are procedure defaults", context)
                self.assertNotIn("The recorded user scope", context)
                self.assertEqual(self.ledger.journal_path.read_bytes(), before)
                self.assertNotIn("actor_kind", legacy)
                if kind != "user":
                    self.assertNotIn("User's note", context)

    def test_no_plan_stage_or_intent_copy_needed_in_all_three_adapters(self):
        for harness, (wrapper, model, _suffix) in ADAPTERS.items():
            spec = importlib.util.spec_from_file_location("context_" + harness, wrapper)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for worker_type, unit, depth in (("owner", "_kernel/owner", "1"),
                                             ("stage", "qa/test", "2"),
                                             ("review", "qa/code-review", "2")):
                with self.subTest(harness=harness, worker_type=worker_type):
                    args = module.parser().parse_args([
                        "--worktree", str(self.root), "--slug", "context", "--capability", "autopilot-code",
                        "--capability-mode", "dev", "--intensity", "standard", "--dispatch-depth", depth,
                        "--worker-type", worker_type, "--unit", unit, "--jobs", str(self.jobs), *model])
                    args.attempt_id = "att-context"
                    args.route_id = None if worker_type == "owner" else self.route_id
                    if worker_type == "owner":
                        args.owner_route_binding = SimpleNamespace(route_id=self.route_id, route_file="/issued/route.json")
                    args.artifact_root = str(self.root)
                    render = module.prompt if harness == "opencode" else module.dispatch_prompt
                    for custom in (None, "Run the assigned stage with this extra detail."):
                        args.prompt_text = custom
                        # A different ambient cycle must never supply the task.
                        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": "/wrong-cycle/artifacts"}):
                            prompt, _ = render(args)
                        self.assertIn("Run only sum([1,2])", prompt)
                        self.assertIn("Record the alias failure and python3 success.", prompt)
                        self.assertIn("record-failure", prompt)
                        self.assertIn("No source edits or repository audit.", prompt)
                        if custom:
                            self.assertIn(custom, prompt)
                        self.assertFalse((self.interview.parent / "intent.md").exists())

    def test_latest_raise_drops_old_answers_and_frame_remains_independent(self):
        self.assertEqual(WB.released_task_prompt(self.args(worker_type="frame")), "")
        self.write_journal(self.raised, self.released, self.raised)
        self.assertEqual(WB.released_task_prompt(self.args()), "")
        self.write_journal(self.raised)
        self.assertEqual(WB.released_task_prompt(self.args()), "")

    def test_legacy_route_does_not_inherit_another_routes_context(self):
        self.assertEqual(WB.released_task_prompt(self.args(route_id="rt-unrelated")), "")
        self.assertEqual(WB.released_task_prompt(self.args(route_id=None)), "")
        fresh_jobs = self.root / "fresh-preview" / "jobs.log"
        self.assertEqual(WB.released_task_prompt(self.args(jobs=fresh_jobs)), "")
        self.assertFalse(fresh_jobs.parent.exists())

    def test_missing_or_foreign_input_returns_the_exact_recovery_path(self):
        self.interview.unlink()
        with self.assertRaisesRegex(ValueError, "Restore the recorded interview"):
            WB.released_task_prompt(self.args())
        self.interview.write_text(json.dumps({**self.value, "route_id": "rt-other"}))
        with self.assertRaisesRegex(ValueError, "different route"):
            WB.released_task_prompt(self.args())
        self.interview.write_text(json.dumps(self.value))
        self.released["evidence"]["answers"] = {**self.answers, "route_id": "rt-other"}
        self.write_journal(self.raised, self.released)
        with self.assertRaisesRegex(ValueError, "route_id"):
            WB.released_task_prompt(self.args())


class UnitBootstrapPromptTest(unittest.TestCase):
    """A declared unit gets the same bootstrap section from all three adapters, in the
    same place; a unit without a declaration gets none."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name).resolve()
        self.worktree = tmp / "proj-lab"
        self.worktree.mkdir()
        self.artifact_root = tmp / ".agent_reports"
        cycle = self.artifact_root / "campaigns" / "camp" / "2026-01-01_old"
        (cycle / "artifacts" / "experiments" / "e1").mkdir(parents=True)
        (cycle / "manifest.json").write_text("{}", encoding="utf-8")
        self.exemplar = cycle / "artifacts" / "experiments" / "e1" / "REPORT.md"
        self.exemplar.write_text("# Old report\n" + "x" * 2048, encoding="utf-8")
        store = tmp / "store"
        store.mkdir()
        env = _clean_test_env()
        env.update(XDG_STATE_HOME=str(tmp / "state"), MEM_STORE=str(store), MEM_RECALL_EVENTS=str(tmp / "events.jsonl"),
                   MEM_RECALL_RECEIPTS=str(tmp / "receipts"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        subprocess.run(
            [sys.executable, str(ROOT / "tools/memory/mem.py"), "add", "durable", "feedback",
             "Always ship the report as HTML with tables and audio players.", "--scope", "global",
             "--headline", "report format html deliverable preference"],
            check=True, capture_output=True, cwd=self.worktree)

    def _render(self, harness, unit, gate, node, worker_type="stage", capability="autopilot-lab"):
        wrapper, model, _suffix = ADAPTERS[harness]
        spec = importlib.util.spec_from_file_location(f"bootstrap_{harness}", wrapper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = module.parser().parse_args([
            "--worktree", str(self.worktree), "--slug", "bootstrap", "--capability", capability,
            "--capability-mode", "eval", "--intensity", "standard", "--dispatch-depth", "2",
            "--worker-type", worker_type, "--unit", unit, "--completion-gate", gate,
            "--prompt-text", "Write the lab evaluation report", "--jobs", str(self.worktree / "jobs.log"), *model])
        args.attempt_id = "att-bootstrap"
        args.route_id = None
        args.route_node = node
        args.artifact_root = str(self.artifact_root)
        render = module.prompt if harness == "opencode" else module.dispatch_prompt
        return render(args)[0]

    @staticmethod
    def _section(prompt):
        start = prompt.find("Unit bootstrap material")
        return prompt[start:prompt.find("Assignment:", start)] if start >= 0 else ""

    def test_three_adapters_carry_the_same_bootstrap_section(self):
        sections = {}
        for harness in ADAPTERS:
            with self.subTest(harness=harness):
                prompt = self._render(harness, "editorial/report", "lab-report", "report")
                section = self._section(prompt)
                sections[harness] = section
                self.assertIn("assigned_contract: autopilot-lab", prompt)
                self.assertIn("eval-procedure.md", prompt)
                self.assertIn(str(self.exemplar), section)
                self.assertIn("Always ship the report as HTML", section)
                self.assertTrue(section.endswith("\n\n"))
                self.assertLess(prompt.index("Unit bootstrap material"), prompt.index("Assignment:"))
        self.assertEqual(len(set(sections.values())), 1)
        self.assertTrue(next(iter(sections.values())))

    def test_undeclared_stage_prompt_unchanged(self):
        for harness in ADAPTERS:
            for worker_type, unit, gate, node in (("stage", "qa/test", "code-test", "test"),
                                                  ("review", "qa/test", "lab-independent-verify", "independent-verify")):
                with self.subTest(harness=harness, worker_type=worker_type):
                    prompt = self._render(harness, unit, gate, node, worker_type=worker_type)
                    self.assertNotIn("Unit bootstrap material", prompt)
                    self.assertNotIn("Always ship the report", prompt)

    def test_catalog_declarations(self):
        for unit in ("editorial/report", "research/research-survey", "editorial/polish"):
            self.assertTrue(WB.unit_bootstrap_declaration(ROOT, unit), unit)
        self.assertEqual(WB.unit_bootstrap_declaration(ROOT, "research/research-survey")["gates"], ("research-report",))
        self.assertEqual(WB.unit_bootstrap_declaration(ROOT, "editorial/polish")["gates"], ("audit-report",))
        for unit in ("qa/test", "dev/backend", "material/figure-gen", "_kernel/owner"):
            self.assertEqual(WB.unit_bootstrap_declaration(ROOT, unit), {}, unit)


class LiveSoloResultComparatorFixtureTest(unittest.TestCase):
    """Synthetic events and artifacts exercise live-result acceptance and rejection."""

    def test_fixture_requires_observed_and_reported_results_for_each_duty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(len(LIVE_CSV_ROWS), 6)
            self.assertEqual(LIVE_CSV_TOTALS,
                             {"count": 6, "total": 27, "groups": {"A": 9, "B": 6, "C": 12}})
            self.assertEqual(
                {name: len(re.findall(r"\bTODO\b", body))
                 for name, body in LIVE_TODO_FIXTURES.items()},
                {"first.txt": 1, "second.txt": 2, "third.txt": 0},
            )
            self.assertNotIn("TODO", LIVE_TODO_FIXTURES["third.txt"])
            (root / "rows.csv").write_text(LIVE_CSV_FIXTURE, encoding="utf-8")
            for name, body in LIVE_TODO_FIXTURES.items():
                (root / name).write_text(body, encoding="utf-8")
            (root / "totals.json").write_text(json.dumps(LIVE_CSV_TOTALS), encoding="utf-8")
            coverage = (
                "| File | TODO count | Evidence |\n"
                "| --- | ---: | --- |\n"
                "| first.txt | 1 | `python count first.txt`, fixture count 1 |\n"
                "| second.txt | 2 | `python count second.txt` |\n"
                "| third.txt | 0 | `python count third.txt` |\n"
                "| Total | 3 | command results above |\n"
            )
            (root / "coverage.md").write_text(coverage, encoding="utf-8")

            def command_event(command, output, exit_code=0, status="completed"):
                return {"type": "item.completed", "item": {"type": "command_execution",
                        "command": command, "aggregated_output": output,
                        "exit_code": exit_code, "status": status}}

            events = [
                command_event(LIVE_O1_COMMAND, f"{LIVE_O1_RESULT}\n"),
                command_event("cat rows.csv", LIVE_CSV_FIXTURE),
                *[command_event(f"python3 count-todos {name}", f"{name}: {count}\n")
                  for name, count in LIVE_TODO_COUNTS.items()],
            ]
            report = {
                "O1": {"status": "complete", "result": 3},
                "O2": {"status": "complete", "count": 6, "total": 27,
                       "groups": {"A": 9, "B": 6, "C": 12}},
                "O3": {"status": "complete", "counts": {"first.txt": 1,
                       "second.txt": 2, "third.txt": 0}, "total": 3},
            }
            final = json.dumps(report)
            self.assertEqual(_live_solo_findings(root, final, events), [])
            self.assertEqual(_live_solo_judgment(0, [], events), "PASS")
            self.assertEqual(_live_solo_findings(root, f"```json\n{final}\n```", events), [])

            for status in ("not complete", "incomplete", "blocked"):
                contradictory = json.loads(final)
                contradictory["O1"]["status"] = status
                findings = _live_solo_findings(root, json.dumps(contradictory), events)
                self.assertTrue(any("Final O1" in item for item in findings), findings)
                self.assertEqual(_live_solo_judgment(0, findings, events), "FAIL")
            for marker, field, value in (("O1", "result", 27), ("O2", "total", 3),
                                          ("O3", "counts", {"first.txt": True,
                                                           "second.txt": 2, "third.txt": 0})):
                incorrect = json.loads(final)
                incorrect[marker][field] = value
                findings = _live_solo_findings(root, json.dumps(incorrect), events)
                self.assertTrue(any(f"Final {marker}" in item for item in findings), findings)
            for invalid in ("O1 is not complete; O2 complete; O3 complete; 3 6 27 9 12",
                            '{"O1":{"status":"incomplete"},' + final[1:]):
                findings = _live_solo_findings(root, invalid, events)
                self.assertTrue(any("Final report" in item for item in findings), findings)
                self.assertEqual(_live_solo_judgment(0, findings, events), "FAIL")

            def assert_rejected(candidate_events, expected_finding, label):
                findings = _live_solo_findings(root, final, candidate_events)
                self.assertTrue(any(expected_finding in item for item in findings),
                                f"{label}: {findings}")
                self.assertEqual(_live_solo_judgment(0, findings, candidate_events), "FAIL", label)

            # Correct prefilled artifacts cannot substitute for observable O2/O3 results.
            assert_rejected(events[:1], "O2 CSV inspection event", "absent observations")

            partial_csv = "\n".join(LIVE_CSV_FIXTURE.splitlines()[:4]) + "\n"
            partial_events = [dict(event, item=dict(event["item"])) for event in events]
            partial_events[1]["item"]["aggregated_output"] = partial_csv
            assert_rejected(partial_events, "does not establish all six", "partial O2 rows")

            inconsistent_events = [dict(event, item=dict(event["item"])) for event in events]
            inconsistent_events[1]["item"]["aggregated_output"] = LIVE_CSV_FIXTURE.replace("B,5\n", "B,50\n")
            assert_rejected(inconsistent_events, "does not establish all six", "inconsistent O2 rows")

            out_of_order_events = events[1:] + events[:1]
            assert_rejected(out_of_order_events, "O2 CSV inspection event after O1 is missing",
                            "O2/O3 before O1")

            missing_file_events = events[:4]
            assert_rejected(missing_file_events, "observation for third.txt after O1 is missing",
                            "missing per-file observation")

            wrong_count_events = [dict(event, item=dict(event["item"])) for event in events]
            wrong_count_events[3]["item"]["aggregated_output"] = "second.txt: 9\n"
            assert_rejected(wrong_count_events, "observed TODO count for second.txt differs",
                            "incorrect per-file observation")

            failed_zero_events = [dict(event, item=dict(event["item"])) for event in events]
            failed_zero_events[4]["item"].update(aggregated_output="third.txt: 0\n",
                                                   exit_code=1, status="failed")
            assert_rejected(failed_zero_events, "observation for third.txt after O1 is missing",
                            "failed zero-count grep")

            wrong_cell = coverage.replace("| first.txt | 1 |", "| first.txt | 10 |")
            (root / "coverage.md").write_text(wrong_cell, encoding="utf-8")
            assert_rejected(events, "coverage count for first.txt=10", "wrong exact table cell")
            (root / "coverage.md").write_text(coverage.replace("| second.txt | 2 |", "| second.txt | 1 |"),
                                               encoding="utf-8")
            assert_rejected(events, "coverage count for second.txt=1", "reported/observed mismatch")
            (root / "coverage.md").write_text(coverage, encoding="utf-8")

            failed_o1_events = [dict(event, item=dict(event["item"])) for event in events]
            failed_o1_events[0]["item"].update(exit_code=1, status="failed")
            assert_rejected(failed_o1_events, "O1 command/result event", "failed O1")

            (root / "totals.json").unlink()
            findings = _live_solo_findings(root, final, events)
            self.assertTrue(any("O2 totals.json is missing" in item for item in findings), findings)
            self.assertEqual(_live_solo_judgment(0, findings, events), "FAIL")
            runtime_events = [{"item": {"type": "command_execution", "status": "failed",
                "exit_code": 1, "aggregated_output": "error building bubblewrap command: "
                "app-server socket directory must be a user-owned directory with mode 0700"}}]
            self.assertEqual(_live_solo_judgment(0, ["O1 was not observed"], runtime_events), "BLOCKED")


@unittest.skipUnless(os.environ.get("HEARTING_LIVE_SOLO") == "1",
                     "set HEARTING_LIVE_SOLO=1 with an eligible model to run the live check")
class LiveSoloContinuationTest(unittest.TestCase):
    """One synchronous native Codex run of the finite report-ended solo request."""

    def test_real_model_completes_o1_o2_o3_after_the_first_small_check(self):
        model = os.environ.get("HEARTING_LIVE_MODEL", "").strip()
        effort = os.environ.get("HEARTING_LIVE_EFFORT", "").strip()
        output = os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR", "").strip()
        if not model or not effort:
            self.fail("HEARTING_LIVE_MODEL and HEARTING_LIVE_EFFORT are required for the opt-in live run")
        if not output:
            self.fail("AGENT_ARTIFACT_OUTPUT_DIR must bind evidence to the current cycle")

        evidence_dir = Path(output).resolve() / "evidence" / "solo-live"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".hearting-solo-live-", dir=ROOT) as tmp:
            root = Path(tmp).resolve()
            (root / "rows.csv").write_text(LIVE_CSV_FIXTURE, encoding="utf-8")
            for name, body in LIVE_TODO_FIXTURES.items():
                (root / name).write_text(body, encoding="utf-8")
            self.assertNotIn("TODO", LIVE_TODO_FIXTURES["third.txt"])
            request = (
                "Finite solo work request. Approved endpoint: report. No implementation, "
                "deployment, or merge work. Complete these three distinct duties and then "
                "report the status of O1, O2, and O3. This request has no done_when field.\n\n"
                "O1 (first, a small quick-like check): run exactly "
                f"{LIVE_O1_COMMAND} and record its observed output.\n\n"
                f"O2 (remaining calculation): read all {len(LIVE_CSV_ROWS)} rows in {root / 'rows.csv'}, "
                f"print the header and all {len(LIVE_CSV_ROWS)} CSV data rows in a successful command output, "
                "then compute the row count, sum of values, and per-category totals; write "
                f"{root / 'totals.json'} as JSON with fields count, total, and groups.\n\n"
                "O3 (remaining exhaustive inspection and report): inspect each of these "
                f"three text files using commands that name the file: {root / 'first.txt'}, "
                f"{root / 'second.txt'}, and {root / 'third.txt'}. For each file, run a successful "
                "count command whose output includes a line in the form '<filename>: <count>', "
                "including for zero-count files; use a count method that exits successfully for zero. "
                "Record each observed count, their summed total, and command/evidence references in "
                f"{root / 'coverage.md'}. Use a table with file, TODO count, and evidence columns, "
                "including a Total row.\n\n"
                "Return only one final JSON object with exactly O1, O2, and O3 entries. "
                "O1 has status and result; O2 has status, count, total, and groups; "
                "O3 has status, counts (mapping each filename to its observed count), and total. "
                "Use integer results and status 'complete' only for fulfilled duties; otherwise "
                "report the actual status. Do not include other prose in the final response."
            )
            assignment = WB.assignment_prompt(SimpleNamespace(worker_type="owner"), request, {})
            prompt = ("Quick procedure: use an inline micro-plan, focused verification, "
                      "and a concise report.\n\n" + assignment)
            (evidence_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            command = ["codex", "exec", "--cd", str(root), "--sandbox", "workspace-write",
                       "--model", model, "-c", f"model_reasoning_effort={effort}",
                       "-c", "approval_policy=never", "--json", "-"]
            child_env = os.environ.copy()
            # A synchronous verification run is not a registered worker or route attempt.
            for name in (
                "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_PARENT_ATTEMPT_ID",
                "AGENT_DISPATCH_ROUTE_ID", "AGENT_DISPATCH_ROUTE_FILE",
                "AGENT_OWNER_ATTEMPT_ID", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH",
                "AGENT_ROUTE_ID", "AGENT_ROUTE_FILE",
            ):
                child_env.pop(name, None)
            error = None
            returncode = None
            stdout = ""
            stderr = ""
            try:
                result = subprocess.run(
                    command, input=prompt, text=True, capture_output=True, cwd=root,
                    env=child_env, timeout=900, check=False,
                )
                returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, subprocess.TimeoutExpired):
                    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            (evidence_dir / "stdout.jsonl").write_text(stdout, encoding="utf-8")
            (evidence_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
            events = []
            malformed = []
            for line_no, line in enumerate(stdout.splitlines(), 1):
                try:
                    value = json.loads(line)
                except ValueError:
                    malformed.append(line_no)
                    continue
                if isinstance(value, dict):
                    events.append(value)
            final_parts = []
            for event in events:
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    final_parts.append(str(item.get("text") or ""))
                if event.get("type") == "turn.completed":
                    final_parts.append(str(event.get("last_agent_message") or ""))
            final_text = next((part.strip() for part in reversed(final_parts) if part.strip()), "")
            findings = _live_solo_findings(root, final_text, events)
            judgment = _live_solo_judgment(returncode, findings, events, error)
            evidence = {
                "surface": "synchronous codex exec --json",
                "registered_route_or_attempt": False,
                "judgment": judgment,
                "model": model,
                "effort": effort,
                "command": command,
                "returncode": returncode,
                "error": error,
                "event_count": len(events),
                "malformed_json_lines": malformed,
                "final_text": final_text,
                "findings": findings,
                "artifacts": {
                    "totals.json": (root / "totals.json").is_file(),
                    "coverage.md": (root / "coverage.md").is_file(),
                },
            }
            (evidence_dir / "result.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.assertIsNone(error, f"native execution failed; evidence: {evidence_dir}")
            self.assertEqual(returncode, 0, f"codex exec returned {returncode}; evidence: {evidence_dir}")
            self.assertEqual(judgment, "PASS",
                             f"live O1–O3 judgment was {judgment}: {findings}; evidence: {evidence_dir}")


if __name__ == "__main__":
    unittest.main()
