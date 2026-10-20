#!/usr/bin/env python3
import sys
import unittest
import os
import json
import re
from collections import Counter
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

    def test_codex_stateful_commands_messages_envelopes_and_prompts(self):
        state = {}
        rows = [json.dumps({"type":"item.started", "item":{"id":"c1", "type":"command_execution", "command":"echo hi"}}),
                json.dumps({"type":"item.completed", "item":{"id":"c1", "type":"command_execution", "command":"echo hi", "aggregated_output":"hi", "exit_code":0}}),
                json.dumps({"type":"item.started", "item":{"id":"c2", "type":"command_execution", "command":"still running"}}),
                json.dumps({"type":"item.completed", "item":{"id":"m1", "type":"agent_message", "text":"said once"}}),
                json.dumps({"type":"item.completed", "item":{"id":"m1", "type":"agent_message", "text":"said once"}})]
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._format_present("codex", rows, state)
        displayed = output.getvalue()
        self.assertEqual(displayed.count("echo hi"), 1)
        self.assertEqual(displayed.count("still running"), 1)
        self.assertEqual(displayed.count("said once"), 1)
        self.assertIn("hi", displayed)
        for typ in ("dispatch.supervisor.turn-started", "supervisor.turn.started",
                    "dispatch.supervisor.session-announced", "dispatch.supervisor.parked",
                    "dispatch.supervisor.resumed", "dispatch.supervisor.join", "dispatch.supervisor.terminal",
                    "dispatch.supervisor.future-event"):
            result = view.format_record("codex", {"type": typ, "private":"payload"})
            self.assertEqual(len(result), 1)
            self.assertNotIn("payload", result[0].text)
            self.assertEqual(result[0].kind, "supervisor")
        self.assertEqual(view.format_record("codex", {"type":"dispatch.supervisor.token_usage", "x":1}), [])
        for content in ("user prompt", [{"type":"text", "text":"user prompt"}]):
            self.assertTrue(any(row.kind == "prompt" for row in view.format_record(
                "claude", {"type":"user", "message":{"content":content}})))

    def test_public_read_new_reuses_cursor_incrementally(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "att.log"
            path.write_text('{"type":"first"}\n')
            source, cursor = view.Source(path, "codex"), view.Cursor(path)
            first, cursor = view.read_new(source, cursor)
            self.assertEqual(first, ['{"type":"first"}'])
            with path.open("a") as stream: stream.write('{"type":"second"}\n')
            second, cursor = view.read_new(source, cursor)
            self.assertEqual(second, ['{"type":"second"}'])

    def test_registry_reader_substring_filters_and_throttles_changed_snapshot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jobs.log"
            path.write_text("attempt_id=one\nattempt_id=two\n")
            reader = view.RegistryReader(path)
            self.assertEqual(reader.read("two", now=2), ["attempt_id=two"])
            path.write_text("attempt_id=one\nattempt_id=two\nattempt_id=three\n")
            # A detected change inside the one-second budget keeps the last snapshot.
            self.assertEqual(reader.read("three", now=2.5), [])
            self.assertEqual(reader.read("three", now=3.1), ["attempt_id=three"])

    def test_registry_cli_selection_validation_and_read_only(self):
        import tempfile, contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"
            logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-sample"
            log = logs / f"x.{attempt}.codex.jsonl"
            log.write_text('{"type":"item.completed","item":{"id":"m","type":"agent_message","text":"hello"}}\n')
            registry = root / "jobs.log"
            def row(identifier, path, status="done", extra=""):
                return f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={identifier},harness=codex,worker_type=owner,log_file={path}{extra}\n"
            registry.write_text(row(attempt, log))
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)):
                snapshot = {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()}
                out = io.StringIO()
                with contextlib.redirect_stdout(out): self.assertEqual(view.main([attempt]), 0)
                self.assertIn("hello", out.getvalue())
                self.assertEqual(snapshot, {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()})
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main(["att-absent"]), 2)
                self.assertIn("Unknown attempt", err.getvalue())
                # Exact path carrying a registered attempt ID but different cwd/path is refused.
                mismatched = Path(folder) / f"other.att-{attempt}.codex.jsonl"
                mismatched.write_text("{}\n")
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main([str(mismatched)]), 2)
                self.assertIn("does not match", err.getvalue())
                registry.write_text(row("att-sample-one", log) + row("att-sample-two", log))
                view._REGISTRY_READERS.clear()
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main(["att-sample"]), 2)
                self.assertIn("Ambiguous", err.getvalue())
                registry.write_text(row(attempt, log))
                view._REGISTRY_READERS.clear()
                # Exact registry-bound log path is accepted; a same-name symlink elsewhere is refused.
                out = io.StringIO()
                with contextlib.redirect_stdout(out): self.assertEqual(view.main([str(log)]), 0)
                outside = Path(folder) / log.name; outside.symlink_to(log)
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main([str(outside)]), 2)
                self.assertIn("does not match", err.getvalue())

    def test_cleaned_native_source_leaves_supervisor_record_notice(self):
        import tempfile, contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-cleaned"
            log = logs / f"x.{attempt}.claude.jsonl"
            log.write_text(json.dumps({"type":"dispatch.supervisor.session-announced", "session_id":"sid"})+"\n")
            (root / "jobs.log").write_text(
                f"2026\tdone\trepo\t/tmp\towner\tattempt_id={attempt},harness=claude,worker_type=owner,log_file={log}\n")
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), contextlib.redirect_stdout(output):
                self.assertEqual(view.main([attempt]), 0)
            self.assertIn("감독 기록만 남음", output.getvalue())

    def test_announced_source_deduplicates_sid_and_resolves_multiple_sessions(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); log = root / "owner.jsonl"
            log.write_text("\n".join(json.dumps({"type":"dispatch.supervisor.session-announced",
                "parent_attempt_id":"att-owner", "session_id":sid, "cwd":"/work"})
                for sid in ("sid-one", "sid-one", "sid-two")) + "\n")
            self.assertEqual(view._announced(str(log), "att-owner"),
                             [("sid-one", "/work"), ("sid-two", "/work")])
            paths = {sid: root / f"{sid}.jsonl" for sid in ("sid-one", "sid-two")}
            for path in paths.values(): path.touch()
            row = (root, ["2026", "done", "repo", "/work", "owner", ""],
                   {"attempt_id":"att-owner", "harness":"claude", "log_file":str(log),
                    "runtime_home":str(root), "cwd":"/work"})
            with mock.patch.object(view, "_find_attempt", return_value=row), \
                 mock.patch.object(view, "_log_path", return_value=str(log)), \
                 mock.patch("claude_project_dir.resolve_session_transcript",
                            side_effect=lambda sid, cwd, **kw: (paths[sid], None)) as resolver:
                sources, error = view.resolve_sources("att-owner")
            self.assertIsNone(error)
            self.assertEqual([s.session_id for s in sources[1:]], ["sid-one", "sid-two"])
            self.assertTrue(all(call.kwargs["expected_cwd"] == "/work" for call in resolver.call_args_list))

    def test_follow_owner_lines_children_final_drain_and_single_end_marker(self):
        import tempfile, contextlib, io, time
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            owner_id, child_id = "att-owner", "att-child"
            owner = logs / f"x.{owner_id}.codex.jsonl"
            child = logs / f"x.{child_id}.codex.jsonl"
            owner.write_text('{"type":"session-start"}\n')
            child.write_text('{"type":"item.completed","item":{"id":"c","type":"agent_message","text":"child tail"}}\n')
            registry = root / "jobs.log"
            def line(attempt, status, path, extra=""):
                return f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={attempt},harness=codex,worker_type={'owner' if attempt == owner_id else 'stage'},log_file={path}{extra}\n"
            def save(status):
                registry.write_text(line(owner_id, status, owner) +
                    line(child_id, "done", child, f",parent_attempt_id={owner_id},route_node=execute"))
            save("open")
            sleeps = []
            def tick(_):
                if sleeps: return
                sleeps.append(1)
                with owner.open("a") as stream:
                    stream.write(json.dumps({"type":"item.completed", "item":{"id":"m","type":"agent_message","text":"owner follow line"}})+"\n")
                    stream.write('{"type":"result","subtype":"success"}\n')
                save("done")
                time.sleep(1.01) # permit the changed registry snapshot's one-second refresh budget
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), \
                 contextlib.redirect_stdout(output):
                result = view.main([owner_id, "--follow", "--poll-interval", "0"] , clock=lambda:0, sleep=tick)
            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("owner follow line", text)
            self.assertIn("[execute]", text)
            self.assertIn("child tail", text)
            self.assertEqual(text.count("[끝]"), 1)
            self.assertNotIn("종료 기록 없음", text)

    def test_follow_grace_marks_missing_terminal_but_turn_completed_is_not_terminal(self):
        import tempfile, contextlib, io, time
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-grace"
            owner = logs / f"x.{attempt}.codex.jsonl"
            owner.write_text('{"type":"turn.completed"}\n')
            registry = root / "jobs.log"
            def save(status):
                registry.write_text(f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={attempt},harness=codex,worker_type=owner,log_file={owner}\n")
            save("open")
            elapsed, closed = [0.0], [False]
            def tick(_):
                if not closed[0]:
                    save("done"); closed[0] = True
                elapsed[0] += 1.1
                time.sleep(1.01)
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), contextlib.redirect_stdout(output):
                self.assertEqual(view.main([attempt, "--follow", "--grace", "1", "--poll-interval", "0"],
                                           clock=lambda:elapsed[0], sleep=tick), 0)
            self.assertEqual(output.getvalue().count("[끝]"), 1)
            self.assertIn("종료 기록 없음", output.getvalue())

    def test_supervisor_unknown_inventory_counts_type_names(self):
        types = Counter()
        for name in ("dispatch.supervisor.future-a", "dispatch.supervisor.future-a",
                     "dispatch.supervisor.future-b"):
            lines = view.format_record("codex", {"type":name, "payload":"hidden from short line"})
            types[name] += len(lines)
        self.assertEqual(types.most_common(1), [("dispatch.supervisor.future-a", 2)])


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
        unknown_types=Counter()
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
                                if line.kind == "unknown":
                                    unknown+=1
                                    name = (record.get("type") or record.get("kind") or record.get("method") or "unknown") if isinstance(record, dict) else "non-object"
                                    unknown_types[str(name)] += 1
                        except Exception:
                            formatter_errors+=1
                            print(f"formatter: exception: {path}:{number}")
            except OSError:
                unreadable.append(path)
                print(f"unreadable: disappeared or unreadable path: {path}:1")
        print(f"LocalSamples files={len(files)} lines={total} non_json={non_json} unknown={unknown} "
              f"unreadable={len(unreadable)} formatter_exceptions={formatter_errors} sanitizer_findings={unsafe}")
        print("LocalSamples top_unknown=" + ", ".join(f"{name}:{count}" for name, count in unknown_types.most_common(10)))
        self.assertGreater(len(files), 0)
        self.assertEqual(formatter_errors, 0)
        self.assertEqual(unsafe, 0)


if __name__ == "__main__":
    unittest.main()
