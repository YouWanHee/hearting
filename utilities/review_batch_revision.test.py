#!/usr/bin/env python3
"""Revision generation selection through the public start path; no model calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer as PRODUCER
import dispatch_contract as DC
import dispatch_replacement as REPLACEMENT
import dispatch_replacement_batch as REPLACEMENT_BATCH
import dispatch_owner_input as OWNER_INPUT

spec = importlib.util.spec_from_file_location("revision_route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ROUTE)
spec = importlib.util.spec_from_file_location("revision_node", ROOT / "utilities/dispatch-node.py")
NODE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NODE)


class RevisionBatchCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.jobs = self.base / "dispatch/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.touch()
        self.art = self.base / ".agent_reports"
        self.art.mkdir()
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        self.env = {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.base / "home"),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_CACHE_HOME": str(self.base / "cache"),
            "CODEX_HOME": str(self.base / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.art),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.art / ".runtime/model-worker-governor"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "DISPATCH_DEFAULTS_CONFIG": str(ROOT / "profiles/dispatch-defaults.yaml"),
            "HEARTING_GATES": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for command in (["init", "-q"], ["config", "user.email", "fixture@example.com"],
                        ["config", "user.name", "Fixture"]):
            self.run_cmd(["git", "-C", str(self.repo), *command])
        (self.repo / "x").write_text("initial\n")
        self.run_cmd(["git", "-C", str(self.repo), "add", "x"])
        self.run_cmd(["git", "-C", str(self.repo), "commit", "-qm", "init"])
        # Only the outer CLI transport is fake. All wrappers, admissions,
        # reservation/claim fences, immutable inputs and completion are real.
        runtime = fake_bin / "claude"
        runtime.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
if '--help' in sys.argv:
    print('--permission-mode --setting-sources --settings --tools --strict-mcp-config')
    raise SystemExit(0)
route=json.loads(pathlib.Path(os.environ['AGENT_ROUTE_FILE']).read_text())
node=next(n for n in route['nodes'] if n['id']==os.environ['AGENT_ROUTE_NODE'])
trace=pathlib.Path(os.environ['FIXTURE_TRANSPORT_TRACE'])
with trace.open('a') as handle:
    handle.write(json.dumps({'attempt':os.environ['AGENT_DISPATCH_ATTEMPT_ID'],
        'jobs':os.environ['AGENT_DISPATCH_JOBS'], 'node':node})+'\\n')
if node['id']==os.environ.get('FIXTURE_DEAD_NODE'):
    once=pathlib.Path(os.environ['FIXTURE_DEATH_ONCE'])
    if not once.exists():
        once.write_text('first physical attempt only')
        print('{}')
        raise SystemExit(0)
out=pathlib.Path(os.environ['AGENT_ARTIFACT_OUTPUT_DIR']) / node['outputs'][0]
out.parent.mkdir(parents=True,exist_ok=True)
verdict='FAIL' if node['id']==os.environ.get('FIXTURE_FAIL_NODE') else 'PASS'
out.write_text(f'Fixture independent review: {verdict}.\\n')
text=f'artifact: {out}\\nverdict: {verdict}\\nblocker: '+('fixture finding' if verdict=='FAIL' else 'none')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text}))
''')
        runtime.chmod(0o755)
        self.trace = self.base / "transports.jsonl"
        self.trace.touch()
        self.env['FIXTURE_TRANSPORT_TRACE'] = str(self.trace)
        tuples = [{"parent_harness": "codex", "parent_transport": "headless",
                   "parent_sandbox": "workspace-write", "child_harness": "claude",
                   "launch_authority": "conductor", "status": "supported",
                   "probe_source": "fixture", "probe_time": "2026-10-12T00:00:00Z",
                   "failure_class": "", "checked_worktree": str(self.repo),
                   "failure_scope": "none", "codex_command": "not-applicable",
                   "retry_on_isolated_worktree": 0}]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.route = ROUTE.compile_route("autopilot-code", "debug", "strong",
                self.repo, self.art, signals=["shared-contract"], transport="headless",
                tracking="tracked", tracked_gate_evidence={
                    "spec_read": {"satisfied": True, "source": "fixture"},
                    "drift_verdict": "within-spec", "workflow_mode": "tracked",
                    "artifact_guard": {"satisfied": True, "source": "fixture"}},
                dispatch_evidence={"tuples": tuples, "native_subagent": []})
        self.path = self.art / ".runtime/routes" / (self.route["route_id"] + ".json")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.route))
        with mock.patch.dict(os.environ, self.env, clear=True):
            issued = PRODUCER.begin(self.art, route_file=self.path,
                capability="autopilot-code", intensity="strong", require_cycle=True)
        self.route = json.loads(self.path.read_text())
        self.env.update(issued["env"])
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIsNotNone(PRODUCER.route_cycle_for(self.art, self.route), issued)
        self.output = Path(issued["cycle_dir"]) / "artifacts"
        self.output.mkdir(parents=True, exist_ok=True)
        self.evidence = self.output / "dev_logs/execute.md"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text("initial implementation\n")
        self.owner = "att-fixture-owner-" + self.base.name
        identity = DC.process_launch_identity(os.getpid())
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "worker_type": "owner",
                "capability": "autopilot-code", "intensity": "strong", "harness": "codex",
                "runtime_sandbox": "workspace-write", "owner_route_id": self.route["route_id"],
                "owner_route_file": str(self.path),
                "owner_route_hash": self.route["route_hash"], "parent_sid": "fixture-parent",
                "attempt_id": self.owner, "pid": str(os.getpid()),
                "pid_start": identity["pid_start"]}
        self.jobs.write_text("now\topen\t" + str(self.repo) + "\t" + str(self.repo)
                             + "\towner\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        self.env.update({"AGENT_DISPATCH_SELF_SLUG": "owner",
            "AGENT_DISPATCH_ATTEMPT_ID": self.owner, "CODEX_THREAD_ID": "fixture-owner-session",
            "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write", "AGENT_DISPATCH_DEPTH": "1",
            "AGENT_DISPATCH_CHILD": "1"})
        # Publish prerequisite inline completions using the normal writer.
        # This retains the compiled recipe and its execute -> review group.
        def seed(node_id):
            node = next(n for n in self.route["nodes"] if n["id"] == node_id)
            for dependency in node.get("depends_on", []):
                seed(dependency)
            if node_id in seeded:
                return
            evidence = self.output / ("fixture-" + node_id + ".md")
            evidence.write_text("fixture predecessor\n")
            with mock.patch.dict(os.environ, self.env, clear=True):
                ROUTE._publish_completion_locked(self.route, node, node_id, evidence,
                    jobs=self.jobs, attempt_id="att-inline-" + node_id + "-1",
                    attempt_metadata={"attempt_schema_version": 2,
                        "dispatch_depth": node["dispatch_depth"], "transport": "interactive",
                        "execution_surface": "inline", "registered_worker": False, "fallback_hop": "inline"})
            seeded.add(node_id)
        seeded = set()
        for dependency in next(n for n in self.route["nodes"] if n["id"] == "execute")["depends_on"]:
            seed(dependency)
        self.inline_execute("att-inline-execute-1")
        self.prompt = self.base / "prompt.md"
        self.prompt.write_text("Review original implementation.")
        self.owner_slug = "owner"

    def run_cmd(self, command, expected=0):
        for retry in range(3):
            stage_start = str(ROOT / 'utilities/stage-dispatch-fallback.py') in command
            before = self.atomic_state() if stage_start else None
            result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=45)
            receipts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            refusal = receipts[-1] if receipts else {}
            transient = (expected == 0 and result.returncode == 78
                and refusal.get('reason') == 'prior-attempt-unverifiable'
                and str(refusal.get('detail', '')).endswith(':attempt-descendant-unverifiable'))
            if not stage_start or not transient or retry == 2:
                break
            self.assertEqual(self.atomic_state(), before)
            print('Public CLI atomic procfs refusal before re-observation: ' +
                  json.dumps(refusal, sort_keys=True), file=sys.stderr)
            time.sleep(0.2)
        self.assertIn(result.returncode, expected if isinstance(expected, tuple) else (expected,),
                      result.stdout + result.stderr)
        return result

    def route_cmd(self, action, *args):
        return self.run_cmd([sys.executable, str(ROOT / "utilities/capability-route.py"), action,
                            "--route", str(self.path), "--jobs", str(self.jobs), *args])

    def inline_execute(self, attempt):
        return self.inline("execute", self.evidence, attempt)

    def inline(self, node, evidence, attempt):
        depth = next(n for n in self.route["nodes"] if n["id"] == node)["dispatch_depth"]
        # Inline completion names axes, so --jobs would request a registered
        # row close. Its common readers still inherit the exact canonical jobs.
        return self.run_cmd([sys.executable, str(ROOT / "utilities/capability-route.py"),
            "complete", "--route", str(self.path), "--inline", "--node", node, "--evidence", str(evidence),
            "--attempt-id", attempt, "--dispatch-depth", str(depth), "--transport", "interactive",
            "--execution-surface", "inline", "--registered-worker", "0", "--fallback-hop", "inline"])

    def launch(self, expected=0, slug="fixture-review"):
        result = self.run_cmd([sys.executable, str(ROOT / "utilities/stage-dispatch-fallback.py"),
            "--route", str(self.path), "--node", "impl-review", "--slug", slug,
            "--parent", self.owner_slug, "--jobs", str(self.jobs), "--prompt-file", str(self.prompt),
            "--start"], expected)
        receipts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        return receipts[-1] if receipts else result.stdout

    def test_two_pass_revision_inline_next_generation_and_cap_replay(self):
        self.revision_scenario()

    def test_revision_generation_and_replay_with_gates_on(self):
        self.env["HEARTING_GATES"] = "on"
        self.revision_scenario()

    def test_foreign_registry_cannot_override_the_sealed_jobs(self):
        foreign = self.base / "foreign/jobs.log"
        foreign.parent.mkdir()
        foreign.touch()
        original = self.jobs.read_bytes()
        result = self.run_cmd([sys.executable, str(ROOT / "utilities/stage-dispatch-fallback.py"),
            "--route", str(self.path), "--node", "impl-review", "--parent", "owner",
            "--jobs", str(foreign), "--prompt-file", str(self.prompt), "--start"], expected=65)
        self.assertIn("noncanonical-nested-jobs", result.stdout + result.stderr)
        self.assertEqual(self.jobs.read_bytes(), original)
        self.assertEqual(foreign.read_bytes(), b"")

    def marker(self, node):
        path = self.jobs.parent / "completion" / self.route["route_id"] / (node + ".json")
        return json.loads(path.read_text())

    def fixture_row(self, attempt, *, status=None, remove=(), **changes):
        """Seed a physical observation, never a native transport claim."""
        lines = self.jobs.read_text().splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            meta = DC.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") != attempt:
                continue
            for key in remove:
                meta.pop(key, None)
            meta.update(changes)
            fields[1] = status or fields[1]
            fields[5] = ",".join(k + "=" + str(v) for k, v in meta.items())
            lines[index] = "\t".join(fields)
            self.jobs.write_text("\n".join(lines) + "\n")
            return meta
        self.fail("missing fixture row " + attempt)

    def replace_owner(self):
        # Synthetic physical owner observation is fixture setup. The correction,
        # immutable inputs, reservation, reciprocal binding and claim are real.
        args = SimpleNamespace(attempt_id=self.owner, jobs_path=self.jobs,
            worktree=str(self.repo), owner_route_binding=SimpleNamespace(route_id=self.route['route_id']),
            replacement_input_argv=['--start', '--attempt-id', self.owner,
                                    '--prompt-text', 'Continue this route'],
            worker_type='owner', capability='autopilot-code', intensity='strong',
            dispatch_depth=1, parent_session_id='fixture-parent')
        with mock.patch.dict(os.environ, self.env, clear=True):
            fragment = REPLACEMENT.seal_launch_input(args, 'codex', 'Continue this route')
            self.fixture_row(self.owner, **DC.parse_registry_metadata(fragment))
            OWNER_INPUT.initialize_owner_input(self.jobs, self.owner, 'codex-app-server')
            self.fixture_row(self.owner, status='done', remove=('pid', 'pid_start'),
                note='dead-worker-blocked', failure_class='blocked', launch_outcome='never-launched')
            correction = OWNER_INPUT.submit(self.jobs, self.owner,
                'Preserve completed reviews; correct execute and run bounded closure.', 'correction-1')
            self.assertTrue(correction['retained'])
            # Summary observers inherit the original owner tag and drain their
            # final refresh after the peer joins. Observe their actual exit;
            # do not waive quiescence or terminate a live observer.
            deadline = time.monotonic() + 90
            while True:
                probe = DC.attempt_tagged_descendants(REPLACEMENT._rows(
                    self.jobs.read_text().splitlines())[self.owner][1])
                if probe.state == 'empty' or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            self.assertEqual(probe.state, 'empty', repr(probe))
            record = self.owner_operation(lambda: REPLACEMENT.claim(self.jobs, self.owner))
            self.assertEqual(record['proof']['death_kind'], REPLACEMENT.CORRECTED)
            source = REPLACEMENT._rows(self.jobs.read_text().splitlines())[self.owner][1]
            replay = REPLACEMENT.launch_input(self.jobs, self.owner, source)
            original_rows = self.jobs.read_bytes()
            original = self.historical_bytes()
            successor = record['replacement_attempt_id']
            args.attempt_id = successor
            args.replacement_input_argv = REPLACEMENT._replacement_argv(record, source, replay)
            candidate = {**source, 'attempt_id': successor, 'automatic_retry_of': self.owner,
                         **DC.process_launch_identity(os.getpid())}
            for key in ('note', 'failure_class', 'launch_outcome', 'replacement_input_digest',
                        'replacement_family_id', 'replacement_claim_digest', 'replacement_attempt_id'):
                candidate.pop(key, None)
            candidate.update(DC.parse_registry_metadata(REPLACEMENT.seal_launch_input(
                args, 'codex', REPLACEMENT.route_authority.recovery_task(record, source, replay))))
            self.assertEqual(self.owner_operation(lambda: REPLACEMENT.admission(
                self.jobs, self.jobs.read_text().splitlines(), candidate)), record)
            row = 'now\topen\t' + str(self.repo) + '\t' + str(self.repo) + '\tcurrent-owner\t' + ",".join(
                k + '=' + str(v) for k, v in candidate.items())
            self.assertTrue(self.owner_operation(lambda: DC.claim_attempt_row(self.jobs, successor, row, launch=False)))
            self.assertFalse(self.owner_operation(lambda: DC.claim_attempt_row(self.jobs, successor, row, launch=False)))
            registered = REPLACEMENT._rows(self.jobs.read_text().splitlines())[successor][1]
            self.assertEqual(registered['launch_claimed'], '0')
            self.assertEqual(self.owner_operation(lambda: REPLACEMENT.admission(
                self.jobs, self.jobs.read_text().splitlines(), registered)), record)
            self.assertEqual(REPLACEMENT.effective_attempts(self.jobs, {self.owner})[0], {successor})
            self.assertEqual(REPLACEMENT.launch_input(self.jobs, self.owner, source), replay)
            self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
            for path, data in original.items():
                self.assertEqual(path.read_bytes(), data, str(path))
        self.owner = successor
        self.owner_slug = 'current-owner'
        self.env.update(AGENT_DISPATCH_ATTEMPT_ID=successor, AGENT_DISPATCH_SELF_SLUG=self.owner_slug)

    def owner_operation(self, operation):
        # Re-observe a procfs refusal with exactly the same authority/input.
        # A refusal remains recorded and must have made no durable mutation.
        for retry in range(3):
            before = self.atomic_state()
            try:
                return operation()
            except DC.DispatchContractError as exc:
                if exc.reason != 'replacement-owner-child-unsettled' or retry == 2:
                    raise
                self.assertEqual(self.atomic_state(), before)
                rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
                for child in REPLACEMENT._owned_children(rows, self.owner):
                    fields, meta = rows[child]
                    self.assertEqual(fields[1], 'done')
                    proof = DC.attempt_process_quiescence(meta, terminal_receipt=True)
                    self.assertNotEqual(proof.state, 'live', repr(proof))
                print('Owner authority re-observation after atomic refusal: ' + str(exc), file=sys.stderr)
                time.sleep(0.2)

    def assert_selection(self, receipt):
        rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
        traces = {item['attempt']: item for item in map(json.loads, self.trace.read_text().splitlines())}
        self.assertEqual(self.route['continuation_budget']['review_round_cap'], 2)
        for leg in receipt['legs']:
            node = next(n for n in self.route['nodes'] if n['id'] == leg['node'])
            meta = rows[leg['attempt_id']][1]
            self.assertEqual(meta['parent_attempt_id'], self.owner)
            self.assertEqual(meta['harness'], leg['adapter'])
            for key in ('model_profile', 'intensity', 'dispatch_depth'):
                self.assertEqual(meta[key], str(node.get(key, self.route['effective_intensity'])), key)
            self.assertEqual(leg['perspective'], node['perspective'])
            self.assertEqual(meta['batch_perspective'], node['perspective'])
            self.assertIn(meta['fallback_hop'], [h['fallback_hop'] for h in node['fallback_hops']])
            self.assertEqual(traces[leg['attempt_id']]['jobs'], str(self.jobs))
            self.assertEqual(traces[leg['attempt_id']]['node'], node)

    def historical_bytes(self):
        # Canonical completion files advance; immutable history/input/links do
        # not. Exclude lock files and live transport observations.
        return {p: p.read_bytes() for directory in ("automatic-replacements", "review-inputs", "completion")
                for p in (self.jobs.parent / directory).rglob("*.json")
                if directory != "completion" or (".att-" in p.name and p.name.endswith(".attempt.json"))
                or (p.stem.rsplit(".", 1)[-1].isdigit())}

    def admission(self, node_id):
        node = next(n for n in self.route["nodes"] if n["id"] == node_id)
        with mock.patch.dict(os.environ, self.env, clear=True):
            return NODE.admit_round(self.route, node, self.jobs, owner_attempt_id=self.owner)

    def revise_execute(self, receipt, sequence):
        self.evidence.write_text("implementation correction " + str(sequence) + "\n")
        self.route_cmd("revise", "--node", "execute", "--evidence", str(self.evidence),
            "--basis", "review-findings", "--answers", ",".join(leg["attempt_id"] for leg in receipt["legs"]),
            "--author-attempt-id", self.owner)
        self.assertEqual(self.marker("execute")["sequence"], sequence)
        for leg in receipt["legs"]:
            if not (self.jobs.parent / 'completion' / self.route['route_id'] / (leg['node'] + '.json')).exists():
                continue  # A real FAIL has a verdict row, not a successful marker.
            tombstone = self.marker(leg["node"])
            self.assertEqual(tombstone["state"], "superseded-by-upstream-revision")
            self.assertEqual(tombstone["superseded_by"], {"node": "execute", "sequence": sequence})
        self.inline_execute("att-inline-execute-" + str(sequence + 1))
        self.assertEqual(self.marker("execute")["sequence"], sequence + 1)

    def revision_scenario(self):
        first = self.launch()
        self.assertEqual(first["newly_started"], 2, first)
        self.assertEqual(len(first["legs"]), 2)
        for leg in first["legs"]:
            self.assertEqual(leg["adapter"], "claude")
        self.settle(first)
        second = self.launch()
        self.assertEqual(second["newly_started"], 0, second)
        original_rows = self.jobs.read_bytes()
        from recovery_history import batch_input_path
        metadata = REPLACEMENT._rows(self.jobs.read_text().splitlines())[first["legs"][0]["attempt_id"]][1]
        input_path = batch_input_path(self.jobs, metadata["batch_manifest_sha256"])
        original_input = input_path.read_bytes()
        input_path.write_bytes(b"{")
        corrupt = self.launch(expected=65)
        self.assertEqual(corrupt["reason"], "replacement-record-unreadable", corrupt)
        self.assertEqual(self.jobs.read_bytes(), original_rows)
        input_path.write_bytes(original_input)
        original = self.historical_bytes()
        self.prompt.write_text("Closure review after corrected implementation.")
        refused = self.launch(expected=65)
        self.assertEqual(refused["reason"], "replacement-batch-source-drift", refused)
        self.assertEqual(self.jobs.read_bytes(), original_rows)
        peer_rows = [line for line in self.jobs.read_text().splitlines()
                     if any('attempt_id=' + leg['attempt_id'] + ',' in line for leg in first['legs'])]
        self.replace_owner()
        for row in peer_rows:
            self.assertIn(row, self.jobs.read_text().splitlines())
        original_rows = self.jobs.read_bytes()
        self.revise_execute(first, 2)
        for leg in first["legs"]:
            admission = self.admission(leg["node"])
            self.assertFalse(admission.gate_unmet)
            self.assertTrue(admission.requires_next_round)
            self.assertEqual((admission.budget.verdict_rounds, admission.budget.next_round), (1, 2))
        round_two = self.launch()
        self.assertEqual((round_two["admitted"], round_two["newly_started"]), (2, 2), round_two)
        self.assertTrue({leg["attempt_id"] for leg in first["legs"]}.isdisjoint(
            {leg["attempt_id"] for leg in round_two["legs"]}))
        before_duplicate = self.atomic_state()
        live = self.launch(expected=78, slug="another-display")
        self.assertEqual(live["reason"], "prior-attempt-still-live")
        self.assertEqual(self.atomic_state(), before_duplicate)
        self.settle(round_two)
        self.assert_selection(round_two)
        settled_rows = self.jobs.read_bytes()
        before_replay = self.atomic_state()
        replay = self.launch(slug="another-display")
        self.assertEqual((replay["admitted"], replay["newly_started"]), (0, 0), replay)
        self.assertEqual([leg["attempt_id"] for leg in replay["legs"]],
                         [leg["attempt_id"] for leg in round_two["legs"]])
        self.assertEqual(self.jobs.read_bytes(), settled_rows)
        self.assertEqual(self.atomic_state(), before_replay)
        for leg in round_two["legs"]:
            admission = self.admission(leg["node"])
            self.assertEqual(admission.budget.state, "exhausted")
            self.assertFalse(admission.requires_next_round)
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        # One additional revision-backed verdict is allowed, then spent.
        self.revise_execute(round_two, 4)
        self.prompt.write_text("Final correction check.")
        round_three = self.launch()
        self.settle(round_three)
        self.assertEqual(self.launch()["newly_started"], 0)
        self.revise_execute(round_three, 6)
        self.prompt.write_text("A fourth verdict cannot be admitted.")
        rows = self.jobs.read_bytes()
        spent = self.assert_atomic_refusal(reason="review-round-budget-exhausted")
        self.assertEqual(self.jobs.read_bytes(), rows)

    def test_death_partial_replacement_then_revision_and_cap_replay(self):
        self.env.update(FIXTURE_DEAD_NODE="impl-review", FIXTURE_DEATH_ONCE=str(self.base / "death-once"))
        first = self.launch()
        gap = next(leg for leg in first["legs"] if leg["node"] == "impl-review")
        sibling = next(leg for leg in first["legs"] if leg["node"] != "impl-review")
        self.settle({"legs": [sibling]})
        self.run_cmd([sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
            "reconcile", "--jobs", str(self.jobs), "--attempt", gap["attempt_id"],
            "--only-exact-dead", "--apply", "--agent-home", str(ROOT)])
        sibling_row = next(line for line in self.jobs.read_text().splitlines()
                           if "attempt_id=" + sibling["attempt_id"] + "," in line)
        with mock.patch.dict(os.environ, self.env, clear=True):
            record = REPLACEMENT.claim(self.jobs, gap["attempt_id"])
            source = REPLACEMENT._rows(self.jobs.read_text().splitlines())[gap["attempt_id"]][1]
            replay = REPLACEMENT.launch_input(self.jobs, gap["attempt_id"], source)
            command = REPLACEMENT_BATCH.command(self.jobs, record, source, replay)
        replaced = self.run_cmd(command)
        receipt = json.loads(next(line for line in reversed(replaced.stdout.splitlines()) if line.startswith("{")))
        self.assertEqual((receipt["admitted"], receipt["newly_started"]), (1, 1), receipt)
        replacement = next(leg for leg in receipt["legs"] if leg["node"] == gap["node"])
        self.assertEqual(replacement["attempt_id"], record["replacement_attempt_id"])
        self.settle({"legs": [replacement]})
        self.assertIn(sibling_row, self.jobs.read_text().splitlines())
        membership = self.admission(gap["node"])
        self.assertEqual(membership.budget.verdict_rounds, 1)
        self.assertEqual(membership.physical_attempt_ids, frozenset((gap["attempt_id"], replacement["attempt_id"])))
        original_rows = self.jobs.read_bytes()
        original = self.historical_bytes()
        current = {"legs": [replacement, sibling]}
        self.revise_execute(current, 2)
        self.prompt.write_text("Closure review after partial death replacement and correction.")
        round_two = self.launch()
        self.assertEqual(round_two["newly_started"], 2, round_two)
        self.settle(round_two)
        replay = self.launch(slug="changed-label")
        self.assertEqual((replay["admitted"], replay["newly_started"]), (0, 0), replay)
        self.assertEqual([leg["attempt_id"] for leg in replay["legs"]],
                         [leg["attempt_id"] for leg in round_two["legs"]])
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def atomic_state(self):
        return (self.jobs.read_bytes(), self.trace.read_bytes(),
                {p: p.read_bytes()
                 for directory in ('automatic-replacements', 'batch-reservations',
                                   'review-inputs', 'batch-inputs')
                 for p in (self.jobs.parent / directory).rglob('*')
                 if p.is_file() and not p.name.endswith('.lock')},
                {p: p.read_bytes() for p in (self.art / '.runtime/model-worker-governor').rglob('*.json')})

    def assert_atomic_refusal(self, *, expected=65, reason):
        before = self.atomic_state()
        refused = self.launch(expected=expected)
        self.assertEqual(refused['reason'], reason, refused)
        self.assertEqual(self.atomic_state(), before)
        return refused

    def test_r2_unknown_deferred_and_verdictless_bound_are_atomic(self):
        first = self.launch()
        self.settle(first)
        self.revise_execute(first, 2)
        self.prompt.write_text('Second generation safety matrix.')
        second = self.launch()
        self.settle(second)
        rows = self.jobs.read_bytes()
        target = second['legs'][0]['attempt_id']
        # Synthetic observations are isolated fixture setup; all refusal readers
        # and writers below are the public CLI, with no dependency mocks.
        self.fixture_row(target, status='open', remove=('pid_start',), pid='999999999')
        meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[target][1]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(DC.attempt_process_quiescence(meta).state, 'unverifiable')
        self.assert_atomic_refusal(expected=78, reason='prior-attempt-still-live')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, terminal_conflict='1',
                         conflicting_terminal_note='terminal-observation-unknown',
                         conflicting_failure_class='unknown')
        self.assert_atomic_refusal(reason='round-unsettled')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, remove=('completion_marker',), note='completion-deferred',
                         classifier_source='registered-wrapper-completion-transient-v1')
        self.assert_atomic_refusal(reason='round-unsettled')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, note='dead-invalid-envelope', failure_class='contract')
        fields, meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[target]
        meta = {**meta, 'attempt_id': 'att-fixture-verdictless-2', 'note': 'dead-missing-result'}
        # This extra physical fixture record never had a native launch claim.
        meta.update(launch_claimed='0', launch_started='0', launch_outcome='never-launched')
        for key in ('pid', 'pid_start', 'pgid', 'log_file', 'completion_marker'):
            meta.pop(key, None)
        fields[5] = ','.join(k + '=' + v for k, v in meta.items())
        with self.jobs.open('a') as handle:
            handle.write('\t'.join(fields) + '\n')
        refusal = self.assert_atomic_refusal(reason='review-verdictless-bound')
        self.assertIn('next_action', refusal)

    def test_mixed_pass_fail_revision_through_public_batch(self):
        self.mixed_scenario(one_sided=False)

    def test_mixed_one_sided_supersession_through_public_batch(self):
        self.mixed_scenario(one_sided=True)

    def mixed_scenario(self, *, one_sided):
        self.env['FIXTURE_FAIL_NODE'] = 'impl-review'
        first = self.launch(expected=(0, 70))
        self.settle(first)
        rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
        fail = next(leg for leg in first['legs'] if leg['node'] == 'impl-review')
        sibling = next(leg for leg in first['legs'] if leg['node'] != 'impl-review')
        self.assertEqual(rows[fail['attempt_id']][1]['note'], 'completed-review-blocking')
        self.assertEqual(rows[sibling['attempt_id']][1]['note'], 'completed-marker')
        original = self.historical_bytes()
        original_rows = self.jobs.read_bytes()
        self.env.pop('FIXTURE_FAIL_NODE')
        # A FAIL has no successful marker. Answering just its successful peer
        # produces a genuine one-sided supersession using the canonical writer;
        # the unaddressed FAIL still needs a round through its own admission.
        self.revise_execute({'legs': [sibling]} if one_sided else first, 2)
        if one_sided:
            self.assertEqual(self.marker(sibling['node'])['state'], 'superseded-by-upstream-revision')
            self.assertFalse((self.jobs.parent / 'completion' / self.route['route_id'] /
                              (fail['node'] + '.json')).exists())
            self.assertTrue(self.admission(fail['node']).gate_unmet)
            self.assertFalse(self.admission(sibling['node']).gate_unmet)
        for leg in first['legs']:
            self.assertEqual(self.admission(leg['node']).budget.verdict_rounds, 1)
        self.prompt.write_text('Closure after an actual mixed PASS/FAIL group.')
        second = self.launch()
        self.assertEqual(second['newly_started'], 2, second)
        self.settle(second)
        self.assert_selection(second)
        self.assertEqual(self.launch()['newly_started'], 0)
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def test_one_sided_revision_uses_canonical_writer_and_reader(self):
        first = self.launch()
        self.settle(first)
        original = self.historical_bytes()
        # Revising just one review output is a supported canonical writer
        # operation; its peer remains a successful current result.
        leg = first['legs'][0]
        sibling = first['legs'][1]
        node = next(n for n in self.route['nodes'] if n['id'] == leg['node'])
        evidence = self.output / node['outputs'][0]
        evidence.write_text('Correct this one review result.\n')
        self.route_cmd('revise', '--node', leg['node'], '--evidence', str(evidence),
                       '--basis', 'owner-correction', '--reason', 'One-sided review correction',
                       '--author-attempt-id', self.owner)
        self.inline(leg['node'], evidence, 'att-inline-one-sided')
        sibling_marker = self.marker(sibling['node'])
        self.assertEqual(sibling_marker['attempt_id'], sibling['attempt_id'])
        self.prompt.write_text('One-sided correction cannot authorize new whole-group input.')
        self.assert_atomic_refusal(reason='replacement-batch-source-drift')
        self.assertEqual(self.marker(sibling['node']), sibling_marker)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def settle(self, receipt):
        command = [sys.executable, str(ROOT / "utilities/dispatch_completion_join.py"),
                   "--jobs", str(self.jobs), "--parent-attempt-id", self.owner,
                   "--timeout", "20", "--interval", "0.1"]
        for leg in receipt["legs"]:
            command += ["--attempt-id", leg["attempt_id"]]
        result = self.run_cmd(command)
        for leg in receipt["legs"]:
            node = next(n for n in self.route["nodes"] if n["id"] == leg["node"])
            self.assertTrue((self.output / node["outputs"][0]).is_file(),
                "\n".join(p.name + ": " + p.read_text(errors="replace")
                          for p in (self.jobs.parent / "logs").glob("*" ) if p.is_file()))
            meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[leg['attempt_id']][1]
            if meta.get('note') == 'completed-review-blocking':
                self.assertEqual(meta['failure_class'], 'fail')
                continue
            self.route_cmd("complete", "--node", node["id"],
                "--evidence", str(self.output / node["outputs"][0]),
                "--attempt-id", leg["attempt_id"], "--reviewer-attempt", leg["attempt_id"])
            row = next(line for line in self.jobs.read_text().splitlines()
                       if "attempt_id=" + leg["attempt_id"] + "," in line)
            self.assertEqual(row.split("\t")[1], "done", result.stdout + self.jobs.read_text())
        return result


class AdmissionSelection(unittest.TestCase):
    def test_logical_budget_and_physical_generation_selection(self):
        node = {"id": "impl-review", "kind": "review-worker", "worker_type": "review"}
        route = {"route_id": "rt-selection", "effective_intensity": "strong",
                 "continuation_budget": {"review_round_cap": 2}, "nodes": [node]}
        def row(attempt, status="done", note="completed-marker", **extra):
            return (["now", status, "repo", "cwd", "slug", ""],
                    {"attempt_id": attempt, "worker_type": "review", "note": note, **extra})
        scenarios = [
            ([row("r1")], [], "admit", False, 1),
            ([row("r1")], [{"supersedes": ["r1"]}], "admit", True, 1),
            ([row("r1"), row("r2")], [{"supersedes": ["r1"]}], "exhausted", False, 2),
            ([row("r1"), row("r2")], [{"supersedes": ["r2"]}], "admit", True, 2),
            ([row("r1"), row("r2"), row("r3")], [{"supersedes": ["r3"]}], "exhausted", True, 3),
            ([row("r1"), row("r2", "open")], [{"supersedes": ["r1"]}], "blocked-live", True, 1),
            ([row("r1"), row("r2", note="completion-deferred",
                  classifier_source="registered-wrapper-completion-transient-v1")], [], "blocked-unsettled", False, 1),
            ([row("death1", note="dead-invalid-envelope"), row("death2", note="dead-missing-result")],
             [], "verdictless-bound", False, 0),
            ([row("r1", note="completed-review-blocking")], [], "admit", True, 1),
        ]
        for rows, revisions, state, next_required, verdicts in scenarios:
            with self.subTest(state=state, rows=rows), \
                 mock.patch.object(NODE, "prior_round_attempts", return_value=rows), \
                 mock.patch.object(NODE.ROUTE, "_dependency_revisions", return_value=revisions):
                admission = NODE.admit_round(route, node, Path("unused"), record_auto_revisions=False)
                self.assertEqual(admission.budget.state, state)
                self.assertEqual(admission.budget.verdict_rounds, verdicts)
                self.assertEqual(admission.requires_next_round, next_required)
                self.assertEqual(admission.current_attempt_ids, frozenset((rows[-1][1]["attempt_id"],)))
        # Projection carries both physical ids, but only one verdict. A raw
        # registry string that resembles this field has no exclusion authority.
        for members, expected in ((frozenset(("original", "successor")), frozenset(("original", "successor"))),
                                  ("arbitrary-hidden", frozenset(("successor",)))):
            rows = [row("successor", logical_round_attempt_ids=members)]
            with mock.patch.object(NODE, "prior_round_attempts", return_value=rows), \
                 mock.patch.object(NODE.ROUTE, "_dependency_revisions", return_value=[]):
                admission = NODE.admit_round(route, node, Path("unused"), record_auto_revisions=False)
            self.assertEqual(admission.physical_attempt_ids, expected)
            self.assertEqual(admission.prior_attempt_ids, frozenset(("successor",)))
            self.assertEqual(admission.budget.verdict_rounds, 1)
        default = NODE.RoundAdmission(budget=object())
        self.assertFalse(default.requires_next_round)
        self.assertEqual(default.current_attempt_ids, frozenset())


if __name__ == "__main__":
    unittest.main()
