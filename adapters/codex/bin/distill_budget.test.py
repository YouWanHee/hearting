#!/usr/bin/env python3
"""A distillation budget must fit inside the hook that waits for it.

Codex runs distillation from inside a hook. A hook has a wall clock, and when
the worker's own budget is larger than that clock the hook is reaped first: the
work is thrown away, the marker never advances, and every session end fails the
same way in silence. That is what shipped — a 600s curate budget inside a 3s
SessionEnd hook, measured killing a real 21.9s curate.

These tests pin the two numbers to each other so the inversion cannot return.
They read the shipped files, not a fixture, because the defect was that the two
files disagreed.
"""

import json
import re
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent
ADAPTER = BIN.parent
WORKER = BIN / "distill-worker.sh"
PREFLIGHT = BIN / "preflight.sh"
HOOKS = ADAPTER / "hooks" / "hooks.json"


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


class DistillBudgetFitsItsHookTest(unittest.TestCase):
    def test_curate_budget_fits_inside_the_session_end_hook(self):
        budget = worker_budget("CODEX_DISTILL_TIMEOUT_CURATE")
        hook = hook_timeout("SessionEnd")
        self.assertLess(
            budget, hook,
            "curate may run for %ss inside a %ss SessionEnd hook: the hook is "
            "reaped first and the distillation is discarded" % (budget, hook))

    def test_increment_budget_fits_inside_the_prompt_hook(self):
        budget = worker_budget("CODEX_DISTILL_TIMEOUT")
        hook = hook_timeout("UserPromptSubmit")
        self.assertLess(
            budget, hook,
            "increment may run for %ss inside a %ss UserPromptSubmit hook" % (budget, hook))

    def test_the_session_end_hook_allows_a_real_curate_to_finish(self):
        # A live curate was measured at 21.9s. A budget under that would make
        # the timeout, not the work, decide the outcome on every run.
        self.assertGreater(worker_budget("CODEX_DISTILL_TIMEOUT_CURATE"), 22)


class DistillGoesThroughTheDetachingWrapperTest(unittest.TestCase):
    """Both call sites must use `run_distill`, never the worker directly.

    `run_distill` is what decides synchronous (worker session, which must
    capture before its process disappears) versus detached (interactive, which
    has no such race and should not make the user wait). A call site that
    reaches past it silently reintroduces the blocking path.
    """

    def setUp(self):
        self.source = PREFLIGHT.read_text(encoding="utf-8")

    def test_run_distill_exists_and_branches_on_worker_session(self):
        self.assertIn("run_distill() {", self.source)
        wrapper = self.source.split("run_distill() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("is_worker_session", wrapper)
        self.assertIn("setsid", wrapper)

    def test_no_hook_driven_call_site_invokes_the_worker_directly(self):
        # `distill-propose` is exempt on purpose: the user types it in a
        # terminal and waits for its output, so it has no hook clock over it
        # and must stay synchronous. Every other call site is reached from a
        # hook and must go through the wrapper.
        wrapper_body = self.source.split("run_distill() {", 1)[1].split("\n}", 1)[0]
        outside = self.source.replace(wrapper_body, "")
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
        # If `distill-propose` is ever renamed or removed, the exemption above
        # silently starts covering nothing — or worse, a real call site.
        self.assertIn("\n  distill-propose)\n", self.source)


if __name__ == "__main__":
    unittest.main()
