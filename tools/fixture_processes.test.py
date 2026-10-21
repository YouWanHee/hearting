#!/usr/bin/env python3
"""tools/fixture_processes.py — a detached writer is dead before its temp dir goes."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_processes  # noqa: E402

# Detached, marker only in the ENVIRONMENT (like a `__watch-run` watcher), and it keeps
# writing into the fixture directory until killed.
_WRITER = (
    "import os,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "open(os.path.join(d,'ready'),'w').close()\n"
    "i=0\n"
    "while True:\n"
    "    i+=1; open(os.path.join(d,'w%d'%i),'w').close(); time.sleep(0.005)\n"
)

_DESCENDANT = (
    "import os,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "open(os.path.join(d,'descendant-ready'),'w').write(str(os.getpid()))\n"
    "i=0\n"
    "while True:\n"
    "    i+=1; open(os.path.join(d,'descendant-%d'%i),'w').close(); time.sleep(0.005)\n"
)

_PARENT = (
    "import os,subprocess,sys,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "release=os.environ.get('FIXTURE_RELEASE_PATH')\n"
    "while release and not os.path.exists(release): time.sleep(0.005)\n"
    "p=subprocess.Popen([sys.executable,'-c',os.environ['FIXTURE_DESCENDANT']], "
    "env=os.environ.copy(),start_new_session=True,stdin=subprocess.DEVNULL)\n"
    "open(os.path.join(d,'descendant-pid'),'w').write(str(p.pid))\n"
    "deadline=time.monotonic()+10\n"
    "while not os.path.exists(os.path.join(d,'descendant-ready')) and time.monotonic()<deadline: "
    "time.sleep(0.005)\n"
    "if not os.path.exists(os.path.join(d,'descendant-ready')): raise SystemExit(4)\n"
)


class ReapTest(unittest.TestCase):
    @staticmethod
    def _wait_for(path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def _spawn(self, root, *, detached=True):
        env = dict(os.environ, FIXTURE_DIR=str(root))
        proc = subprocess.Popen([sys.executable, "-c", _WRITER], env=env,
                                start_new_session=detached, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (root / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return proc

    def _spawn_parent(self, root, *, release=None):
        env = dict(os.environ, FIXTURE_DIR=str(root), FIXTURE_DESCENDANT=_DESCENDANT)
        if release is not None:
            env["FIXTURE_RELEASE_PATH"] = str(release)
        return subprocess.Popen([sys.executable, "-c", _PARENT], env=env,
                                start_new_session=True, stdin=subprocess.DEVNULL)

    def test_an_environ_only_writer_is_killed_and_the_dir_removes_cleanly(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        proc = self._spawn(root)
        self.assertNotIn(str(root).encode(), Path(f"/proc/{proc.pid}/cmdline").read_bytes())
        killed = fixture_processes.reap(str(root))
        self.assertIn(proc.pid, killed)
        proc.wait(timeout=5)
        tmp.cleanup()  # raises OSError(ENOTEMPTY) if a writer were still alive
        self.assertFalse(root.exists())

    def test_a_marked_child_in_the_callers_group_is_killed_without_killing_the_caller(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            proc = self._spawn(root, detached=False)
            self.assertEqual(os.getpgid(proc.pid), os.getpgrp())
            self.assertEqual(fixture_processes.reap(str(root)), [proc.pid])
            self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL)

    def test_detached_grandchild_is_gone_before_fixture_directory_removal(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        parent = self._spawn_parent(root)
        try:
            self.assertTrue(self._wait_for(root / "descendant-ready"), "detached grandchild did not start")
            child_pid = int((root / "descendant-ready").read_text())
            child_start = fixture_processes._start_ticks(child_pid)
            self.assertIsNotNone(child_start)
            self.assertNotIn(str(root).encode(), Path(f"/proc/{child_pid}/cmdline").read_bytes())
            self.assertEqual(parent.wait(timeout=5), 0)

            killed = fixture_processes.reap(str(root))

            self.assertIn(child_pid, killed)
            self.assertTrue(fixture_processes._gone(child_pid, child_start))
            self.assertEqual(fixture_processes.marked_processes(str(root)), {})
            tmp.cleanup()
            self.assertFalse(root.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            fixture_processes.reap(str(root))
            if root.exists():
                tmp.cleanup()

    def test_second_scan_reaps_a_detached_descendant_created_after_first_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        release = root / "release-parent"
        parent = self._spawn_parent(root, release=release)
        original_scan = fixture_processes.marked_processes
        first_snapshot = {}
        descendant = {}
        scans = 0

        def delayed_scan(marker):
            nonlocal scans
            batch = original_scan(marker)
            scans += 1
            if scans == 1:
                first_snapshot.update(batch)
                release.touch()
                self.assertTrue(self._wait_for(root / "descendant-ready"),
                                "delayed descendant did not cross the barrier")
                child_pid = int((root / "descendant-ready").read_text())
                child_start = fixture_processes._start_ticks(child_pid)
                self.assertIsNotNone(child_start)
                descendant.update(pid=child_pid, start=child_start)
                self.assertEqual(parent.wait(timeout=5), 0)
            return batch

        try:
            with mock.patch.object(fixture_processes, "marked_processes", side_effect=delayed_scan):
                killed = fixture_processes.reap(str(root))

            child_pid = descendant["pid"]
            self.assertIn(parent.pid, first_snapshot)
            self.assertNotIn(child_pid, first_snapshot)
            self.assertGreaterEqual(scans, 2)
            self.assertIn(child_pid, killed)
            self.assertTrue(fixture_processes._gone(child_pid, descendant["start"]))
            self.assertEqual(original_scan(str(root)), {})
            tmp.cleanup()
            self.assertFalse(root.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            fixture_processes.reap(str(root))
            if root.exists():
                tmp.cleanup()

    def test_reap_does_not_kill_a_writer_owned_by_another_fixture_root(self):
        first_tmp = tempfile.TemporaryDirectory()
        second_tmp = tempfile.TemporaryDirectory()
        first_root, second_root = Path(first_tmp.name), Path(second_tmp.name)
        first = self._spawn(first_root)
        second = self._spawn(second_root)
        second_start = fixture_processes._start_ticks(second.pid)
        try:
            self.assertTrue((first_root / "ready").exists())
            self.assertTrue((second_root / "ready").exists())
            killed = fixture_processes.reap(str(first_root))
            self.assertIn(first.pid, killed)
            self.assertNotIn(second.pid, killed)
            self.assertFalse(fixture_processes._gone(second.pid, second_start))
            self.assertIn(second.pid, fixture_processes.marked_processes(str(second_root)))
            self.assertEqual(first.wait(timeout=5), -signal.SIGKILL)
            self.assertIn(second.pid, fixture_processes.reap(str(second_root)))
            self.assertEqual(second.wait(timeout=5), -signal.SIGKILL)
        finally:
            for proc, root in ((first, first_root), (second, second_root)):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                fixture_processes.reap(str(root))
            first_tmp.cleanup()
            second_tmp.cleanup()

    def test_nothing_marked_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(fixture_processes.reap(name), [])


if __name__ == "__main__":
    unittest.main()
