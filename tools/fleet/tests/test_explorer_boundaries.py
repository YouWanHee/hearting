import unittest
from tools.fleet.explorer.focus import SelectionReader
from tools.fleet.explorer.types import FocusKey, FocusRequest

class SourceBoundaryTests(unittest.TestCase):
    def test_unexpanded_focus_is_passed_without_hidden_session_reads(self):
        seen = []
        reader = SelectionReader(lambda request: seen.append(request.expanded_sessions) or {})
        reader.read(FocusRequest(FocusKey("claude", "s"), 1, 1, ()))
        self.assertEqual(seen, [()])
