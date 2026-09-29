#!/usr/bin/env python3
"""Gate-off dispatch uses current inputs while structural failures stay hard."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import dispatch_contract as DC
import hearting_gates as G
import review_input as REVIEW


class HeartingGatesTest(unittest.TestCase):
    def test_switch_and_diagnostic(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(G.gates_on())
            with contextlib.redirect_stderr(io.StringIO()) as output:
                G.same_work_or_refuse('route-hash-mismatch', 'old\nnew')
            self.assertEqual(output.getvalue(), 'hearting: gate-off route-hash-mismatch old new\n')
            with contextlib.redirect_stderr(io.StringIO()) as repeat:
                G.same_work_or_refuse('route-hash-mismatch', 'old\nnew')
                G.same_work_or_refuse('route-hash-mismatch', 'other')
            self.assertEqual(repeat.getvalue(), 'hearting: gate-off route-hash-mismatch other\n')
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            with self.assertRaises(DC.DispatchContractError) as error:
                G.same_work_or_refuse('route-hash-mismatch', 'detail')
            self.assertEqual((error.exception.reason, error.exception.detail), ('route-hash-mismatch', 'detail'))

    def test_review_rebinds_current_bytes_but_preserves_history_and_missing_file_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {'HEARTING_GATES': 'off'}):
            root = Path(td)
            jobs = root / 'jobs.log'
            jobs.touch()
            evidence = root / 'plan.md'
            evidence.write_text('original')
            metadata = dict(attempt_id='att-review', route_id='rt-review', route_hash='sha256:old', route_node='plan-check')
            metadata[REVIEW.KEY] = REVIEW.seal_binding(jobs, metadata, REVIEW._file(evidence))
            binding_path = REVIEW._path(jobs, metadata['attempt_id'])
            original = binding_path.read_bytes()
            evidence.write_text('revised')
            metadata['route_hash'] = 'sha256:current'
            with contextlib.redirect_stderr(io.StringIO()):
                current = REVIEW.read_binding(jobs, metadata, verify_current=True)
            self.assertEqual(current['sha256'], hashlib.sha256(b'revised').hexdigest())
            self.assertEqual(current['route_hash'], 'sha256:current')
            self.assertEqual(binding_path.read_bytes(), original)
            with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
                with self.assertRaises(DC.DispatchContractError):
                    REVIEW.read_binding(jobs, metadata, verify_current=True)
            metadata['route_hash'] = 'sha256:old'
            evidence.rename(root / 'removed.md')
            with self.assertRaises(DC.DispatchContractError) as error:
                REVIEW.read_binding(jobs, metadata, verify_current=True)
            self.assertEqual(error.exception.reason, 'reviewed-evidence-unreadable')

    def test_continuation_keeps_current_turn_instead_of_refusing_old_session_binding(self):
        spec = importlib.util.spec_from_file_location('gate_off_route', Path(__file__).with_name('capability-route.py'))
        route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(route)
        args = ({'runtime_lineage': {'thread_id': 'old', 'node_turn_ids': {'plan': 'old-turn'}}},
                [{'node_id': 'plan'}], {})
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            with self.assertRaisesRegex(ValueError, 'continuation-last-turn-mismatch'):
                route._continuation_lineage(*args, thread_id='current', last_turn_id='current-turn')
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'off'}), contextlib.redirect_stderr(io.StringIO()):
            lineage = route._continuation_lineage(*args, thread_id='current', last_turn_id='current-turn')
        self.assertEqual(lineage['thread_id'], 'current')
        self.assertEqual(lineage['lastTurnId'], 'current-turn')


if __name__ == '__main__':
    unittest.main()
