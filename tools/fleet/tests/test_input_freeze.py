"""Nothing the terminal sends may hold the frame loop.

The dashboard froze twice from terminal input, in two different terminals: under herdr
(2026-07-01) and in a VS Code terminal (2026-09-09). The second was measured while stuck —
0 CPU, zero read and write syscalls, one thread, sleeping on the tty with the terminal's
output queue empty — and any keypress advanced it exactly one frame, which is what a
half-assembled mouse or focus escape sequence does.

The freeze needs a real terminal and a real click, so it is not reproducible here. What is
testable is the configuration that prevents it, which is why it lives in its own function.
"""

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import render  # noqa: E402


class FakeCurses:
    """Records what `_configure_input` asks the terminal to do."""

    BUTTON1_CLICKED = 0x4

    def __init__(self, *, escdelay_raises=False, mouse_raises=False):
        self.calls = []
        self._escdelay_raises = escdelay_raises
        self._mouse_raises = mouse_raises

    def set_escdelay(self, ms):
        self.calls.append(("set_escdelay", ms))
        if self._escdelay_raises:
            raise Exception("terminal refused")

    def mousemask(self, mask):
        self.calls.append(("mousemask", mask))
        if self._mouse_raises:
            raise Exception("terminal refused")

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class OlderCurses:
    """A curses build predating `set_escdelay` (older Python / ncurses).

    Written out rather than subclassed: `hasattr` has to report False, and removing an
    inherited method from one instance is not something a subclass can do cleanly.
    """

    BUTTON1_CLICKED = FakeCurses.BUTTON1_CLICKED

    def __init__(self):
        self.calls = []

    def mousemask(self, mask):
        self.calls.append(("mousemask", mask))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class InputConfigurationTest(unittest.TestCase):
    def test_mouse_reporting_is_off_by_default(self):
        fake = FakeCurses()
        render._configure_input(fake, {})
        self.assertEqual(fake.named("mousemask"), [],
                         "mouse reporting is what froze the loop; it must not be default")

    def test_mouse_reporting_is_opt_in(self):
        fake = FakeCurses()
        render._configure_input(fake, {"FLEET_MOUSE": "1"})
        self.assertEqual(fake.named("mousemask"), [("mousemask", FakeCurses.BUTTON1_CLICKED)])

    def test_only_an_exact_1_opts_in(self):
        # A leftover "0"/"true"/"" must not silently turn the freeze back on.
        for value in ("0", "", "true", "yes", "2", " 1"):
            with self.subTest(value=value):
                fake = FakeCurses()
                render._configure_input(fake, {"FLEET_MOUSE": value})
                self.assertEqual(fake.named("mousemask"), [])

    def test_escape_delay_is_shortened_so_a_stray_esc_costs_one_frame(self):
        fake = FakeCurses()
        render._configure_input(fake, {})
        calls = fake.named("set_escdelay")
        self.assertEqual(len(calls), 1)
        # The frame cadence is ~100ms; the delay has to sit well under it.
        self.assertLessEqual(calls[0][1], 50)
        self.assertGreater(calls[0][1], 0)

    def test_a_terminal_that_refuses_either_setting_does_not_crash_the_tui(self):
        # These run before the first frame; an exception here would take the whole
        # dashboard down over a cosmetic setting.
        render._configure_input(FakeCurses(escdelay_raises=True), {})
        render._configure_input(FakeCurses(mouse_raises=True), {"FLEET_MOUSE": "1"})
        render._configure_input(OlderCurses(), {})

    def test_an_older_curses_without_set_escdelay_still_configures_the_mouse(self):
        fake = OlderCurses()
        render._configure_input(fake, {"FLEET_MOUSE": "1"})
        self.assertEqual(fake.named("set_escdelay"), [])
        self.assertEqual(len(fake.named("mousemask")), 1)


class StallDumpTest(unittest.TestCase):
    """`kill -USR1 <fleet pid>` must be able to answer "where is it stuck?".

    This host sets `ptrace_scope=1`, so strace and gdb cannot attach to a process that is
    not their own child. Both freezes were diagnosed by elimination because fleet could not
    answer for itself.
    """

    def test_the_entry_point_arms_a_stack_dump(self):
        import importlib
        fleet = importlib.import_module("fleet.fleet")
        self.assertTrue(hasattr(fleet, "_arm_stall_dump"))

    def test_arming_writes_a_dump_the_operator_can_read(self):
        import faulthandler
        import os
        import signal
        import tempfile
        import importlib
        fleet = importlib.import_module("fleet.fleet")
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("platform has no SIGUSR1")
        previous = faulthandler.is_enabled()
        fleet._arm_stall_dump()
        path = os.path.join(tempfile.gettempdir(), "fleet-stall-%d.txt" % os.getpid())
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            self.assertTrue(os.path.exists(path), "no dump file was written")
            dump = Path(path).read_text(encoding="utf-8", errors="replace")
            self.assertIn("most recent call first", dump)
            self.assertIn("test_input_freeze.py", dump)
        finally:
            faulthandler.unregister(signal.SIGUSR1)
            if not previous:
                faulthandler.disable()


if __name__ == "__main__":
    unittest.main()
