#!/usr/bin/env python3
"""Assert the four split suites collect each original test identity once."""
from collections import Counter
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "review_batch_revision.test.py",
    "review_batch_revision_resume.test.py",
    "review_batch_revision_replacement.test.py",
    "review_batch_revision_mixed.test.py",
)
EXPECTED = Counter({
    "test_two_pass_revision_inline_next_generation_and_cap_replay": 1,
    "test_revision_generation_and_replay_with_gates_on": 1,
    "test_foreign_registry_cannot_override_the_sealed_jobs": 1,
    "test_logical_budget_and_physical_generation_selection": 1,
    "test_one_started_one_missing_admitted_resume": 1,
    "test_both_zero_admitted_resume_with_gates_on": 1,
    "test_closure_capped_peer_pending_sibling": 1,
    "test_live_peer_preserved_and_unknown_unsettled_refused": 1,
    "test_death_partial_replacement_then_revision_and_cap_replay": 1,
    "test_r2_unknown_deferred_and_verdictless_bound_are_atomic": 1,
    "test_mixed_pass_fail_revision_through_public_batch": 1,
    "test_mixed_one_sided_supersession_through_public_batch": 1,
    "test_one_sided_revision_uses_canonical_writer_and_reader": 1,
})


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class ReviewBatchRevisionCollectionTest(unittest.TestCase):
    def test_original_thirteen_methods_are_collected_once_without_inheritance(self):
        observed = Counter()
        for index, filename in enumerate(SUITES):
            path = ROOT / "utilities" / filename
            spec = importlib.util.spec_from_file_location(f"revision_split_{index}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            observed.update(test._testMethodName for test in _flatten(suite))
        self.assertEqual(observed, EXPECTED)
        from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture
        self.assertEqual(unittest.defaultTestLoader.getTestCaseNames(RevisionBatchFixture), [])
        self.assertEqual(unittest.defaultTestLoader.getTestCaseNames(AdmissionSelectionFixture), [])


if __name__ == "__main__":
    unittest.main()
