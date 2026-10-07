import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import start_receipt as receipt
from session_identity import SessionIdentity


class StartReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / 'jobs.log'
        self.result = {'route_id': 'rt-test', 'state': 'running', 'parent_next': 'bounded-wait',
                       'parent_next_command': 'hearting run capability-route start --route rt-test --wait',
                       'launches': [{'receipt': 'started=1\n'}]}

    def save(self, harness='codex', sid='own'):
        with patch.object(receipt, 'identity', return_value=SessionIdentity(harness, sid, 'test', 'sole')):
            return receipt.save(self.result, self.jobs)

    def test_saved_full_receipt_survives_filtered_stdout_and_new_receipt_republishes_once(self):
        saved = self.save()
        path = Path(saved['receipt_file'])
        self.assertEqual(json.loads(path.read_text())['receipt'], saved)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = receipt.context('codex', 'own', self.jobs)
        self.assertIn('parent_next=bounded-wait', text)
        self.assertIn(self.result['parent_next_command'], text)
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')
        self.result['parent_next'] = 'end-turn'
        self.result['parent_next_command'] = ''
        self.save()
        self.assertIn('parent_next=end-turn', receipt.context('codex', 'own', self.jobs))

    def test_foreign_stale_and_malformed_receipts_never_republish(self):
        path = Path(self.save()['receipt_file'])
        self.assertEqual(receipt.context('claude', 'own', self.jobs), '')
        self.assertEqual(receipt.context('codex', 'foreign', self.jobs), '')
        row = json.loads(path.read_text()); row['saved_at'] = 0; path.write_text(json.dumps(row))
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')
        path.write_text('broken')
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')

    def test_storage_failure_and_missing_identity_preserve_execution_result(self):
        with patch.object(receipt, '_write', side_effect=OSError('read only')):
            self.assertIs(self.save(), self.result)
        with patch.object(receipt, 'identity', return_value=SessionIdentity()):
            saved = receipt.save(self.result, self.jobs)
        self.assertEqual(saved['parent_next'], self.result['parent_next'])
        self.assertTrue(Path(saved['receipt_file']).is_file())
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')

    def test_native_hook_json_and_opencode_text_read_same_saved_value(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                self.save(harness)
                env = {**os.environ, 'AGENT_HOME': str(ROOT), 'AGENT_DISPATCH_JOBS': str(self.jobs)}
                if harness == 'opencode':
                    cmd = [sys.executable, str(ROOT/'utilities/start_receipt.py'), '--harness', harness, '--session-id', 'own']
                    payload = ''
                else:
                    cmd = [sys.executable, str(ROOT/f'adapters/{harness}/hooks/start-receipt-context.py')]
                    if harness == 'codex': cmd.append('--codex')
                    payload = json.dumps({'hook_event_name': 'PostToolUse', 'session_id': 'own', 'tool_response': 'FILTERED'})
                proc = subprocess.run(cmd, env=env, input=payload, text=True, capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                text = proc.stdout if harness == 'opencode' else json.loads(proc.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn('parent_next=bounded-wait', text)
                self.assertEqual(subprocess.run(cmd, env=env, input=payload, text=True, capture_output=True).stdout, '')

    def test_installed_hook_copy_and_opencode_callback_keep_receipt_visible(self):
        self.save('claude')
        env = {**os.environ, 'AGENT_HOME': str(ROOT), 'AGENT_DISPATCH_JOBS': str(self.jobs),
               'XDG_STATE_HOME': self.tmp.name}
        installed = Path(self.tmp.name) / 'runtime-hooks/start-receipt-context.py'
        installed.parent.mkdir(); installed.write_bytes((ROOT/'hooks/start-receipt-context.py').read_bytes())
        payload = json.dumps({'hook_event_name': 'PostToolUse', 'session_id': 'own'})
        proc = subprocess.run([sys.executable, str(installed)], input=payload, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('parent_next=bounded-wait', json.loads(proc.stdout)['hookSpecificOutput']['additionalContext'])
        self.save('opencode')
        plugin = (ROOT/'adapters/opencode/plugins/hearting-guards.js').as_uri()
        script = f'''import {{ AgentHarnessGuards }} from {json.dumps(plugin)};
const hooks = await AgentHarnessGuards({{directory: {json.dumps(self.tmp.name)}, client: {{}}}});
const output = {{output: "FILTERED", args: {{}}}};
await hooks["tool.execute.after"]({{tool: "bash", sessionID: "own", args: {{}}}}, output);
console.log(JSON.stringify(output));
hooks.dispose();'''
        proc = subprocess.run(['node', '--input-type=module', '-e', script], env=env, text=True,
                              capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = json.loads(proc.stdout)
        self.assertTrue(output['output'].startswith('FILTERED\n[start-receipt]'))
        self.assertIn('parent_next=bounded-wait', output['output'])


if __name__ == '__main__':
    unittest.main()
