#!/usr/bin/env python3
"""The memory lifecycle must not run inside a clock it cannot fit.

Codex reaches memory work from two hooks, and a hook has a wall clock. Codex also
clamps the SessionEnd hook to 3 seconds whatever `hooks.json` asks for, and says so on
every session ("clamping SessionEnd hook timeout to 3s"). Nothing on this path fits
that: a live curate measured 21.9s, and the `mem sync` ahead of it measured 54.8s on
the real store — so the sync was killed every time and the curate behind it was never
reached at all.

Raising the declared timeout was tried and is inert (the runtime clamps it back). The
shape that works is to step outside the clock: the hook-reached branch re-enters itself
detached and returns immediately.

The detach tests OBSERVE the script running — an earlier version of this file only read
numbers out of the two files, which is exactly the kind of test that stays green while
the behavior it names is unreachable.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent
ADAPTER = BIN.parent
ROOT = ADAPTER.parent.parent
WORKER = BIN / "distill-worker.sh"
PREFLIGHT = BIN / "preflight.sh"
HOOKS = ADAPTER / "hooks" / "hooks.json"

# What Codex actually enforces on SessionEnd, regardless of what is declared.
CODEX_SESSION_END_CLAMP = 3


def hook_timeout(event):
    """The smallest timeout any hook registered for `event` declares."""
    payload = json.loads(HOOKS.read_text(encoding="utf-8"))
    timeouts = [
        entry.get("timeout")
        for block in payload["hooks"].get(event, [])
        for entry in block.get("hooks", [])
        if entry.get("timeout") is not None
    ]
    if not timeouts:
        raise AssertionError("no %s hook declares a timeout" % event)
    return min(timeouts)


def worker_budget(name):
    """The default seconds for `${name:-<default>}` in the worker script."""
    match = re.search(r"\$\{%s:-(\d+)\}" % re.escape(name), WORKER.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError("%s default not found in %s" % (name, WORKER.name))
    return int(match.group(1))


class DeclaredBudgetTest(unittest.TestCase):
    def test_the_declared_session_end_timeout_matches_what_codex_enforces(self):
        # Asking for more than the clamp does not get more; it only makes the runtime
        # print a warning at every single session end.
        self.assertEqual(hook_timeout("SessionEnd"), CODEX_SESSION_END_CLAMP)

    def test_the_curate_budget_is_long_enough_for_a_real_curate(self):
        # A live curate was measured at 21.9s. A budget under that would make the
        # timeout, not the work, decide the outcome on every run. It no longer has to
        # fit any hook — the work runs detached — but it still has to fit reality.
        self.assertGreater(worker_budget("CODEX_DISTILL_TIMEOUT_CURATE"), 22)

    def test_the_increment_budget_stays_bounded(self):
        # Detached, so no hook waits for it; still bounded, because a runaway
        # background distiller on every tenth turn is its own problem.
        self.assertLessEqual(worker_budget("CODEX_DISTILL_TIMEOUT"), 60)


class _DetachObservingTest(unittest.TestCase):
    """Runs the real script with `setsid` replaced by a recorder.

    Nothing downstream of the detach executes, so no live memory store, ledger or
    session file is touched — and the thing being measured (does this branch hand its
    work off and return?) is measured on the shipped script, not on a copy of it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub_dir = Path(self.tmp.name) / "bin"
        self.stub_dir.mkdir()
        self.record = Path(self.tmp.name) / "setsid.argv"
        stub = self.stub_dir / "setsid"
        stub.write_text(
            "#!/bin/sh\n"
            'echo "CODEX_PREFLIGHT_DETACHED=${CODEX_PREFLIGHT_DETACHED:-}" >> __REC__\n'
            'echo "$*" >> __REC__\n'.replace("__REC__", '"%s"' % self.record),
            encoding="utf-8")
        stub.chmod(0o755)

    def _env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = "%s:%s" % (self.stub_dir, env.get("PATH", ""))
        env["AGENT_HOME"] = str(ROOT)
        env["MEM_STORE"] = str(Path(self.tmp.name) / "store")
        # Not a worker: a worker exits before either branch (D-42).
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL",
                    "CODEX_PREFLIGHT_DETACHED"):
            env.pop(key, None)
        env.update(extra)
        return env

    def _run(self, *args, **extra):
        started = time.monotonic()
        proc = subprocess.run([str(PREFLIGHT)] + list(args), env=self._env(**extra),
                              capture_output=True, text=True, timeout=60,
                              cwd=self.tmp.name)
        return proc, time.monotonic() - started

    def _recorded(self, expect=None, timeout=5.0):
        """What the recorder saw. The hand-off is asynchronous BY DESIGN, so waiting for
        it is part of observing it — the parent returns before its child has run."""
        deadline = time.monotonic() + timeout
        while True:
            text = self.record.read_text(encoding="utf-8") if self.record.exists() else ""
            if expect is None or expect in text or time.monotonic() > deadline:
                return text
            time.sleep(0.02)

    def _recorded_nothing(self, settle=1.0):
        """Proving a NEGATIVE needs a settle window, or it only proves we asked early."""
        time.sleep(settle)
        return self.record.read_text(encoding="utf-8") if self.record.exists() else ""


class SessionEndDetachesTest(_DetachObservingTest):
    def test_it_hands_the_work_off_and_returns_inside_the_clamp(self):
        proc, elapsed = self._run("session-end", self.tmp.name, "sid-x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, CODEX_SESSION_END_CLAMP,
                        "session-end took %.2fs of a %ss hook: %s"
                        % (elapsed, CODEX_SESSION_END_CLAMP, proc.stderr))
        recorded = self._recorded(expect="session-end")
        self.assertIn("session-end", recorded)
        self.assertIn("CODEX_PREFLIGHT_DETACHED=1", recorded)

    def test_the_detached_re_entry_does_not_detach_again(self):
        proc, _elapsed = self._run("session-end", self.tmp.name, "sid-x",
                                   CODEX_PREFLIGHT_DETACHED="1", CODEX_DISTILL_ENABLE="0",
                                   MEM_SYNC_REMOTE="0")
        # It may fail on the sandboxed memory store; what must not happen is another
        # hand-off, which would fork forever.
        self.assertNotIn("session-end", self._recorded_nothing())
        self.assertIsNotNone(proc.returncode)

    def test_a_worker_session_still_owns_no_lifecycle_at_all(self):
        proc, _elapsed = self._run("session-end", self.tmp.name, "sid-x",
                                   AGENT_SESSION_ROLE="worker")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self._recorded_nothing(), "")


class TurnNudgeDetachesTest(_DetachObservingTest):
    def test_the_increment_tier_is_handed_off_too(self):
        # The user is waiting for their own prompt to be accepted; nobody waits 20s for
        # a memory increment.
        proc, elapsed = self._run("turn-nudge", self.tmp.name, "sid-x",
                                  MEM_NUDGE_INTERVAL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 3.0)
        self.assertIn("turn-nudge-distill", self._recorded(expect="turn-nudge-distill"))

    def test_a_turn_that_is_not_due_hands_nothing_off(self):
        proc, _elapsed = self._run("turn-nudge", self.tmp.name, "sid-x",
                                   MEM_NUDGE_INTERVAL="99")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._recorded_nothing(), "")


class DistillGoesThroughTheOneRunnerTest(unittest.TestCase):
    """Every hook-reached call site goes through `run_distill`.

    `run_distill` used to branch on `is_worker_session` and run synchronously for a
    worker. That branch was unreachable from the day it was written: both call sites
    `exit 0` on a worker several lines earlier (D-42). Pinned here so it does not come
    back as a comment describing behavior nobody can observe.
    """

    def setUp(self):
        self.source = PREFLIGHT.read_text(encoding="utf-8")
        self.wrapper = self.source.split("run_distill() {", 1)[1].split("\n}", 1)[0]

    def test_the_runner_is_a_plain_synchronous_call(self):
        self.assertIn("run_distill() {", self.source)
        self.assertNotIn("setsid", self.wrapper)
        self.assertNotIn("is_worker_session", self.wrapper)

    def test_detachment_lives_in_one_place(self):
        self.assertIn("detach_self() {", self.source)
        body = self.source.split("detach_self() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("setsid", body)
        self.assertIn("CODEX_PREFLIGHT_DETACHED=1", body)

    def test_no_hook_driven_call_site_invokes_the_worker_directly(self):
        # `distill-propose` is exempt on purpose: the user types it in a terminal and
        # waits for its output, so it has no hook clock over it and must stay
        # synchronous. Every other call site is reached from a hook.
        outside = self.source.replace(self.wrapper, "")
        blocks = re.split(r"\n  ([a-z0-9|-]+)\)\n", "\n" + outside)
        offenders = []
        for label, body in zip(blocks[1::2], blocks[2::2]):
            if label == "distill-propose":
                continue
            offenders += [
                "%s: %s" % (label, line.strip())
                for line in body.splitlines()
                if "distill-worker.sh" in line and not line.lstrip().startswith("#")
            ]
        self.assertEqual(
            offenders, [],
            "these hook-driven call sites bypass run_distill and block the "
            "hook again: %s" % offenders)

    def test_the_exempt_manual_surface_is_still_present(self):
        # If `distill-propose` is ever renamed or removed, the exemption above silently
        # starts covering nothing — or worse, a real call site.
        self.assertIn("\n  distill-propose)\n", self.source)


if __name__ == "__main__":
    unittest.main()
