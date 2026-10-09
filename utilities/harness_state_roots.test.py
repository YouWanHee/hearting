#!/usr/bin/env python3
"""Which harness state a launch writes is one rule, read the same way by every adapter (audit §4 #8, A7)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import harness_state_roots as H  # noqa: E402
from dispatch_contract import dispatch_state_root  # noqa: E402


class HarnessStateRootsTest(unittest.TestCase):
    def test_recursive_owner_can_append_route_history_without_granting_it_to_workers(self):
        ledger = Path(self._tmp.name) / "route-chains"
        with mock.patch.dict("os.environ", {"FLEET_ROUTE_CHAIN_DIR": str(ledger)}):
            owner = self.args(nested_headless_network=True)
            self.assertIn(ledger, H.route_bound_worker_writable_dirs(owner))
            worker = self.args(route_id="rt-worker", nested_headless_network=False)
            self.assertNotIn(ledger, H.route_bound_worker_writable_dirs(worker))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        (self.home / ".core-grounding").mkdir(parents=True)
        self.jobs = Path(self._tmp.name) / "state" / "jobs.log"

    def args(self, **values):
        base = dict(agent_home=str(self.home), jobs_path=str(self.jobs))
        base.update(values)
        return argparse.Namespace(**base)

    def test_each_rule(self):
        plain, routed = self.args(), self.args(route_id="rt-1", command_attempt_id="att-1")
        registered = self.args(execution_surface="registered-headless", registered_worker=1)
        root = dispatch_state_root(str(self.jobs))
        self.assertEqual(H.progress_writable_dirs(plain), (root / "heartbeats", root / "watchdog"))
        self.assertEqual(H.progress_writable_dirs(self.args(jobs_path=None)), ())
        self.assertEqual(H.route_bound_worker_writable_dirs(plain), ())
        self.assertEqual(H.route_bound_worker_writable_dirs(routed), ((self.home / ".core-grounding").resolve(),))
        self.assertEqual([H.spec_read_marker_required(a) for a in (plain, routed, registered)], [False, True, True])
        self.assertEqual([H.registry_writable_launch(a) for a in (plain, routed, self.args(route_id="rt-1"))],
                         [False, True, False])
        self.assertEqual(H.spec_grounding_dir(plain), self.home / ".spec-grounding")

    def test_the_codex_wrapper_projects_the_shared_answer(self):
        spec = importlib.util.spec_from_file_location(
            "codex_dispatch_headless_state_roots", ROOT / "adapters/codex/bin/dispatch-headless.py")
        codex = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codex)
        for name, shared in (("progress_writable_dirs", H.progress_writable_dirs),
                             ("route_bound_worker_writable_dirs", H.route_bound_worker_writable_dirs),
                             ("spec_read_marker_required", H.spec_read_marker_required),
                             ("registry_writable_launch", H.registry_writable_launch),
                             ("_spec_grounding_dir", H.spec_grounding_dir),
                             ("_core_grounding_dir", H.core_grounding_dir)):
            with self.subTest(name=name):
                self.assertIs(getattr(codex, name), shared)
        ledger = Path(self._tmp.name) / "fresh-ledger"
        with mock.patch.dict("os.environ", {"FLEET_ROUTE_CHAIN_DIR": str(ledger)}), \
                mock.patch.object(codex, "owner_root", return_value=Path(self._tmp.name) / "titles"):
            self.assertFalse(ledger.exists())
            codex.ensure_owner_writable_dirs(self.args(nested_headless_network=True))
            self.assertTrue(ledger.is_dir())

    def test_only_an_adapter_that_confines_writes_opens_them(self):
        # The rule is shared; opening the directories is the realization of an
        # adapter whose declared enforcement confines writes.
        declared = {h: json.loads((ROOT / "adapters" / h / "config" / "harness-capabilities.json")
                                  .read_text(encoding="utf-8"))["access"]["enforcement"]
                    for h in ("claude", "codex", "opencode")}
        self.assertEqual({h for h, e in declared.items() if e == "os-sandbox"}, {"codex"})


if __name__ == "__main__":
    unittest.main()
