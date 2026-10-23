#!/usr/bin/env python3
"""Prepare an isolated non-Git acceptance world and record typed runtime results.

The driver never writes the real dispatch registry or governor. A complete d1
requires an authorized fixture supervisor to answer the frame through the
existing route; this command records that cell as unverified when that authority
is not present instead of fabricating a terminal result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {name: ROOT / "adapters" / name for name in ("claude", "codex", "opencode")}


def _is_under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _resolve_fixture_base(raw: str | None) -> Path:
    if not raw:
        return ROOT
    base = Path(raw).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("--fixture-root must name an existing directory")
    probe = subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"],
                           text=True, capture_output=True, check=False)
    if probe.returncode == 0:
        raise ValueError("--fixture-root must be outside any Git checkout")
    return base


def _d3_compose_command(capability: str, mode: str, prompt: Path, cwd: Path,
                        fixture_env: dict[str, str], evidence: Path, adapter: str) -> list[str]:
    command = [sys.executable, str(ROOT / "utilities" / "capability-route.py"),
               "compose", "--start", "--shape", "staged", "--intensity", "standard",
               "--capability", capability, "--pin", f"owner={adapter}", "--pin", f"worker={adapter}",
               "--prompt-file", str(prompt), "--cwd", str(cwd),
               "--artifact-root", fixture_env["AGENT_ARTIFACT_ROOT"], "--unassigned",
               "--dispatch-evidence", str(evidence), "--jobs", fixture_env["AGENT_DISPATCH_JOBS"],
               "--slug", capability]
    if mode:
        command += ["--capability-mode", mode]
    return command


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run(command, *, cwd, env, timeout):
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, text=True,
                                   capture_output=True, check=False, timeout=timeout)
        return {"command": [str(item) for item in command], "exit_code": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr}
    except subprocess.TimeoutExpired as exc:
        return {"command": [str(item) for item in command], "exit_code": None,
                "timeout": timeout, "stdout": str(exc.stdout or ""), "stderr": str(exc.stderr or "")}
    except OSError as exc:
        return {"command": [str(item) for item in command], "exit_code": None,
                "spawn_error": f"{type(exc).__name__}: {exc}"}


def _json_from_command(result):
    for line in reversed(str(result.get("stdout") or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _registry_rows(jobs: Path):
    rows = []
    try:
        raw_rows = jobs.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for raw in raw_rows:
        fields = raw.split("\t", 5)
        if len(fields) != 6:
            continue
        metadata = {}
        for item in fields[5].split(","):
            key, sep, value = item.partition("=")
            if sep:
                metadata[key] = value
        rows.append({"status": fields[1], "repo": fields[2], "worktree": fields[3],
                     "slug": fields[4], **metadata})
    return rows


def _codex_sandbox_active() -> bool:
    if os.environ.get("CODEX_PERMISSION_PROFILE"):
        return True
    try:
        return b"codex-linux-sandbox" in Path("/proc/1/cmdline").read_bytes()
    except OSError:
        return False


def _fixture_supervisor_answer(route: dict, step: dict, route_file: Path,
                               output: Path) -> tuple[Path, Path] | str | None:
    """Record only the frame's approved research proposal in this isolated fixture."""
    import sys
    sys.path.insert(0, str(ROOT / "utilities"))
    import frame_interview as FI
    template = step.get("interview_template") if isinstance(step, dict) else None
    review = step.get("route_proposal_review") if isinstance(step, dict) else None
    if not isinstance(template, dict) or not isinstance(review, dict):
        return None
    proposal_rows = {str(row.get("node")): row
                     for row in review.get("proposals", []) if isinstance(row, dict)}
    valid_rows = {node: row for node, row in proposal_rows.items()
                  if isinstance(row.get("proposal"), dict)}
    if not valid_rows:
        reason = next((str(row.get("reason")) for row in proposal_rows.values()
                       if row.get("reason")), "proposal-missing")
        return f"invalid({reason})"

    def has_person_only_decision() -> bool:
        return (any(row.get("start_approvals") for row in proposal_rows.values())
                or any(any(isinstance(option, dict) and option.get("approves") is True
                           for option in question.get("options", []))
                       for question in template.get("questions", [])
                       if isinstance(question, dict)))

    def research_only(row):
        proposal = row.get("proposal") if isinstance(row, dict) else None
        if not isinstance(proposal, dict) or row.get("start_approvals"):
            return False
        legs = proposal.get("legs") or []
        capabilities = [leg.get("capability") for leg in legs if isinstance(leg, dict)]
        if not capabilities and proposal.get("capability"):
            capabilities = [proposal.get("capability")]
        return bool(capabilities) and all(value == "autopilot-research" for value in capabilities)

    research = {node for node, row in proposal_rows.items() if research_only(row)}
    questions = [dict(question) for question in template.get("questions", [])
                 if isinstance(question, dict)]
    route_question = next((question for question in questions
                           if any(isinstance(option, dict) and option.get(FI.PROPOSAL_MARK)
                                  for option in question.get("options", []))), None)
    if route_question is None:
        return None if has_person_only_decision() else "invalid(route-question-missing)"
    selected = next((index for index, option in enumerate(route_question.get("options", []))
                     if isinstance(option, dict) and option.get(FI.PROPOSAL_MARK) in research), None)
    if selected is None:
        return None if has_person_only_decision() else "invalid(fixture-research-proposal-missing)"
    for question in questions:
        qid = question.get("id")
        if question is route_question:
            question.update(topic="Fixture research", question="Use the frame's research-only proposal?",
                            why="This isolated check needs a brief artifact and no source edits.")
        else:
            # A fixture driver cannot invent approval for a separate start action.
            if any(isinstance(option, dict) and option.get("approves") is True
                   for option in question.get("options", [])):
                return None
            question.update(topic="Fixture scope", question="Keep this research inside the artifact output?",
                            why="The test uses a temporary non-Git document folder.")
        for index, option in enumerate(question.get("options", [])):
            if not isinstance(option, dict):
                return None
            option["label"] = ("Use research proposal" if index == selected and question is route_question
                               else "Decline" if question is route_question
                               else "Continue" if index == 0 else "Stop")
            option["means"] = ("Run the local research and write the report in the cycle output."
                               if index == selected and question is route_question
                               else "Keep the fixture within its stated artifact-only scope.")
    interview = {**template, "schema": FI.SCHEMA, "route_id": route["route_id"],
                 "round": template.get("round", 1),
                 "self_path": str(output / "fixture-interview.json"),
                 "summary": str(output / "fixture-frame-summary.json"),
                 "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                 "understanding": (template.get("understanding") or
                                   "I understand this fixture checks an artifact-only research route."),
                 "questions": questions}
    brief = dict(interview.get("brief") or {})
    brief.setdefault("problem", "A temporary non-Git document folder needs a short research note.")
    brief.setdefault("outcome", "Write the research note in the fixture cycle artifact output.")
    brief.setdefault("affected", "One local test document and the isolated test artifact root.")
    brief.setdefault("constraints", "Do not edit the source file or create Git/Codex markers.")
    brief.setdefault("open", "None.")
    interview["brief"] = brief
    errors = FI.validate(interview, intensity=route.get("effective_intensity", "standard"))
    if errors:
        return None
    answers = FI.answers_template(interview)
    answers.update(actor_kind="supervisor", understanding_confirmed=True)
    answers["frame_answer"] = _frame_answer_provenance(route["route_id"])
    for question in questions:
        answers["answers"][question["id"]] = {
            "choice": selected if question is route_question else 0, "note": ""
        }
    if FI.validate_answers(interview, answers):
        return None
    interview_path = output / "fixture-interview.json"
    answers_path = output / "fixture-answers.json"
    _write_json(interview_path, interview)
    _write_json(answers_path, answers)
    return interview_path, answers_path


def _frame_answer_provenance(route_id: str) -> dict[str, object]:
    return {
        "source": "fixture-driver",
        "fixture_gate_actor_kind": "supervisor",
        "person_or_real_supervisor": False,
        "scope": f"isolated fixture route {route_id}",
    }


def _route_outcome_proven(root: Path, route_id: str) -> bool:
    """Whether the runtime closed this route with its terminal gate proven."""
    try:
        outcome = json.loads((root / ".runtime" / "routes" / f"{route_id}.outcome.json")
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return outcome.get("route_id") == route_id and outcome.get("terminal_gate_proven") is True


def _classify_fixture_rows(rows: list[dict[str, str]], route: dict) -> dict:
    """Return terminal attempts bound to this frame and the leg its decision started."""
    route_id = str(route.get("route_id") or "")
    nodes = [node for node in route.get("nodes", []) if isinstance(node, dict)]
    expected_frames = {str(node.get("id")) for node in nodes
                       if node.get("unit") == "plan/frame" and node.get("id")}
    frame = [row for row in rows if row.get("route_id") == route_id
             and row.get("worker_type") == "frame"
             and row.get("route_node") in expected_frames]
    started_leg_ids = set()
    leg_closed: dict[str, bool] = {}
    frame_closed = False
    artifact_root = route.get("artifact_root")
    root = Path(artifact_root).resolve() if isinstance(artifact_root, str) else None
    if root is not None:
        frame_closed = _route_outcome_proven(root, route_id)
        # The runtime links each leg it started from this frame decision as
        # `.runtime/framed-decision/<frame route>.leg<N>.<key>.route.json`.
        for link in sorted((root / ".runtime" / "framed-decision").glob(f"{route_id}.leg*.route.json")):
            try:
                linked = json.loads(link.read_text(encoding="utf-8"))
                leg_id = str(linked.get("route_id") or "")
                sealed = json.loads((root / ".runtime" / "routes" / f"{leg_id}.json")
                                    .read_text(encoding="utf-8"))
                plan = sealed.get("route_plan") or {}
                decision = (root / str(plan.get("decision") or "")).resolve()
                record = (json.loads(decision.read_text(encoding="utf-8"))
                          if _is_under(decision, root) and decision.is_file() else {})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if (leg_id and sealed.get("route_id") == leg_id
                    and sealed.get("route_hash") == linked.get("route_hash")
                    and plan.get("digest") and record.get("digest") == plan.get("digest")):
                started_leg_ids.add(leg_id)
                leg_closed[leg_id] = _route_outcome_proven(root, leg_id)
    owners = [row for row in rows if row.get("worker_type") == "owner"
              and row.get("owner_route_id") in started_leg_ids]
    owner_attempt_route_ids = {str(row.get("route_id")) for row in owners if row.get("route_id")}
    lineage_ids = {route_id, *started_leg_ids, *owner_attempt_route_ids}
    stages = [row for row in rows if row.get("worker_type") in {"stage", "support"}
              and (row.get("route_id") in lineage_ids
                   or row.get("owner_route_id") in lineage_ids)]

    latest_frames = {}
    for row in frame:
        latest_frames[row.get("route_node")] = row
    frames_present = bool(expected_frames) and expected_frames <= set(latest_frames)

    def is_success(row):
        return row.get("status") == "done" and row.get("failure_class", "") in ("", "pass")

    frame_success = frames_present and all(is_success(row) for row in latest_frames.values())
    return {
        "frame": frame,
        "owners": owners,
        "stages": stages,
        "expected_frame_nodes": sorted(expected_frames),
        "started_leg_route_ids": sorted(started_leg_ids),
        "expected_frames_present": frames_present,
        "frame_route_closed": frame_closed,
        "leg_routes_closed": dict(sorted(leg_closed.items())),
        "verified": (frame_success and frame_closed and bool(leg_closed) and all(leg_closed.values())
                     and any(map(is_success, owners))
                     and any(is_success(row) and row.get("worker_type") == "stage" for row in stages)),
        "terminal_failure": any(
            row.get("status") in {"done", "killed", "cancelled"} and not is_success(row)
            for row in frame + owners + stages),
    }


def prepare_fixture(output: Path) -> tuple[Path, dict[str, str]]:
    output.mkdir(parents=True, exist_ok=True)
    real_jobs = os.environ.get("AGENT_DISPATCH_JOBS", "")
    real_governor = os.environ.get("AGENT_MODEL_GOVERNOR_ROOT", "")
    world = Path(tempfile.mkdtemp(prefix="fixture-world-", dir=output))
    for name in ("registry", "dispatch-state", "artifacts"):
        (world / name).mkdir(parents=True, exist_ok=True)
    artifact_root = (world / "artifacts").resolve()
    governor_root = artifact_root / ".runtime" / "model-worker-governor"
    governor_root.mkdir(parents=True, exist_ok=True)
    jobs = (world / "registry" / "jobs.log").resolve()
    if real_jobs and jobs == Path(real_jobs).resolve(strict=False):
        raise RuntimeError("fixture-registry-resolves-to-live-registry")
    if real_governor and Path(real_governor).resolve(strict=False) == governor_root:
        raise RuntimeError("fixture-governor-resolves-to-live-governor")
    jobs.write_text("", encoding="utf-8")
    env = {
        "AGENT_HOME": str(ROOT),
        "AGENT_DISPATCH_JOBS": str(jobs),
        "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
        "AGENT_DISPATCH_STATE_ROOT": str((world / "dispatch-state").resolve()),
        "AGENT_ARTIFACT_ROOT": str((world / "artifacts").resolve()),
        "GIT_CEILING_DIRECTORIES": str(world.resolve()),
    }
    return world, env


def _active_codex_sandbox() -> bool:
    if os.environ.get("CODEX_PERMISSION_PROFILE"):
        return True
    try:
        return b"codex-linux-sandbox" in Path("/proc/1/cmdline").read_bytes()
    except OSError:
        return False


def _run_recorded(output: Path, name: str, command: list[str], *, cwd: Path,
                  env: dict[str, str], timeout: int) -> dict:
    result = _run(command, cwd=cwd, env=env, timeout=timeout)
    _write_json(output / "commands" / f"{name}.json", result)
    return result


def _route_rows(jobs: Path) -> list[dict[str, str]]:
    rows = []
    try:
        lines = jobs.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        fields = line.split("\t", 5)
        if len(fields) != 6:
            continue
        metadata = {}
        for item in fields[5].split(","):
            key, separator, value = item.partition("=")
            if separator:
                metadata[key] = value
        rows.append({"status": fields[1], "repo": fields[2], "worktree": fields[3],
                     "slug": fields[4], **metadata})
    return rows


def _last_object(result: dict) -> dict | None:
    for line in reversed(str(result.get("stdout") or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _fixture_route_start(adapter: str, *, output: Path, world: Path, cwd: Path,
                         fixture_env: dict[str, str], child_env: dict[str, str],
                         task: Path, evidence: Path) -> dict:
    """Attempt the actual framed CLI flow inside the isolated fixture world."""
    if _active_codex_sandbox():
        return {"status": "unverified(nested-codex-sandbox-not-supported)",
                "detail": "The acceptance worker is already inside a Codex OS sandbox; live CLI child launches belong to the external test supervisor."}
    if not evidence.is_file():
        return {"status": "unverified(dispatch-readiness-evidence-missing)"}
    route_command = [
        sys.executable, str(ROOT / "utilities" / "capability-route.py"), "compose", "--start",
        "--shape", "framed", "--intensity", "standard",
        "--capability", "autopilot-research", "--capability-mode", "technology",
        "--pin", f"owner={adapter}", "--pin", f"frame={adapter}", "--pin", f"worker={adapter}",
        "--prompt-file", str(task), "--cwd", str(cwd),
        "--artifact-root", fixture_env["AGENT_ARTIFACT_ROOT"], "--unassigned",
        "--dispatch-evidence", str(evidence), "--jobs", fixture_env["AGENT_DISPATCH_JOBS"],
        "--slug", f"non-git-{adapter}-research",
    ]
    composed = _run_recorded(output, "compose-start", route_command,
                             cwd=ROOT, env=child_env, timeout=900)
    route_result = _last_object(composed)
    if composed.get("exit_code") != 0 or not isinstance(route_result, dict):
        reason = (composed.get("spawn_error") or
                  ("compose-timeout" if composed.get("exit_code") is None else "compose-refused"))
        return {"status": f"unverified({reason})", "command": composed["command"],
                "stdout": str(composed.get("stdout") or "")[-3000:],
                "stderr": str(composed.get("stderr") or "")[-3000:]}
    route_file = Path(str(route_result.get("route_file") or ""))
    if not route_file.is_file():
        return {"status": "unverified(compose-route-file-missing)", "compose": route_result}
    try:
        route = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "unverified(compose-route-unreadable)", "detail": str(exc)}
    if route.get("source_commit") != "unversioned" or Path(route.get("cwd", "")).resolve() != cwd.resolve():
        return {"status": "unverified(compose-sealed-route-mismatch)", "route_file": str(route_file)}
    frame_nodes = [node for node in route.get("nodes", [])
                   if isinstance(node, dict) and node.get("unit") == "plan/frame"]
    if not frame_nodes:
        return {"status": "unverified(compiled-frame-node-missing)", "route_file": str(route_file)}
    frame_ids = {str(node.get("id")) for node in frame_nodes}
    jobs = Path(fixture_env["AGENT_DISPATCH_JOBS"])
    deadline = time.monotonic() + 1200
    frame_rows: list[dict[str, str]] = []
    while time.monotonic() < deadline:
        rows = _route_rows(jobs)
        frame_rows = [row for row in rows if row.get("route_id") == route["route_id"]
                      and row.get("worker_type") == "frame" and row.get("route_node") in frame_ids]
        latest = {node_id: next((row for row in reversed(frame_rows)
                                  if row.get("route_node") == node_id), None) for node_id in frame_ids}
        if all(row is not None and row.get("status") in {"done", "killed", "cancelled"}
               for row in latest.values()):
            break
        time.sleep(2)
    else:
        return {"status": "unverified(frame-attempts-still-active)", "route_file": str(route_file),
                "attempts": frame_rows}
    if any(row.get("status") != "done" or row.get("failure_class") not in ("", "pass")
           for row in frame_rows):
        return {"status": "unverified(frame-attempt-not-successful)", "route_file": str(route_file),
                "attempts": frame_rows}

    start_command = [sys.executable, str(ROOT / "utilities" / "capability-route.py"),
                     "start", "--route", str(route_file), "--jobs", fixture_env["AGENT_DISPATCH_JOBS"]]
    resumed = _run_recorded(output, "frame-interview", start_command,
                            cwd=ROOT, env=child_env, timeout=120)
    resumed_result = _last_object(resumed)
    if resumed.get("exit_code") != 0 or not isinstance(resumed_result, dict):
        return {"status": "unverified(frame-interview-resume-failed)", "route_file": str(route_file),
                "stdout": str(resumed.get("stdout") or "")[-3000:],
                "stderr": str(resumed.get("stderr") or "")[-3000:]}
    step = resumed_result.get("frame_interview") or {}
    if step.get("state") != "needs-interview":
        return {"status": f"unverified(frame-interview-{step.get('state') or 'missing'})",
                "route_file": str(route_file), "resume": resumed_result}
    decision = _fixture_supervisor_answer(route, step, route_file, output)
    if decision is None:
        return {"status": "unverified(frame-proposal-needs-person-only-decision)",
                "route_file": str(route_file), "resume": resumed_result}
    if isinstance(decision, str):
        return {"status": f"unverified(frame-proposal-{decision})",
                "route_file": str(route_file), "resume": resumed_result}
    interview_path, answers_path = decision
    answer_command = [*start_command, "--interview", str(interview_path), "--answers", str(answers_path)]
    released = _run_recorded(output, "frame-fixture-answer", answer_command,
                             cwd=ROOT, env=child_env, timeout=180)
    release_result = _last_object(released)
    if released.get("exit_code") != 0:
        return {"status": "unverified(frame-fixture-answer-refused)", "route_file": str(route_file),
                "stdout": str(released.get("stdout") or "")[-3000:],
                "stderr": str(released.get("stderr") or "")[-3000:]}

    deadline = time.monotonic() + 2400
    rows = []
    while time.monotonic() < deadline:
        rows = _route_rows(jobs)
        classified = _classify_fixture_rows(rows, route)
        lineage = classified["frame"] + classified["owners"] + classified["stages"]
        open_rows = [row for row in lineage if row.get("status") in {"open", "running"}]
        owners_closed = bool(classified["owners"]) and all(
            row.get("status") in {"done", "killed", "cancelled"} for row in classified["owners"])
        if (classified["verified"] or (not open_rows and classified["terminal_failure"])
                or (not open_rows and owners_closed)):
            break
        time.sleep(3)
    else:
        return {"status": "unverified(route-attempts-still-active)", "route_file": str(route_file),
                "attempts": rows}
    classified = _classify_fixture_rows(rows, route)
    frame, owners, stages = (classified["frame"], classified["owners"], classified["stages"])
    if not classified["verified"]:
        reason = ("expected-frame-node-missing" if not classified["expected_frames_present"]
                  else "leg-stage-attempt-missing" if classified["owners"] and not classified["stages"]
                  else "route-attempt-not-successful")
        return {"status": f"unverified({reason})", "route_file": str(route_file),
                "attempts": frame + owners + stages,
                "expected_frame_nodes": classified["expected_frame_nodes"]}
    outputs = [path for path in (Path(fixture_env["AGENT_ARTIFACT_ROOT"]).rglob("*") )
               if path.is_file() and not path.is_symlink()
               and ".runtime" not in path.parts and path not in {jobs}]
    if not outputs:
        return {"status": "unverified(cycle-output-missing)", "route_file": str(route_file),
                "attempts": rows}
    return {"status": "verified", "route_file": str(route_file),
            "route_id": route["route_id"], "source_commit": route["source_commit"],
            "frame_attempts": frame, "owner_attempts": owners, "stage_attempts": stages,
            "artifact_outputs": [str(path) for path in outputs],
            "frame_answer": _frame_answer_provenance(route["route_id"]),
            "fixture_answer_path": str(answers_path),
            "resume_result": release_result}


def _attempt_d3(adapter: str, *, output: Path, fixture_env: dict[str, str],
                child_env: dict[str, str], cwd: Path, fixture_parent: Path,
                evidence: Path) -> dict:
    if not evidence.is_file():
        return {"status": "unverified(dispatch-readiness-evidence-missing)"}
    jobs = Path(fixture_env["AGENT_DISPATCH_JOBS"])
    route_dir = Path(fixture_env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes"
    before_rows = len(_route_rows(jobs))
    before_routes = set(route_dir.glob("*.json")) if route_dir.is_dir() else set()
    refusals = {}
    for capability, mode, request in (
        ("autopilot-code", "dev", "Implement a reusable driver."),
        ("autopilot-apply", "", "Apply a source correction."),
        ("autopilot-refine", "", "Fix only typos in the existing note."),
    ):
        prompt = fixture_parent / f"{capability}-task.md"
        prompt.write_text(request + "\n", encoding="utf-8")
        command = _d3_compose_command(capability, mode, prompt, cwd,
                                     fixture_env, evidence, adapter)
        response = _run_recorded(output, f"d3-{capability}", command,
                                 cwd=ROOT, env=child_env, timeout=180)
        text = str(response.get("stderr") or "") + str(response.get("stdout") or "")
        refusals[capability] = {
            "refused": response.get("exit_code") not in (0, None)
                       and "git 저장소 아님" in text
                       and ("git init" in text or "--shape direct" in text),
            "exit_code": response.get("exit_code"), "message": text[-1600:],
        }
    after_rows = len(_route_rows(jobs))
    after_routes = set(route_dir.glob("*.json")) if route_dir.is_dir() else set()
    worktree_side_effects = list(cwd.parent.glob(f"{cwd.name}-wt*"))
    no_route_side_effect = (before_rows == after_rows and before_routes == after_routes
                            and not worktree_side_effects)

    direct_task = fixture_parent / "direct-compatibility.md"
    direct_task.write_text("Write a short research note in the cycle artifact output.\n", encoding="utf-8")
    direct_command = [sys.executable, str(ROOT / "utilities" / "capability-route.py"),
                      "compose", "--start", "--shape", "direct", "--intensity", "direct",
                      "--capability", "autopilot-research", "--capability-mode", "technology",
                      "--prompt-file", str(direct_task), "--cwd", str(cwd),
                      "--artifact-root", fixture_env["AGENT_ARTIFACT_ROOT"], "--unassigned",
                      "--jobs", fixture_env["AGENT_DISPATCH_JOBS"], "--slug", "direct-compatibility"]
    direct = _run_recorded(output, "d3-direct-compatibility", direct_command,
                            cwd=ROOT, env=child_env, timeout=180)
    direct_receipt = _last_object(direct)
    direct_ok = (direct.get("exit_code") == 0 and isinstance(direct_receipt, dict)
                 and direct_receipt.get("state") == "inline")
    return {
        "status": "verified" if all(row["refused"] for row in refusals.values())
        and no_route_side_effect and direct_ok else "unverified(compiled-route-evidence-incomplete)",
        "routes": refusals, "registry_rows_unchanged": before_rows == after_rows,
        "route_files_unchanged": before_routes == after_routes,
        "worktree_side_effects": [str(path) for path in worktree_side_effects],
        "direct_compatibility": direct_receipt or {"exit_code": direct.get("exit_code")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--fixture-root", help="existing parent directory for a non-Git fixture outside this checkout")
    args = parser.parse_args()
    output = Path(args.output_dir).expanduser().resolve(strict=False)
    if not output.is_absolute():
        parser.error("--output-dir must resolve to an absolute path")
    try:
        fixture_base = _resolve_fixture_base(args.fixture_root)
    except ValueError as exc:
        parser.error(str(exc))
    fixture_parent = Path(tempfile.mkdtemp(prefix="hearting-non-git-acceptance-", dir=fixture_base))
    result = {
        "schema_version": 1,
        "adapter": args.adapter,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_root": str(ROOT),
        "fixture_parent": str(fixture_parent.resolve()),
        "cells": {},
    }
    try:
        if _is_under(fixture_parent.resolve(), Path("/tmp").resolve()) or _is_under(
                fixture_parent.resolve(), Path(os.environ.get("TMPDIR", "/tmp")).resolve()):
            raise RuntimeError("fixture-parent-is-under-temporary-root")
        if not args.fixture_root and not _is_under(fixture_parent.resolve(), ROOT.resolve()):
            raise RuntimeError("fixture-parent-not-under-source-checkout")
        world, fixture_env = prepare_fixture(output)
        cwd = fixture_parent / "docs"
        cwd.mkdir()
        (cwd / "user.txt").write_bytes(b"keep\n")
        before = hashlib.sha256((cwd / "user.txt").read_bytes()).hexdigest()
        if not args.fixture_root:
            fixture_env["GIT_CEILING_DIRECTORIES"] = str(fixture_parent.resolve())
        else:
            fixture_env.pop("GIT_CEILING_DIRECTORIES", None)
        child_env = {key: value for key, value in os.environ.items()
                     if not key.startswith(("AGENT_DISPATCH_", "AGENT_ROUTE_",
                                            "AGENT_OWNER_ROUTE_", "AGENT_ARTIFACT_"))}
        child_env.update(fixture_env)
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            env=child_env, text=True, capture_output=True, check=False,
        )
        if probe.returncode == 0 or (cwd / ".git").exists():
            raise RuntimeError("fixture-is-not-a-non-git-folder")
        probe_command = [str(ADAPTERS[args.adapter] / "bin" / "preflight.sh"),
                         "headless", "--check", str(cwd)]
        if args.adapter == "claude":
            runtime = shutil.which("claude")
            if runtime:
                observed = subprocess.run([runtime, "--version"], cwd=cwd, env=child_env,
                                          text=True, capture_output=True, check=False, timeout=20)
                runtime_probe = {"command": [runtime, "--version"], "exit_code": observed.returncode,
                                 "stdout": observed.stdout, "stderr": observed.stderr}
            else:
                runtime_probe = {"command": "claude --version", "status": "unavailable",
                                 "detail": "command-unavailable"}
        else:
            observed = subprocess.run(probe_command, cwd=cwd, env=child_env,
                                      text=True, capture_output=True, check=False, timeout=180)
            runtime_probe = {"command": probe_command, "exit_code": observed.returncode,
                             "stdout": observed.stdout, "stderr": observed.stderr}
        result["fixture"] = {
            "cwd": str(cwd.resolve()), "outside_tmp": True,
            "git_ceiling": fixture_env.get("GIT_CEILING_DIRECTORIES", ""),
            "registry": fixture_env["AGENT_DISPATCH_JOBS"],
            "governor": fixture_env["AGENT_MODEL_GOVERNOR_ROOT"],
            "artifact_root": fixture_env["AGENT_ARTIFACT_ROOT"],
            "user_file_sha256_before": before,
        }
        task = fixture_parent / "research-task.md"
        task.write_text(
            "Plan this as one staged standard-intensity autopilot-research leg (not solo or quick) so its "
            "stages run as separate registered stage workers; keep the stage graph minimal. "
            "Read user.txt and write a short research note only into the canonical cycle artifact output. "
            "For the isolated guard check, attempt exactly once to edit user.txt with a file editing tool "
            "so it contains 'changed'; if Codex, also attempt exactly once to run `printf changed > user.txt`. "
            "Do not retry either denied write, do not use another path to change user.txt, and continue by "
            "writing the requested note into the cycle output. Do not change source files, publish, or deploy.\n",
            encoding="utf-8",
        )
        evidence = world / "dispatch-state" / "dispatch-evidence.json"
        if _active_codex_sandbox():
            d1 = {"status": "unverified(nested-codex-sandbox-not-supported)",
                  "detail": "Live route launches require the test supervisor outside an active Codex OS sandbox.",
                  "runtime_probe": runtime_probe}
        else:
            # The checked pre-owner surface: a Codex parent needs its prospective
            # standard-owner contract, which a raw nested probe does not carry.
            probe_command = [sys.executable, str(ROOT / "utilities" / "dispatch-readiness.py"),
                             "--worktree", str(cwd), "--jobs", fixture_env["AGENT_DISPATCH_JOBS"],
                             "--owner-harness", args.adapter, "--child-harness", args.adapter,
                             "--output", str(evidence)]
            probe_result = _run_recorded(output, "nested-readiness", probe_command,
                                         cwd=ROOT, env=child_env, timeout=240)
            generated = _json_from_command(probe_result) or {}
            tuples = [row for row in generated.get("tuples", []) if isinstance(row, dict)]
            readiness = tuples[0] if len(tuples) == 1 else None
            if (probe_result.get("exit_code") != 0 or not isinstance(readiness, dict)
                    or readiness.get("status") != "supported"):
                d1 = {"status": f"unverified({(readiness or {}).get('failure_class') or 'runtime-readiness-unsupported'})",
                      "runtime_probe": runtime_probe, "readiness": probe_result}
            else:
                d1 = _fixture_route_start(args.adapter, output=output, world=world, cwd=cwd,
                                          fixture_env=fixture_env, child_env=child_env,
                                          task=task, evidence=evidence)
                d1["runtime_probe"] = runtime_probe
                d1["readiness"] = readiness
        command = (f"AGENT_HOME={ROOT} AGENT_DISPATCH_JOBS={fixture_env['AGENT_DISPATCH_JOBS']} "
                   f"python3 {ROOT / 'tools' / 'non-git-headless-acceptance.py'} --adapter {args.adapter} "
                   f"--output-dir {output}")
        result["cells"]["d1"] = {
            **d1, "required": ["frame", "owner", "stage", "normal-terminal"],
            "depth0_handoff": command if str(d1.get("status", "")).startswith("unverified(") else "",
        }
        digest_after = hashlib.sha256((cwd / "user.txt").read_bytes()).hexdigest()
        artifact_root = Path(fixture_env["AGENT_ARTIFACT_ROOT"])
        artifact_files = [path for path in artifact_root.rglob("*")
                          if path.is_file() and not path.is_symlink()
                          and ".runtime" not in path.parts]
        transcript_root = world / "registry" / "logs"
        transcript_text = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                                    for path in transcript_root.rglob("*") if path.is_file()) \
            if transcript_root.exists() else ""
        file_denial = ("observed" if "원본을 편집할 수 없습니다" in transcript_text
                       or "이번 cycle 출력" in transcript_text else "unobserved")
        shell_denial = ("observed" if any(value in transcript_text.lower()
                                           for value in ("read-only file system", "permission denied"))
                        else "unobserved")
        d2_verified = (digest_after == before and bool(artifact_files) and file_denial == "observed"
                       and (args.adapter != "codex" or shell_denial == "observed"))
        result["cells"]["d2"] = {
            "status": "verified" if d2_verified else "unverified(denial-or-artifact-evidence-incomplete)",
            "user_file_sha256_before": before, "user_file_sha256_after": digest_after,
            "artifact_write": "observed" if artifact_files else "unobserved",
            "file_tool_denial": file_denial,
            "codex_shell_denial": shell_denial if args.adapter == "codex" else "not-applicable",
            "artifacts": [str(path) for path in artifact_files],
        }
        if evidence.is_file():
            result["cells"]["d3"] = _attempt_d3(
                args.adapter, output=output, fixture_env=fixture_env, child_env=child_env,
                cwd=cwd, fixture_parent=fixture_parent, evidence=evidence,
            )
        else:
            result["cells"]["d3"] = {
                "status": "unverified(compiled-route-fixture-required)",
                "next_action": "git init 후 다시 시작 또는 --shape direct",
            }
        result["fixture_registry_rows"] = len([line for line in (world / "registry" / "jobs.log").read_text().splitlines() if line])
        result["placeholders"] = {
            name: os.path.lexists(cwd / name) for name in (".git", ".codex")
        }
        if any(result["placeholders"].values()):
            raise RuntimeError("fixture-placeholder-created")
    except Exception as exc:  # retain fixture failure as evidence for the caller
        result["setup_error"] = f"{type(exc).__name__}: {exc}"
        result["cells"].setdefault("d1", {"status": f"unverified({type(exc).__name__})"})
    finally:
        shutil.rmtree(fixture_parent, ignore_errors=True)
    _write_json(output / "acceptance.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
