#!/usr/bin/env python3
"""Exercise human history notices as a channel separate from model context."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools/install"))
import session_tidy

spec = importlib.util.spec_from_file_location(
    "codex_sessionstart", ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)


class HistoryChannelTests(unittest.TestCase):
    def test_person_notice_uses_confirmed_predecessor_and_omits_path(self):
        lineage = [{"harness": "claude", "sid": "new", "relation": "confirmed",
                    "predecessor": {"harness": "claude", "sid": "old"}},
                   {"harness": "claude", "sid": "old", "transcript": {
                       "path": "/private/raw/transcript", "status": "available"}}]
        seat = session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(session_tidy, "seat_lineage", return_value=lineage), \
             patch("herdr_integration.history_key_available", return_value=True), \
             patch("herdr_integration.history_key_label", return_value="Ctrl-b Shift-H"):
            notice = session_tidy.build_human_history_notice(
                seat, "claude", "new", env={"HERDR_ACTIVE_PANE_ID": "pane", "HERDR_SOCKET_PATH": "/socket"})
        self.assertIn("Ctrl-b Shift-H", notice)
        self.assertIn("원문 있음", notice)
        self.assertNotIn("/private/raw/transcript", notice)

    def test_unconfirmed_neighbor_does_not_make_notice(self):
        seat = session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(session_tidy, "seat_lineage", return_value=[
                {"harness": "claude", "sid": "new", "relation": "chronological"},
                {"harness": "claude", "sid": "neighbor", "relation": "chronological"}]):
            self.assertEqual(session_tidy.build_human_history_notice(seat, "claude", "new", env={}), "")

    def test_codex_places_notice_outside_additional_context(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            codex.emit_context("SessionStart", ["MODEL-CARD"], "PERSON-NOTICE")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "MODEL-CARD")
        self.assertEqual(payload["systemMessage"], "PERSON-NOTICE")

    def test_codex_card_bridge_parses_separate_fields(self):
        payload = {"additionalContext": "MODEL-CARD", "humanNotice": "PERSON-NOTICE"}
        result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(codex.subprocess, "run", return_value=result):
            card, human = codex.card_context({"session_id": "sid"}, "/fixture")
        self.assertEqual((card, human), ("MODEL-CARD", "PERSON-NOTICE"))


if __name__ == "__main__":
    unittest.main()
