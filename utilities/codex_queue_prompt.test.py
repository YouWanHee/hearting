#!/usr/bin/env python3
"""A native prompt renders pending transport receipts before acknowledging them."""
import contextlib
import importlib.util
import importlib.machinery
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import dispatch_contract as contract
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
spec = importlib.util.spec_from_file_location('queue_prompt_hook', ROOT / 'adapters/codex/hooks/userprompt-lifecycle.py')
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook
spec.loader.exec_module(hook)

class PromptReceiptTest(unittest.TestCase):
    def test_failed_send_is_visible_only_to_exact_parent_and_acked_after_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jobs.log").touch()
            receipt = {'schema_version': 2, 'state': 'ready', 'job_registry': str(root / 'jobs.log'),
                       'parent_attempt_id': 'parent-session-native', 'delivery_classification': 'success',
                       'children': [{'attempt_id': 'att-exact', 'status': 'done', 'readiness': 'ready',
                                     'reason': 'registry-closed', 'required_action': 'advance-completed',
                                     'harness': 'codex', 'delivery_classification': 'success'}]}
            did = 'delivery-native-prompt'
            pending.create(root, recipient_kind='codex-native-queue', recipient_key='native-parent',
                delivery_id=did, session_generation='', session_generation_supported='0',
                attempt_ids=['att-exact'], parent_attempt_id='parent-session-native',
                route_id='rt-test', route_node='execute', receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={'att-exact': 'rev'})
            pending.claim(root, 'native-parent', did, claim_owner='failed-carrier', lease_seconds=60)
            pending.release_claim(root, 'native-parent', did, claim_owner='failed-carrier')
            with mock.patch.object(contract, 'dispatch_state_roots', return_value=[root]):
                self.assertEqual(hook.native_queue_prompt_receipts('sibling'), ([], []))
                batches, texts = hook.native_queue_prompt_receipts('native-parent')
            self.assertIn('att-exact', texts[0])
            self.assertIn('perform already approved parent review/follow-through', texts[0])
            self.assertIn('Receipt, queue consumption and ACK are not execution', texts[0])
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'claimed')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                hook.emit_context('UserPromptSubmit', texts)
            self.assertIn('additionalContext', output.getvalue())
            sweep.ack_delivered(root, 'native-parent', batches[0][1], acked_by='codex-native-prompt:native-parent')
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'acked')

    def test_receiving_turn_uses_native_turn_id_and_rejects_message_alias(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        payload = {"session_id": "native-parent", "turn_id": "turn-native", "message_id": "message-alias"}
        with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=Path("/tmp/queue.sock")), \
             mock.patch.object(codex_queue_delivery, "read_exact_turn", return_value={
                 "id": "turn-native", "status": "inProgress"}) as read_turn:
            self.assertEqual(hook.native_receiving_turn(payload, "native-parent"), {
                "thread_id": "native-parent", "turn_id": "turn-native", "status": "inProgress"})
            self.assertIsNone(hook.native_receiving_turn(
                {"session_id": "native-parent", "message_id": "message-alias"}, "native-parent"))
        self.assertEqual(read_turn.call_count, 1)

    def test_hook_retains_before_emit_and_acks_only_after_successful_render(self):
        import sys
        import types
        sid = "native-parent"
        root = Path("/tmp/native-state")
        record = {"delivery_id": "delivery-exact"}
        batches = [(root, [record])]
        events = []
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda _payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *a, **kw: None)
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *a, **kw: None
        with mock.patch.dict(sys.modules, {"context_alert": context_alert, "fleet": fleet,
                                           "herdr_session_projection": projection}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value={"session_id": sid, "prompt": "continue"}), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=""), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=(batches, ["receipt"])), \
             mock.patch.object(hook, "native_receiving_turn", return_value={
                 "thread_id": sid, "turn_id": "turn-exact", "status": "inProgress"}) as bind, \
             mock.patch("dispatch_session_sweep.retain_receiving_turn",
                        side_effect=lambda *a: events.append("retain") or {(str(root), record["delivery_id"])}), \
             mock.patch.object(hook, "candidate_context", return_value=""), \
             mock.patch.object(hook, "run_preflight", return_value=""), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context", side_effect=lambda *a, **kw: events.append("emit")), \
             mock.patch("dispatch_session_sweep.ack_delivered",
                        side_effect=lambda *a, **kw: events.append("ack")) as ack:
            self.assertEqual(hook.main(), 0)
        self.assertEqual(events, ["retain", "emit", "ack"])
        self.assertEqual(ack.call_args.args[2], [record])
        bind.assert_called_once()

    def test_hook_withholds_completion_ack_when_native_turn_binding_is_unknown(self):
        import sys
        import types
        sid = "native-parent"
        root = Path("/tmp/native-state")
        record = {"delivery_id": "delivery-exact"}
        batches = [(root, [record])]
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda _payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *a, **kw: None)
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *a, **kw: None
        with mock.patch.dict(sys.modules, {"context_alert": context_alert, "fleet": fleet,
                                           "herdr_session_projection": projection}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value={"session_id": sid}), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=""), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=(batches, ["receipt"])), \
             mock.patch.object(hook, "native_receiving_turn", return_value=None), \
             mock.patch("dispatch_session_sweep.retain_receiving_turn", return_value=set()), \
             mock.patch.object(hook, "candidate_context", return_value=""), \
             mock.patch.object(hook, "run_preflight", return_value=""), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context"), \
             mock.patch("dispatch_session_sweep.ack_delivered") as ack:
            self.assertEqual(hook.main(), 0)
        ack.assert_not_called()

    def test_prompt_guidance_receives_exact_codex_payload_identity(self):
        import types
        prompt = "actual Codex prompt (peer-from: claude [0a] peer ; ref=abc)"
        sid = "codex-payload-session"
        calls = []

        class GuidanceLoader:
            def create_module(self, spec):
                return None
            def exec_module(self, module):
                module.receiver_guidance = lambda *args, **kwargs: calls.append((args, kwargs)) or "ordinary guidance"

        guidance_spec = importlib.machinery.ModuleSpec("peer_receiver_guidance_test", GuidanceLoader())
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *args, **kwargs: None
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *args, **kwargs: None)
        payload = {"session_id": sid, "cwd": "/tmp/codex-project", "prompt": prompt}
        no_context = lambda *args, **kwargs: ""
        with mock.patch.dict(sys.modules, {"herdr_session_projection": projection,
                                           "context_alert": context_alert, "fleet": fleet}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value=payload), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), \
             mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), \
             mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=""), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=([], [])), \
             mock.patch.object(hook, "candidate_context", return_value=""), \
             mock.patch.object(hook, "run_preflight", side_effect=no_context), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context"), \
             mock.patch.object(importlib.util, "spec_from_file_location", return_value=guidance_spec):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(calls, [((prompt, {"harness": "codex", "session_id": sid}),
                                  {"cwd": "/tmp/codex-project"})])

if __name__ == '__main__':
    unittest.main()
