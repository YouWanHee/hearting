#!/usr/bin/env python3
"""Candidate assertions for the hermetic sibling delivery measurement."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).with_name("dispatch_delivery_batch_fixture.py")


class DeliveryBatchMeasurementTest(unittest.TestCase):
    def test_candidate_shared_sweep_coalesces_success_and_delivers_failures(self):
        with tempfile.TemporaryDirectory(prefix="delivery-batch-test-") as temporary:
            output = Path(temporary) / "candidate.json"
            completed = subprocess.run(
                [sys.executable, str(DRIVER), "--source-root", str(ROOT), "--output", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))

        measured = {row["variant"]: row["observed"] for row in result["measurements"]}
        self.assertEqual(set(measured), {"all_success", "two_failures"})
        self.assertEqual(measured["all_success"]["wakes"], 1)
        self.assertEqual(measured["two_failures"]["wakes"], 3)
        for variant, observation in measured.items():
            self.assertEqual(set(observation["ack_transition_count_by_record"].values()), {1}, variant)
            self.assertEqual(observation["unsettled_record_ids"], [], variant)
        failures = measured["two_failures"]["failure_delivery_completion"]
        self.assertEqual(sorted(failures.values()), [8, 19])

        print("carrier | variant | wakes | emissions | injected chars | fixed chars | ACKs | unsettled")
        for variant in ("all_success", "two_failures"):
            observation = measured[variant]
            print(f"shared_sweep | {variant} | {observation['wakes']} | "
                  f"{observation['emissions']} | {observation['injected_characters']} | "
                  f"{observation['fixed_instruction_characters']} | "
                  f"{sum(observation['ack_transition_count_by_record'].values())} | "
                  f"{len(observation['unsettled_record_ids'])}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
