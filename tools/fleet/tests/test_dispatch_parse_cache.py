"""Exact-file reuse and unchanged snapshots when terminal retention runs early."""
import builtins
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import model, render
from fleet.collectors import dispatch, parse_cache, procscan


class ParseCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.path = self.root / "attempt.jsonl"
        self.path.write_text(json.dumps({"type": "thread.started", "thread_id": "thread-a"}) + "\n")
        dispatch._CODEX_ATTEMPT_CACHE.clear()

    def test_second_process_cache_does_not_read_source(self):
        cold = dispatch._parse_codex_attempt_tail(str(self.path))
        dispatch._CODEX_ATTEMPT_CACHE.clear()  # empty memory, as on next Fleet startup
        original = builtins.open
        def opened(path, *args, **kwargs):
            if str(path) == str(self.path):
                raise AssertionError("unchanged source read on disk-cache hit")
            return original(path, *args, **kwargs)
        with mock.patch.object(builtins, "open", side_effect=opened):
            self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)

    def test_append_truncate_replace_and_same_size_edit_invalidate(self):
        path = str(self.path)
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-a")
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "thread.started", "thread_id": "thread-b"}) + "\n")
        self.assertTrue(dispatch._parse_codex_attempt_tail(path)["thread_ambiguity"])
        self.path.write_text('{"type":"thread.started","thread_id":"thread-c"}\n')
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-c")
        stamp = self.path.stat()
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes().replace(b"thread-c", b"thread-d"))
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-d")
        stamp = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b"thread-d", b"thread-e"))
        os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        dispatch._CODEX_ATTEMPT_CACHE.clear()
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-e")
        self.assertEqual(len(list(parse_cache._directory().glob("*.json"))), 1)

    def test_corrupt_or_unwritable_cache_falls_back_to_source(self):
        cold = dispatch._parse_codex_attempt_tail(str(self.path))
        next(parse_cache._directory().glob("*.json")).write_text("broken JSON")
        dispatch._CODEX_ATTEMPT_CACHE.clear()
        self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)
        cache_block = self.root / "blocked"
        cache_block.write_text("file, not directory")
        with mock.patch.object(parse_cache, "_directory", return_value=cache_block / "cache"):
            dispatch._CODEX_ATTEMPT_CACHE.clear()
            self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)

    def test_changed_source_during_parse_is_not_published(self):
        stamp = self.path.stat()
        sig = (stamp.st_mtime_ns, stamp.st_size, stamp.st_dev, stamp.st_ino)
        key = parse_cache.key("test", str(self.path), sig, ())
        self.path.write_text("changed\n")
        parse_cache.save(key, {"thread_id": "old"}, str(self.path), sig)
        self.assertIsNone(parse_cache.load(key))

    def test_valid_json_with_damaged_value_reparses_for_every_harness(self):
        fixtures = (
            (dispatch._parse_codex_attempt_tail, dispatch._CODEX_ATTEMPT_CACHE,
             {"type": "thread.started", "thread_id": "thread-a"},
             ({"activity": {}}, {"token_usage": ["damaged"]}, {"exec_tool": []})),
            (dispatch._parse_claude_stream_tail, dispatch._CLAUDE_STREAM_CACHE,
             {"type": "system", "subtype": "init", "session_id": "session-a"},
             ({"session_id": []}, {"active_context_tokens": {}}, {"ambiguity": True})),
            (dispatch._parse_opencode_attempt_tail, dispatch._OPENCODE_ATTEMPT_CACHE,
             {"type": "step_finish", "sessionID": "session-a", "part": {"tokens": {"input": 100}}},
             ({"active_context_tokens": []}, {"session_id": {}}, {"ambiguity": 123})),
        )
        for index, (parse, memory, event, corruptions) in enumerate(fixtures):
            path = self.root / ("fixture-%d.jsonl" % index)
            path.write_text(json.dumps(event) + "\n")
            memory.clear()
            good = parse(str(path))
            for corruption in corruptions:
                # Find the cached record by its valid parser value, then damage only
                # that value: the source stamp/signature deliberately stays exact.
                cache = next(p for p in parse_cache._directory().glob("*.json")
                             if json.loads(p.read_text())["value"] == good)
                record = json.loads(cache.read_text())
                record["value"].update(corruption)
                cache.write_text(json.dumps(record))
                memory.clear()
                self.assertEqual(parse(str(path)), good, corruption)


class RetentionGoldenTest(unittest.TestCase):
    def test_dropped_owners_never_enrich_and_all_snapshots_match(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            registry = root / "jobs.log"
            rows = []
            for harness, event in (
                ("claude", {"type": "system", "subtype": "init", "session_id": "cl-live", "model": "claude-sonnet-4-6"}),
                ("codex", {"type": "thread.started", "thread_id": "cx-live"}),
                ("opencode", {"type": "step_finish", "sessionID": "oc-live", "part": {"tokens": {"input": 100}}}),
            ):
                for old in (True, False):
                    attempt = "att-" + harness + ("-old" if old else "-live")
                    log = logs / ("worker." + attempt + "." + harness + ".jsonl")
                    log.write_text(json.dumps(event) + "\n")
                    meta = ",".join(("attempt_id=" + attempt, "harness=" + harness,
                                     "dispatch_depth=1", "worker_type=owner" if old else "worker_type=worker",
                                     "note=dead-runtime-exit" if old else "note=",
                                     "log_file=" + str(log), "pid=" + str(os.getpid()),
                                     "pid_start=" + str(procscan.read_proc_start(os.getpid()))))
                    # Old terminal rows cannot claim a live PID; active rows keep its identity.
                    if old:
                        meta = meta.replace("pid=" + str(os.getpid()), "pid=99999991")
                    rows.append("%s\t%s\tfixture\t%s\t%s\t%s\n" % (
                        "2000-01-01T00:00:00Z" if old else "2099-01-01T00:00:00Z",
                        "done" if old else "open", root, attempt, meta))
            registry.write_text("".join(rows))
            before_bytes = registry.read_bytes()
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": tmp, "AGENT_HOME": tmp,
                "AGENT_DISPATCH_JOBS": str(registry), "XDG_CACHE_HOME": str(root / "cache")}, clear=True))
            stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
            stack.enter_context(mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[str(registry)]))
            stack.enter_context(mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()))
            stack.enter_context(mock.patch.object(dispatch, "_codex_attempt_rollout", return_value=None))
            stack.enter_context(mock.patch.object(dispatch, "_scan_registry_evidence", return_value=({}, {})))
            stack.enter_context(mock.patch("time.time", return_value=2_000_000_000.0))
            retain = dispatch._retain_dead_terminal_owners
            calls = []
            def late_retention(jobs, *args, **kwargs):
                calls.append(1)
                return jobs if len(calls) == 1 else retain(jobs, *args, **kwargs)
            # Replay the previous placement: all details, then the identical retention rule.
            with mock.patch.object(dispatch, "_retain_dead_terminal_owners", side_effect=late_retention):
                model.reset_state_tracker()
                baseline = dispatch.collect(jobs_path=str(registry))
            with mock.patch.object(dispatch, "_enrich_codex_attempt_session", wraps=dispatch._enrich_codex_attempt_session) as enrich:
                model.reset_state_tracker()
                optimized = dispatch.collect(jobs_path=str(registry))
            self.assertEqual(len(enrich.call_args_list), 3)
            self.assertTrue(all("-old" not in call.args[0].attempt_id for call in enrich.call_args_list))
            self.assertEqual([j.to_dict() for j in baseline], [j.to_dict() for j in optimized])
            self.assertEqual(registry.read_bytes(), before_bytes)
            for show_all in (False, True):
                render.set_show_all(show_all)
                for process_view in (False, True):
                    render.set_process_view(process_view)
                    for width in (80, 120, 180):
                        a = render._build_lines([], baseline, "both", width < 100, 0,
                                                term_width=width, governor=None)
                        b = render._build_lines([], optimized, "both", width < 100, 0,
                                                term_width=width, governor=None)
                        self.assertEqual(a, b, (show_all, process_view, width))
            render.set_show_all(False)
            render.set_process_view(False)


if __name__ == "__main__":
    unittest.main()
