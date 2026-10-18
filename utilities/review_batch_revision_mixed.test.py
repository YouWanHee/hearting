#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import unittest
from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture

class RevisionBatchCLI(RevisionBatchFixture):
    def test_mixed_pass_fail_revision_through_public_batch(self):
        self._case_test_mixed_pass_fail_revision_through_public_batch()

    def test_mixed_one_sided_supersession_through_public_batch(self):
        self._case_test_mixed_one_sided_supersession_through_public_batch()

    def test_one_sided_revision_uses_canonical_writer_and_reader(self):
        self._case_test_one_sided_revision_uses_canonical_writer_and_reader()

if __name__ == "__main__":
    unittest.main()
