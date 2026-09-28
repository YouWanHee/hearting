#!/usr/bin/env python3
"""A native prompt renders pending transport receipts before acknowledging them."""
import contextlib
import importlib.util
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
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'claimed')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                hook.emit_context('UserPromptSubmit', texts)
            self.assertIn('additionalContext', output.getvalue())
            sweep.ack_delivered(root, 'native-parent', batches[0][1], acked_by='codex-native-prompt:native-parent')
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'acked')

if __name__ == '__main__':
    unittest.main()
