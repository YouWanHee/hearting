#!/usr/bin/env python3
"""SD-111 P4: dispatch_session_sweep unit tests.

Every fixture injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into an isolated
temp tree and appends the actual values to evidence/sd111/fixture_env.tsv
(plan §10.1 hard gate), matching P1/P2's own fixture pattern.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PD = _load("dispatch_pending_delivery", HERE / "dispatch_pending_delivery.py")
SWEEP = _load("dispatch_session_sweep", HERE / "dispatch_session_sweep.py")

FIXTURE_ENV_LOG = os.environ.get("SD111_FIXTURE_ENV_LOG")


def _log_fixture_env(test_file: str, home: str, xdg: str, harness: str) -> None:
    if not FIXTURE_ENV_LOG:
        return
    with open(FIXTURE_ENV_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{test_file}\t{home}\t{xdg}\t{harness}\n")


class IsolatedRootMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p4-")
        base = Path(self._tmp.name)
        home = base / "home"
        xdg = base / "xdg-state"
        harness = base / "harness-state"
        for d in (home, xdg, harness):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(harness),
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)
        _log_fixture_env(
            "dispatch_session_sweep.test.py", str(home), str(xdg), str(harness)
        )
        self.root = harness / "dispatch"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        super().tearDown()


def _receipt(**overrides):
    base = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p4/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base.update(overrides)
    return base


class SweepTest(IsolatedRootMixin, unittest.TestCase):
    def _seed(self, session_id="sess-owner", **overrides):
        receipt = overrides.pop("receipt", _receipt())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        receipt["job_registry"] = str(self.root / "jobs.log")
        kwargs = dict(
            root=self.root,
            recipient_kind="claude-parent-runtime",
            recipient_key=session_id,
            delivery_id="delivery-" + "a" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=PD._canonical_receipt_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        kwargs.update(overrides)
        return PD.create(**kwargs)

    # -- 2026-08-29: Claude carrier 2 delivers (sweep_deliver / ack_delivered).

    def test_deliver_claims_pending_record_without_generation_proof(self):
        self._seed()
        records, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "claimed")
        self.assertEqual(records[0]["attempts"], 1)
        text = SWEEP._bounded_receipt_text(records[0])
        self.assertIn("delivery_id=delivery-" + "a" * 32, text)
        self.assertIn("attempt_id=att-0000000000000000000000000000bbbb", text)
        self.assertNotIn("job_registry", text)

    def test_deliver_then_ack_makes_record_terminal_and_silent_next_time(self):
        self._seed()
        records, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(SWEEP.ack_delivered(self.root, "sess-owner", records, acked_by="t"), 1)
        again, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(count, 1)
        self.assertEqual(again, [])
        state = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"]
        self.assertEqual(state, "acked")

    def test_deliver_reclaims_sent_ambiguous_record_only_after_lease_expiry(self):
        # The async rewake carrier leaves a record sent-ambiguous; a lost wake
        # must be re-deliverable by the sweep once the lease has expired.
        self._seed()
        first = PD.claim(
            self.root, "sess-owner", "delivery-" + "a" * 32,
            claim_owner="rewake:1", lease_seconds=60.0,
        )
        PD.mark_sent_ambiguous(
            self.root, "sess-owner", "delivery-" + "a" * 32, claim_owner="rewake:1"
        )
        early, _ = SWEEP.sweep_deliver(
            self.root, "claude-parent-runtime", "sess-owner", now_ns=first["claim_deadline_ns"] - 1
        )
        self.assertEqual(early, [])
        late, _ = SWEEP.sweep_deliver(
            self.root, "claude-parent-runtime", "sess-owner", now_ns=first["claim_deadline_ns"] + 1
        )
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["attempts"], 2)

    def test_deliver_ignores_foreign_session_and_foreign_recipient_kind(self):
        self._seed()
        self.assertEqual(
            SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-other"), ([], 0)
        )
        records, count = SWEEP.sweep_deliver(self.root, "codex-stop-hook", "sess-owner")
        self.assertEqual((records, count), ([], 1))

    def test_a_successor_at_the_same_pane_receives_the_stored_record_and_acks_it_under_the_old_key(self):
        # After a /clear the record stays stored (and acked) under the registered parent; the
        # handover binds only the attempts it names to the new session.
        import dispatch_seat_handover as handover
        from unittest import mock
        self._seed(session_id="sess-old")
        self._seed(session_id="sess-old", delivery_id="delivery-" + "b" * 32,
                   attempt_ids=["att-0000000000000000000000000000cccc"],
                   row_revisions={"att-0000000000000000000000000000cccc": "beef"})
        bound = frozenset({"att-0000000000000000000000000000bbbb"})
        with mock.patch.object(handover, "storage_recipients",
                               return_value=[("sess-new", None), ("sess-old", bound)]):
            records, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual([r["delivery_id"] for r in records], ["delivery-" + "a" * 32])
            self.assertEqual(count, 2)                                  # both are seen; only the bound one is taken
            self.assertEqual(SWEEP.ack_delivered(self.root, "sess-new", records, acked_by="t"), 1)
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "a" * 32)["state"], "acked")
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "b" * 32)["state"], "pending")
        self.assertIsNone(PD.read(self.root, "sess-new", "delivery-" + "a" * 32))

    def test_without_a_handover_another_session_still_receives_nothing(self):
        self._seed(session_id="sess-old")
        self.assertEqual(SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new"), ([], 0))
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "a" * 32)["state"], "pending")

    def test_hook_injects_additional_context_and_acks(self):
        self._seed()
        hook = Path(__file__).resolve().parents[1] / "hooks" / "dispatch-session-sweep.py"
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.root.parent)
        env["HARNESS_STATE_ROOT"] = str(self.root)
        env["AGENT_DISPATCH_JOBS"] = str(self.root / "jobs.log")
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"session_id": "sess-owner", "hook_event_name": "UserPromptSubmit"}),
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("att-0000000000000000000000000000bbbb", context)
        self.assertNotIn("Harvest each attempt", context)
        self.assertIn("Follow each receipt's required_action", context)
        state = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"]
        self.assertEqual(state, "acked")

    # -- A-21: generation-unproven claim refused, state unchanged. ---------

    def test_a21_sentinel_record_refused_generation_unproven_state_unchanged(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-owner", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["claim_owner"])

    def test_a21_refusal_reason_is_generation_fence_not_absence(self):
        # Distinguishes carrier 2's refusal (generation fence, this test)
        # from carrier 1's refusal (incarnation-binding mismatch, covered in
        # hooks/dispatch_owner_rewake.test.py) per round 2 C-3.
        self._seed("sess-owner")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-owner", "delivery-" + "a" * 32,
                claim_owner="probe", lease_seconds=30.0,
                require_generation_proof=True,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-generation-unproven")

    # -- A47-8: claim_authority is per-claim, not sticky across a reclaim. -

    def test_a47_8_carrier_timeout_keeps_pending_then_sweep_acks(self):
        # A carrier-2 (sweep_deliver) claim that times out before ack is a
        # deliverer-unproven claim that never got consumed -- the next
        # sweep_deliver pass reclaims it back to pending (§13.34.1-(2): a
        # timed-out claim is not a stuck grade). A record whose session
        # later proves generation (session_generation_supported="1", unlike
        # A-21's real-world unsupported fixtures above) can then be claimed
        # and acked by carrier 1 (sweep) as generation-proven -- the grade
        # recorded is whichever claim actually won, not whatever claimed it
        # first.
        self._seed("sess-owner", session_generation_supported="1")
        delivery_id = "delivery-" + "a" * 32
        first, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["claim_authority"], "deliverer-unproven")
        # Timeout: never acked. Lease expires; reclaim releases it to pending.
        PD.reclaim(
            self.root, "sess-owner", delivery_id, now_ns=first[0]["claim_deadline_ns"] + 1,
        )
        released = PD.read(self.root, "sess-owner", delivery_id)
        self.assertEqual(released["state"], "pending")
        outcome, count = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "proven")
        self.assertEqual((outcome, count), ("claimed", 1))
        claimed = PD.read(self.root, "sess-owner", delivery_id)
        self.assertEqual(claimed["state"], "claimed")
        self.assertEqual(claimed["claim_authority"], "generation-proven")
        acked = PD.ack(self.root, "sess-owner", delivery_id, acked_by="carrier-1")
        self.assertEqual(acked["state"], "acked")

    # -- A-11(i): different session_id -> different digest -> dir absent. --

    def test_a11_foreign_session_reads_zero_claims_zero(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-foreign", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))
        self.assertFalse(
            PD.record_directory(self.root, "sess-foreign").is_dir()
        )

    # -- A-11(ii)/A-21 second incarnation: carrier 2 has no process binding, -
    # -- so a second incarnation of the *same* session_id is indistinguish- -
    # -- able from the first by digest; the generation fence refuses both. -

    def test_second_incarnation_same_session_id_still_refused_by_fence(self):
        self._seed("sess-owner")
        first = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        second = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(first, ("refused", 1))
        self.assertEqual(second, ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)

    # -- empty / absent directory: no entries, no exception. ---------------

    def test_no_records_for_session_returns_refused_zero(self):
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-empty", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))

    # -- fail-open: an unreadable directory must not raise. -----------------

    def test_unreadable_directory_fails_open(self):
        self._seed("sess-locked")
        directory = PD.record_directory(self.root, "sess-locked")
        original_mode = directory.stat().st_mode
        try:
            os.chmod(directory, 0)
            if os.access(directory, os.R_OK):
                self.skipTest("running as a user that bypasses directory permissions")
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-locked", "unsupported"
            )
            self.assertEqual((outcome, count), ("refused", 0))
        finally:
            os.chmod(directory, original_mode)

    # -- self-instrumentation: observation only, never a gate. -------------

    def test_sweep_never_materializes_a_missing_root(self):
        # Regression: the legacy <agent-home>/.dispatch read-order root does
        # not exist for a packaged release; sweeping it must not create
        # `<release>/.dispatch/logs/` (that write made every superseded
        # release refuse pruning with delta-digest-mismatch).
        missing = Path(self._tmp.name) / "release" / ".dispatch"
        records, entries = SWEEP.sweep_deliver(missing, "claude-parent-runtime", "sess-missing")
        self.assertEqual((records, entries), ([], 0))
        self.assertFalse(missing.exists())
        self.assertFalse((Path(self._tmp.name) / "release").exists())

    def test_self_instrumentation_appends_one_line_per_sweep(self):
        self._seed("sess-owner")
        log_path = self.root / "logs" / SWEEP.LOG_FILENAME
        self.assertFalse(log_path.exists())
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertTrue(log_path.is_file())
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(set(payload), {"ts_ns", "elapsed_ns", "entries", "claimed"})
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(payload["claimed"], 0)
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(
            len(log_path.read_text(encoding="utf-8").splitlines()), 2
        )

class OpenCodeTurnCarrierTest(IsolatedRootMixin, unittest.TestCase):
    """The OpenCode plugin carrier: look, claim and render through the shared sweep, hand the text
    to the idle session's next turn, then ack (taken) or release (not taken)."""

    SID = "ses-oc-parent"
    _seed = SweepTest._seed

    def env(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        env = dict(os.environ)
        env.update(HARNESS_STATE_ROOT=str(self.root), AGENT_DISPATCH_JOBS=str(self.root / "jobs.log"),
                   AGENT_HOME=str(ROOT))
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH"):
            env.pop(key, None)
        return env

    def seed_turn(self, **overrides):
        return self._seed(self.SID, recipient_kind="opencode-turn", **overrides)

    def state(self):
        return PD.read(self.root, self.SID, "delivery-" + "a" * 32)["state"]

    def cli(self, action, stdin=""):
        proc = subprocess.run([sys.executable, str(HERE / "dispatch_session_sweep.py"), action,
                               "--recipient-kind", "opencode-turn", "--session", self.SID],
                              input=stdin, capture_output=True, text=True, env=self.env(), check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_sweep_steps_claim_render_and_settle_one_record(self):
        self.seed_turn()
        roots = self.cli("roots")
        self.assertIn(str(self.root), roots)
        owed = self.cli("deliver")
        self.assertEqual([item["delivery_id"] for item in owed["records"]], ["delivery-" + "a" * 32])
        self.assertIn("att-0000000000000000000000000000bbbb", owed["text"])
        self.assertIn("Follow each receipt's required_action", owed["text"])
        self.assertEqual(self.state(), "claimed")
        self.assertEqual(self.cli("deliver")["records"], [])      # a live claim is not handed out twice
        self.assertEqual(self.cli("release", json.dumps(owed))["count"], 1)
        self.assertEqual(self.state(), "pending")
        again = self.cli("deliver")
        self.assertEqual(self.cli("ack", json.dumps(again))["count"], 1)
        self.assertEqual(self.state(), "acked")

    def test_every_runtime_renders_delivered_records_with_one_text(self):
        self.seed_turn()
        records, _ = SWEEP.sweep_deliver(self.root, "opencode-turn", self.SID)
        text = SWEEP.delivery_context([(self.root, records)])
        self.assertTrue(text.startswith(SWEEP.COMPLETION_DELIVERY_HEADER))
        self.assertIn("harvest", text)      # the exact next handle the queue carrier also sends
        gate = _receipt(children=[{**_receipt()["children"][0], "required_action": "human-gate:frame-review",
                                   "reason": "/tmp/frame-summary.json"}])
        gate_record = {"delivery_id": "delivery-gate", "receipt": gate}
        text = SWEEP.delivery_context([(self.root, [gate_record])])
        self.assertTrue(text.startswith(SWEEP.GATE_DELIVERY_HEADER))
        self.assertIn("gate=frame-review", text)
        self.assertEqual(SWEEP.delivery_context([]), "")
        for hook in (ROOT / "hooks" / "dispatch-session-sweep.py",
                     ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"):
            self.assertIn("delivery_context", hook.read_text(encoding="utf-8"))

    def run_plugin(self, body, *, prompt_async="accept"):
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const prompts = [];
const session = {
  messages: async () => ({data: []}), prompt: async () => ({data: null}),
  promptAsync: async (request) => { prompts.push(request); return MODE === "accept" ? {data: undefined, response: {ok: true, status: 204}} : {error: {name: "NotFound"}, response: {ok: false, status: 404}} },
};
if (MODE === "absent") delete session.promptAsync;
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
const settle = async () => { for (let i = 0; i < 100 && !globalThis.done; i++) await new Promise(r => setTimeout(r, 50)); };
BODY
hooks.dispose();
console.log(JSON.stringify({prompts}));
'''.replace("MODE", json.dumps(prompt_async)).replace("BODY", body)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=self.env(), cwd=str(self.root),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    IDLE_THEN_WAIT = r'''
const out = {env: {}};
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, out);
globalThis.carrierEnv = out.env.AGENT_PARENT_COMPLETION_CARRIER;
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
for (let i = 0; i < 200; i++) { await new Promise(r => setTimeout(r, 50)); if (prompts.length) break; }
await new Promise(r => setTimeout(r, 1500));
console.log(JSON.stringify({carrierEnv: globalThis.carrierEnv}));
'''

    def test_an_idle_parent_gets_its_record_as_its_next_turn_and_the_record_is_acked(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(len(result["prompts"]), 1)
        request = result["prompts"][0]
        self.assertEqual(request["path"], {"id": self.SID})
        self.assertNotIn("noReply", request["body"])      # a turn, not a silent insert
        self.assertIn("att-0000000000000000000000000000bbbb", request["body"]["parts"][0]["text"])
        self.assertEqual(self.state(), "acked")

    def test_a_turn_opencode_did_not_take_is_released_for_the_next_pass(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, prompt_async="refuse")
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(self.state(), "pending")

    def test_a_record_written_after_the_parent_went_idle_arrives_by_the_interval_look(self):
        # The usual order: the parent yields first, the owner finishes later. Only the
        # plugin's own interval look (no event from the idle session) can deliver it.
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const prompts = [];
const session = {messages: async () => ({data: []}), prompt: async () => ({data: null}),
  promptAsync: async (request) => { prompts.push(request); return {response: {ok: true, status: 204}} }};
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, {env: {}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
console.log("idle");
for (let i = 0; i < 300 && !prompts.length; i++) await new Promise(r => setTimeout(r, 100));
await new Promise(r => setTimeout(r, 1500));
hooks.dispose();
console.log(JSON.stringify({prompts}));
process.exit(0);
'''
        self.env()
        node = subprocess.Popen(["node", "--input-type=module", "-e", js], env=self.env(), cwd=str(self.root),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(node.stdout.readline().strip(), "idle")
            self.seed_turn()                                    # the owner finishes now
            out, err = node.communicate(timeout=60)
        finally:
            if node.poll() is None:
                node.kill()
        self.assertEqual(node.returncode, 0, err)
        prompts = json.loads(out.strip().splitlines()[-1])["prompts"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("att-0000000000000000000000000000bbbb", prompts[0]["body"]["parts"][0]["text"])
        self.assertEqual(self.state(), "acked")

    def test_the_carrier_names_itself_only_where_it_can_carry(self):
        script = r'''
const out = {env: {}};
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, out);
console.log(JSON.stringify({carrierEnv: out.env.AGENT_PARENT_COMPLETION_CARRIER}));
'''
        for mode, expected in (("accept", "opencode-turn:ses-oc-parent"), ("absent", "")):
            with self.subTest(mode=mode):
                js_env = self.run_plugin_env(script, mode)
                self.assertEqual(js_env, expected)

    def run_plugin_env(self, script, mode):
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const session = {promptAsync: async () => ({response: {ok: true}})};
if (MODE === "absent") delete session.promptAsync;
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
BODY
hooks.dispose();
'''.replace("MODE", json.dumps(mode)).replace("BODY", script)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=self.env(), cwd=str(self.root),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])["carrierEnv"]


if __name__ == "__main__":
    unittest.main()
