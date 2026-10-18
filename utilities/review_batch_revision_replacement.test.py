#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import unittest
from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture

class RevisionBatchCLI(RevisionBatchFixture):
    def test_death_partial_replacement_then_revision_and_cap_replay(self):
        self._case_test_death_partial_replacement_then_revision_and_cap_replay()

    def test_r2_unknown_deferred_and_verdictless_bound_are_atomic(self):
        self._case_test_r2_unknown_deferred_and_verdictless_bound_are_atomic()

if __name__ == "__main__":
    unittest.main()
