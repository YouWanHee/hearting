#!/usr/bin/env python3
"""The memory lifecycle must not run inside a clock it cannot fit.

Codex reaches memory work from two hooks, and a hook has a wall clock. Codex also
clamps the SessionEnd hook to 3 seconds whatever `hooks.json` asks for, and says so on
every session ("clamping SessionEnd hook timeout to 3s"). Nothing on this path fits
that: a live curate measured 21.9s, and the `mem sync` ahead of it measured 54.8s on
the real store — so the sync was killed every time and the curate behind it was never
reached at all.

Raising the declared timeout was tried and is inert (the runtime clamps it back). The
integrated path uses the native SessionEnd bridge and receipt-owned nudge launcher.
The tracked inner command stays synchronous so completion cannot outrun its work.

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


class _BridgeObservingTest(unittest.TestCase):
    """Use the real receipt controllers with entirely synthetic side effects."""
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("budget_nudge_fixture", BIN / "distill-nudge-launch.test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fixture = module.NudgeIntegrationTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)


class SessionEndDetachesTest(_BridgeObservingTest):
    def test_it_hands_the_work_off_and_returns_inside_the_clamp(self):
        self.fixture.test_native_session_end_receipt_waits_for_delayed_increment()

    def test_the_detached_re_entry_does_not_detach_again(self):
        # The tracked inner command is synchronous even if upstream's legacy
        # re-entry marker is inherited. All effects finish before it returns.
        child = self.fixture.session_end(MEM_SESSION_COMPLETION="1", CODEX_PREFLIGHT_DETACHED="1")
        _, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertIsNone(self.fixture.end_receipt())
        self.assertIsNone(self.fixture.receipt())
        self.assertEqual([r["event"] for r in self.fixture.events()],
                         ["initial-sync","worker-start","worker-finish","post-sync"])

    def test_a_worker_session_still_owns_no_lifecycle_at_all(self):
        result, _ = self.fixture.native_session_end(AGENT_SESSION_ROLE="worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        time.sleep(1.0)
        self.assertIsNone(self.fixture.end_receipt())
        self.assertEqual(self.fixture.events(), [])
        self.fixture.test_all_worker_flags_skip_prompt_and_launcher_without_state()


class TurnNudgeDetachesTest(_BridgeObservingTest):
    def test_the_increment_tier_is_handed_off_too(self):
        self.fixture.test_tenth_prompt_returns_candidates_before_worker_and_next_counter()

    def test_a_turn_that_is_not_due_hands_nothing_off(self):
        result, elapsed = self.fixture.prompt(MEM_NUDGE_INTERVAL="99")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 3)
        time.sleep(1.0)
        self.assertIsNone(self.fixture.receipt())
        self.assertEqual(self.fixture.events(), [])


class DistillGoesThroughTheOneRunnerTest(_BridgeObservingTest):
    def test_the_runner_is_a_plain_synchronous_call(self):
        self.fixture.test_session_end_waits_for_increment_before_all_completion_phases()

    def test_detachment_lives_in_one_place(self):
        self.fixture.test_active_second_firing_is_deduplicated_then_new_generation_runs()
        source = PREFLIGHT.read_text()
        self.assertNotIn("detach_self()", source)
        self.assertNotIn("setsid nohup", source)

    def test_no_hook_driven_call_site_invokes_the_worker_directly(self):
        # Native hook goes through the deadline-limited bridge; only the tracked
        # synchronous session-end and explicit manual proposal can call a worker.
        registrations = json.loads(HOOKS.read_text())["hooks"]["SessionEnd"]
        self.assertTrue(registrations)
        commands = [entry["command"] for block in registrations for entry in block["hooks"]]
        self.assertTrue(all("sessionend-lifecycle.py" in command for command in commands))
        blocks = re.split(r"\n  ([a-z0-9|-]+)\)\n", "\n" + PREFLIGHT.read_text())
        offenders = [label for label,body in zip(blocks[1::2],blocks[2::2])
                     if label not in ("session-end","distill-propose")
                     and any("distill-worker.sh" in line and not line.lstrip().startswith("#")
                             for line in body.splitlines())]
        self.assertEqual(offenders, [])

    def test_the_exempt_manual_surface_is_still_present(self):
        self.assertIn("\n  distill-propose)\n", PREFLIGHT.read_text())


if __name__ == "__main__":
    unittest.main()
