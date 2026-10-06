#!/usr/bin/env python3
"""The helpers the three dispatch wrappers shared letter for letter now live once (audit §4 #10)."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_wrapper_common as C  # noqa: E402

ALIASES = {
    "fail": "fail", "jobs_lock": "jobs_lock", "process_start_ticks": "process_start_ticks",
    "read_launch_fence_failure": "read_launch_fence_failure", "resolve_artifact_root": "resolve_artifact_root",
    "_is_report_bundle_publish_stage": "is_report_bundle_publish_stage",
    "resolve_report_bundle_root": "resolve_report_bundle_root", "seed_launch_heartbeat": "seed_launch_heartbeat",
    "_route_node_leg_fields": "route_node_leg_fields", "_supervisor_route": "supervisor_route",
    "prepare_review_output_request": "prepare_review_output_request",
}


def load(harness: str):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_dispatch_headless_common", ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperCommonTest(unittest.TestCase):
    def test_every_wrapper_keeps_the_old_names_as_the_shared_functions(self):
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            for old, new in ALIASES.items():
                with self.subTest(harness=harness, name=old):
                    self.assertIs(getattr(wrapper, old), getattr(C, new))

    def test_a_few_behaviors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(C.fail("x-reason", 64, detail="d"), 64)
        self.assertEqual(out.getvalue(), "check=failed\nreason=x-reason\ndetail=d\n")
        self.assertEqual(C.process_start_ticks(-1), "")
        owner = argparse.Namespace(owner_route_binding=None, route_file="r", route_id="i",
                                   route_hash="h", route_node="one-shot")
        self.assertEqual(C.supervisor_route(owner), ("r", "i", "h"))
        owner.route_node = "execute"
        self.assertIsNone(C.supervisor_route(owner))
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            with C.jobs_lock(jobs):
                self.assertTrue(Path(f"{jobs}.lock").exists())


if __name__ == "__main__":
    unittest.main()
