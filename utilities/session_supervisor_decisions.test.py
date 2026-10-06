#!/usr/bin/env python3
"""Both session supervisors make their continuation and stage decisions through one module."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_supervisor_decisions as D  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUPERVISORS = {"claude": load("claude-session-supervisor"), "codex": load("codex-app-server-supervisor")}


class SharedDecisionsTest(unittest.TestCase):
    def test_both_supervisors_use_the_shared_error_and_notice(self):
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                self.assertIs(module.SupervisorError, D.SupervisorError)
                self.assertIs(module._apply_notice, D.apply_notice)

    def test_an_exhausted_admit_warns_through_the_callers_emit_on_both(self):
        ledger = SimpleNamespace(gross_remaining=0, stall_remaining=0, reserved_remaining=0,
                                 admit=lambda **kw: SimpleNamespace(admitted=False, gross_remaining=0,
                                                                    stall_remaining=0, reserved_remaining=0))
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                events = []
                with mock.patch.object(D.budget_record, "reserve", return_value=(False, "")), \
                     mock.patch.object(D.budget_record, "record_warning", side_effect=OSError("disk")), \
                     mock.patch.object(module, "emit", events.append):
                    verdict, notice = module._admit_continuation(
                        ledger, "/state", parent_attempt_id="att-1", route_id="rt-1", route_hash="sha256:1",
                        ordinal=1, purpose="ordinary", stalled=False)
                self.assertFalse(verdict.admitted)
                self.assertEqual(notice, "")
                self.assertEqual([event["type"] for event in events],
                                 ["dispatch.supervisor.continuation-budget-warning-unrecorded"])

    def test_a_spent_reserve_refuses_the_terminal_handoff_alike(self):
        ledger = SimpleNamespace(reserved_remaining=0)
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name), self.assertRaises(D.SupervisorError):
                module._seal_terminal_handoff_or_raise(
                    ledger, "/state", args=SimpleNamespace(), ordinal=1, failure_reason="budget",
                    terminal_handoff_issued=[False])

    def test_stage_advance_is_off_unless_enabled_and_names_each_runtime(self):
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                self.assertIsNone(module.attempt_stage_advance(SimpleNamespace(enable_stage_advance=False), [], set()))
        source = {name: (HERE / f"{file}.py").read_text() for name, file in
                  (("claude", "claude-session-supervisor"), ("codex", "codex-app-server-supervisor"))}
        self.assertIn('default_harness="claude"', source["claude"])
        self.assertIn('default_harness="codex"', source["codex"])


if __name__ == "__main__":
    unittest.main()
