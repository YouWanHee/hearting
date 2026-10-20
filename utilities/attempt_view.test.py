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

    def test_claude_tool_result_shapes_and_empty_thinking(self):
        rows = view.format_record("claude", {"type":"assistant", "message":{"content":[
            {"type":"thinking", "thinking":""}, {"type":"thinking", "thinking":"reason"},
            {"type":"tool_use", "name":"read", "input":{"path":"a"}}]}})
        self.assertEqual(sum(line.kind == "thinking" for line in rows), 1)
        for content in ("plain", [{"type":"text", "text":"plain"}]):
            rows = view.format_record("claude", {"type":"user", "message":{"content":[
                {"type":"tool_result", "content":content}]}})
            self.assertTrue(any("plain" in line.text for line in rows))

    def test_codex_variants_and_malformed_envelopes(self):
        for record in ({"type":"command_execution", "command":"echo", "aggregated_output":"ok"},
                       {"type":"commandExecution", "command":"echo", "aggregatedOutput":"ok"},
                       {"type":"event_msg", "item":{"type":"commandExecution", "command":"echo", "aggregatedOutput":"ok"}}):
            rows = view.format_record("codex", record)
            self.assertTrue(any("echo" in line.text for line in rows))
        for value in ("not-json", "3", "[]", "{}", 7, ["x"]):
            self.assertTrue(view.format_record("claude", value))

    def test_control_sequence_and_secret_matrix(self):
        for value in ("\x1b]8;;https://a\x07link\x1b]8;;\x07", "\x1b]52;c;secret\x1b\\",
                      "\x1bPsecret\x1b\\", "\x9d52;c;secret\x9c", "\x90x\x9c",
                      "\x1b]unterminated", "\x1b[31unterminated", "\x00\r\x7f"):
            clean = view.sanitize(value)
            self.assertFalse(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", clean))
        for value in ("https://name:secret@example.test", "BEARER abc123", "Bearer \x1b[31msecret"):
            clean = view.sanitize(value)
            self.assertNotRegex(clean, r"https?://[^\s/@]+:[^\s/@]+@")
            self.assertNotRegex(clean, r"(?i)Bearer\s+(?!\[redacted\])[^\s]+")

    def test_full_presentation_still_sanitizes_and_preserves_assistant_prose(self):
        lines = view.format_record("claude", {"type":"assistant", "message":{"content":[
            {"type":"text", "text":"prose " + "x" * 5000 + " Bearer secret"}]}})
        self.assertTrue(any("x" * 5000 in line.text for line in lines))
        self.assertTrue(all("secret" not in line.text for line in lines))

    def test_cursor_buffers_split_utf8_and_notices_replacement(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log"
            path.write_bytes("한".encode()[:2])
            cursor = view.Cursor(path)
            self.assertEqual(cursor.read_new(), [])
            with path.open("ab") as stream: stream.write("한\n".encode()[2:])
            self.assertEqual(cursor.read_new(), ["한"])
            path.write_bytes(b"next\n")
            self.assertEqual(cursor.read_new(), ["next"])
            self.assertTrue(cursor.notices)

    def test_present_omission_counts_and_full_keeps_masking(self):
        import contextlib
        import io
        rows = [view.Line("output", "\n".join(str(i) for i in range(40))),
                view.Line("message", "assistant: " + "a" * 3000 + " Bearer token")]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured): view._present(rows, full=False)
        self.assertIn("(+16줄 생략", captured.getvalue())
        self.assertIn("a" * 3000, captured.getvalue())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured): view._present(rows, full=True)
        self.assertIn("39", captured.getvalue())
        self.assertNotIn("Bearer token", captured.getvalue())

    def test_session_selection_zero_one_many(self):
        from unittest import mock
        import contextlib
        import io
        def candidate(identifier):
            return (Path("/tmp"), ["2026", "open", "codex", "/tmp", "owner", ""],
                    {"attempt_id":identifier, "worker_type":"owner", "harness":"codex"})
        with mock.patch.object(view, "_owner_candidates", return_value=[]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(view.main(["--session", "sid"]), 0)
            self.assertIn("No owner attempts", out.getvalue())
        with mock.patch.object(view, "_owner_candidates", return_value=[candidate("att-one")]), \
             mock.patch.object(view, "_attempt_prefix_matches", return_value=[candidate("att-one")]), \
             mock.patch.object(view, "resolve_sources", return_value=([], "attempt not found")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err): self.assertEqual(view.main(["--session", "sid"]), 2)
            self.assertIn("attempt not found", err.getvalue())
        with mock.patch.object(view, "_owner_candidates", return_value=[candidate("att-one"), candidate("att-two")]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(view.main(["--session", "sid"]), 0)
            self.assertIn("att-one", out.getvalue())
            self.assertIn("att-two", out.getvalue())

    def test_opencode_v2_state_content_and_pending_refresh_deduplicate(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "opencode.db"
            sid = "ses_viewv2"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
                con.execute("INSERT INTO session_message VALUES (?, 'assistant', 1, ?)",
                            (sid, json.dumps({"content":[{"type":"tool", "name":"bash", "state":{
                                "status":"running", "input":{"cmd":"pwd"}, "content":[]}}]})))
            source = view.Source(db, "opencode", "opencode-db", sid)
            state = {"cursor":0,"refresh":[],"rows":{},"entities":{}}
            first = view._follow_db(source,state)
            self.assertTrue(any("pwd" in line.text for line in first))
            with sqlite3.connect(db) as con:
                con.execute("UPDATE session_message SET data=? WHERE seq=1", (json.dumps({"time":{"completed":1},"content":[
                    {"type":"tool", "name":"bash", "state":{"status":"completed", "input":{"cmd":"pwd"},
                     "content":[{"type":"text", "text":"done"}]}}]}),))
            second = view._follow_db(source,state)
            joined = "\n".join(line.text for line in second)
            self.assertIn("completed", joined)
            self.assertIn("done", joined)
            self.assertNotIn('cmd', joined)
            self.assertEqual(view._follow_db(source,state), [])


@unittest.skipUnless(os.environ.get("HEARTING_ATTEMPT_VIEW_LOCAL") == "1",
                     "set HEARTING_ATTEMPT_VIEW_LOCAL=1 for local log checks")
class LocalSamples(unittest.TestCase):
    def test_all_local_dispatch_logs_format_without_stopping(self):
        files=set()
        roots=view._dispatch_roots()
        for root in roots:
            logs=root / "logs"
            if logs.is_dir(): files.update(p.resolve() for p in logs.rglob("*.jsonl") if p.is_file())
            try: registry=(root / "jobs.log").read_text(errors="replace").splitlines()
            except OSError: registry=[]
            for raw in registry:
                parsed=view._metadata(raw)
                if parsed:
                    fields,meta=parsed
                    admitted=view._log_path(root,fields,meta)
                    if admitted: files.add(Path(admitted).resolve())
        total=non_json=unknown=formatter_errors=unsafe=0
        unreadable=[]
        for index,path in enumerate(sorted(files),1):
            if index == 1 or index % 100 == 0:
                print(f"LocalSamples progress {index}/{len(files)}: {path}", flush=True)
            harness=next((h for h in ("claude", "codex", "opencode") if path.name.endswith("."+h+".jsonl")), "unknown")
            try:
                with path.open("rb") as stream:
                    for number,raw in enumerate(stream,1):
                        total+=1
                        text=raw.decode("utf-8", "replace").rstrip("\r\n")
                        try: record=json.loads(text)
                        except (ValueError, TypeError): record=text; non_json+=1
                        try:
                            for line in view.format_record(harness, record):
                                reasons=[]
                                if re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", line.text) or "\x1b" in line.text:
                                    reasons.append("control-sequence")
                                if any(match.group(1) != "[redacted]" for match in
                                       re.finditer(r"(?i)\bBearer\s+([^\s]+)", line.text)):
                                    reasons.append("unmasked-bearer")
                                if re.search(r"https?://[^\s/@]+:[^\s/@]+@", line.text, re.I):
                                    reasons.append("url-credentials")
                                if reasons:
                                    unsafe+=len(reasons)
                                    print(f"sanitizer: {','.join(reasons)}: {path}:{number}")
                                if line.kind == "unknown": unknown+=1
                        except Exception:
                            formatter_errors+=1
                            print(f"formatter: exception: {path}:{number}")
            except OSError:
                unreadable.append(path)
                print(f"unreadable: disappeared or unreadable path: {path}:1")
        print(f"LocalSamples files={len(files)} lines={total} non_json={non_json} unknown={unknown} "
              f"unreadable={len(unreadable)} formatter_exceptions={formatter_errors} sanitizer_findings={unsafe}")
        self.assertGreater(len(files), 0)
        self.assertEqual(formatter_errors, 0)
        self.assertEqual(unsafe, 0)


if __name__ == "__main__":
    unittest.main()
