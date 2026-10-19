"""The reference-lock directory works where no-follow chmod is unavailable.

glibc < 2.32 (Ubuntu 20.04, Debian 11, RHEL 8) rejects
``os.chmod(path, mode, follow_symlinks=False)`` with NotImplementedError. An
escaped NotImplementedError ends every caller of ``admission`` -- the owner
supervisor delivering a finished stage among them -- so the directory mode is
set through a no-follow descriptor there instead, with the same trust checks.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dispatch_attempt_reference as R

REAL_CHMOD = os.chmod


def _chmod_without_nofollow(path, mode, *args, follow_symlinks=True, **kwargs):
    """CPython's os.chmod on glibc < 2.32: the no-follow form is unavailable."""
    if not follow_symlinks:
        raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
    return REAL_CHMOD(path, mode, *args, **kwargs)


class NoFollowChmodUnavailableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(R.os, "chmod", _chmod_without_nofollow)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_directory_gets_owner_only_mode(self):
        directory = self.root / R.LOCK_DIRECTORY
        directory.mkdir()
        REAL_CHMOD(directory, 0o755)
        R._ensure_directory(directory)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), R.DIR_MODE)

    def test_admission_creates_and_holds_reference_locks(self):
        with R.admission(self.root, ["att-h6-first", "att-h6-second"]) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(R.held_attempts(), {"att-h6-first", "att-h6-second"})
        directory = self.root / R.LOCK_DIRECTORY
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), R.DIR_MODE)
        self.assertEqual(R.held_attempts(), frozenset())

    def test_symlinked_directory_is_still_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        REAL_CHMOD(target, 0o755)
        link = self.root / R.LOCK_DIRECTORY
        link.symlink_to(target)
        with self.assertRaises(R.AttemptReferenceError) as caught:
            R._ensure_directory(link)
        self.assertEqual(str(caught.exception), "attempt-reference-directory-untrusted")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
