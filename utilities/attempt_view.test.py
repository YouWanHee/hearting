#!/usr/bin/env python3
import sys
import unittest
import os
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attempt_view as view


class AttemptViewTests(unittest.TestCase):
    def test_sanitizes_terminal_controls_and_credentials(self):
        value = "a\x1b]52;c;secret\x07 https://name:pass@example.test Bearer xyz\x1b[31m!"
        clean = view.sanitize(value)
        self.assertNotIn("secret", clean)
        self.assertNotIn("name:pass", clean)
        self.assertNotIn("xyz", clean)
        self.assertNotIn("\x1b", clean)

    def test_unknown_and_raw_records_survive_as_one_line(self):
        self.assertEqual(len(view.format_record("codex", "not-json\nsecond")), 1)
        lines = view.format_record("claude", {"type": "new-event", "payload": "hello"})
        self.assertEqual(len(lines), 1)
        self.assertIn("new-event", lines[0].text)

    def test_codex_command_and_opencode_tool_keep_input_and_output(self):
        codex = view.format_record("codex", {"type": "command_execution", "command": "echo hi",
                                             "aggregated_output": "hi"})
        self.assertTrue(any("echo hi" in line.text for line in codex))
        tool = view.format_record("opencode", {"type": "tool", "name": "bash",
                                               "input": {"cmd": "pwd"}, "output": "here"})
        self.assertTrue(any("pwd" in line.text for line in tool))
        self.assertTrue(any("here" in line.text for line in tool))

    def test_fleet_import_is_not_part_of_viewer(self):
        self.assertFalse(any(name == "fleet" or name.startswith(("fleet.", "tools.fleet"))
                             for name in sys.modules))


@unittest.skipUnless(os.environ.get("HEARTING_ATTEMPT_VIEW_LOCAL") == "1",
                     "set HEARTING_ATTEMPT_VIEW_LOCAL=1 for local log checks")
class LocalSamples(unittest.TestCase):
    def test_all_local_dispatch_logs_format_without_stopping(self):
        files=set()
        roots=view._dispatch_roots()
        for root in roots:
            logs=root / "logs"
            if logs.is_dir(): files.update(p.resolve() for p in logs.rglob("*.jsonl") if p.is_file())
        total=non_json=unknown=errors=unsafe=0
        for path in sorted(files):
            harness=next((h for h in ("claude", "codex", "opencode") if path.name.endswith("."+h+".jsonl")), "unknown")
            try:
                with path.open("rb") as stream:
                    for raw in stream:
                        total+=1
                        text=raw.decode("utf-8", "replace").rstrip("\r\n")
                        try: record=json.loads(text)
                        except (ValueError, TypeError): record=text; non_json+=1
                        try:
                            for line in view.format_record(harness, record):
                                if "\x1b" in line.text or "\x9b" in line.text: unsafe+=1
                                if "Bearer [redacted]" not in line.text and "Bearer " in line.text: unsafe+=1
                                if "://" in line.text and "@" in line.text and re.search(r"https?://[^\s/@]+:[^\s/@]+@", line.text, re.I): unsafe+=1
                                if line.kind == "unknown": unknown+=1
                        except Exception:
                            errors+=1
            except OSError:
                errors+=1
        print(f"LocalSamples files={len(files)} lines={total} non_json={non_json} unknown={unknown} errors={errors} unsafe={unsafe}")
        self.assertGreater(len(files), 0)
        self.assertEqual(errors, 0)
        self.assertEqual(unsafe, 0)


if __name__ == "__main__":
    unittest.main()
