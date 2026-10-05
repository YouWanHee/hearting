#!/usr/bin/env python3
"""OpenCode context window and summary subtitle — the fields that never rendered.

Four independent producer defects kept every opencode row blank where claude/codex rows
were populated. Each class below pins one of them against the real on-disk shapes:

  1. ``opencode run --format json`` wraps every event in a singular ``part`` envelope the
     transcript text extractor did not descend into, so a dispatch attempt yielded zero
     characters and the refresher wrote an empty title and no summary at all.
  2. The SQLite refresh cursor anchored on the ``message`` table, which holds only
     per-message metadata. Conversational text lives in ``part``.
  3. ``collectors/dispatch.py`` parsed context telemetry for claude and codex only, so an
     opencode job row had no ctx% even though every ``step_finish`` carries its tokens.
  4. The models.dev window lookup ignored ``providerID`` and took the maximum across every
     provider publishing that model id.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import refresh_title as rt                       # noqa: E402
from fleet.collectors import dispatch                       # noqa: E402
from fleet.collectors import opencode                       # noqa: E402
from fleet.model import DispatchJob                         # noqa: E402


def _envelope(event_type, part):
    """One line of `opencode run --format json` output."""
    return {"type": event_type, "timestamp": 1786081697040,
            "sessionID": "ses_test", "part": part}


_TEXT_LINE = _envelope("text", {
    "id": "prt_1", "messageID": "msg_1", "sessionID": "ses_test", "type": "text",
    "text": "Reading the precedent files before writing the parser."})
_TOOL_LINE = _envelope("tool_use", {
    "id": "prt_2", "type": "tool", "callID": "call_1",
    "state": {"status": "completed", "output": "SECRET TOOL OUTPUT"}})
_STEP_LINE = _envelope("step_finish", {
    "id": "prt_3", "type": "step-finish", "reason": "tool-calls",
    "tokens": {"total": 19691, "input": 1611, "output": 160, "reasoning": 40,
               "cache": {"write": 300, "read": 17920}},
    "cost": 0.0076186})


class TranscriptTextTest(unittest.TestCase):
    """Defect 1 — the `part` envelope."""

    def test_part_envelope_yields_assistant_text(self):
        raw = "\n".join(json.dumps(line) for line in
                        (_STEP_LINE, _TEXT_LINE, _TOOL_LINE)) + "\n"
        text = rt._delta_text(raw, harness="opencode")
        self.assertIn("Reading the precedent files", text)

    def test_tool_and_step_envelopes_contribute_nothing(self):
        raw = "\n".join(json.dumps(line) for line in (_TOOL_LINE, _STEP_LINE)) + "\n"
        self.assertEqual(rt._delta_text(raw, harness="opencode"), "")

    def test_tool_output_never_leaks_into_the_summary_source(self):
        raw = json.dumps(_TOOL_LINE) + "\n"
        self.assertNotIn("SECRET", rt._delta_text(raw, harness="opencode"))


class MessageTableTest(unittest.TestCase):
    """Defect 2 — the refresh cursor must anchor on the table that holds text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "opencode.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
        con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT)")
        # Real shapes: `message` rows are metadata only; the text lives in `part`.
        con.execute("INSERT INTO message VALUES (?,?,?)", (
            "msg_1", "ses_test",
            json.dumps({"role": "assistant", "modelID": "glm-5.2",
                        "providerID": "opencode-go", "cost": 0.02,
                        "tokens": {"input": 1611, "cache": {"read": 17920, "write": 0}}})))
        con.execute("INSERT INTO part VALUES (?,?,?,?)", (
            "prt_1", "msg_1", "ses_test",
            json.dumps({"type": "text", "text": "Now writing the artifact."})))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_part_table_is_selected(self):
        self.assertEqual(rt.opencode_message_table(self.db), "part")

    def test_delta_reads_conversational_text(self):
        table = rt.opencode_message_table(self.db)
        text, cursor, _ = rt.read_opencode_delta(self.db, "ses_test", 0, table=table)
        self.assertIn("Now writing the artifact.", text)
        self.assertGreater(cursor, 0)

    def test_collector_and_refresher_agree_on_table_order(self):
        # A cursor written under one module's order must stay readable by the other.
        self.assertEqual(tuple(opencode._MESSAGE_TABLES), tuple(rt.OPENCODE_MESSAGE_TABLES))


class _Registry:
    """models.dev cache with one model id published at two different windows."""

    PAYLOAD = {
        "opencode-go": {"id": "opencode-go", "models": {
            "glm-5.2": {"limit": {"context": 1000000, "output": 131072}}}},
        "nano-gpt": {"id": "nano-gpt", "models": {
            "TEE/glm-5.2": {"limit": {"context": 1048576}}}},
    }

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "models.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.PAYLOAD, handle)
        self.saved = os.environ.get("OPENCODE_MODELS")
        os.environ["OPENCODE_MODELS"] = path
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("OPENCODE_MODELS", None)
        else:
            os.environ["OPENCODE_MODELS"] = self.saved
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        self.tmp.cleanup()


class ContextWindowTest(unittest.TestCase):
    """Defect 4 — providerID decides the window, not the cross-provider maximum."""

    def test_provider_scoped_window_wins(self):
        with _Registry():
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "opencode-go"), 1000000)
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "nano-gpt"), 1048576)

    def test_unknown_provider_falls_back_to_the_widest_window(self):
        # Over-large window understates ctx% — the safer direction to be wrong.
        with _Registry():
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "no-such"), 1048576)
            self.assertEqual(opencode._model_ctx_limit("glm-5.2"), 1048576)

    def test_missing_registry_reports_no_window(self):
        saved = os.environ.get("OPENCODE_MODELS")
        os.environ["OPENCODE_MODELS"] = "/nonexistent/models.json"
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        try:
            self.assertIsNone(opencode._model_ctx_limit("glm-5.2", "opencode-go"))
        finally:
            if saved is None:
                os.environ.pop("OPENCODE_MODELS", None)
            else:
                os.environ["OPENCODE_MODELS"] = saved
            opencode._REG.update(ts=0.0, map=None, by_provider=None)


class DispatchTelemetryTest(unittest.TestCase):
    """Defect 3 — an opencode job row must carry its own ctx%."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.saved_home = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = self.tmp.name
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self.saved_home
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()
        self.tmp.cleanup()

    def _job(self, lines, attempt="att-1", harness="opencode"):
        path = os.path.join(self.logs, "slug.%s.%s.jsonl" % (attempt, harness))
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        job = DispatchJob(key="code", slug="slug")
        job.harness = harness
        job.attempt_id = attempt
        job.model = "opencode-go/glm-5.2"
        job._log_file = path
        return job

    def test_last_step_finish_sets_ctx_pct(self):
        early = json.loads(json.dumps(_STEP_LINE))
        early["part"]["tokens"] = {"input": 100, "cache": {"read": 0, "write": 0}}
        job = self._job([early, _TEXT_LINE, _STEP_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        # last step wins: 1611 input + 17920 cache.read + 300 cache.write
        self.assertEqual(job.active_context_tokens, 19831)
        self.assertEqual(job.context_window_tokens, 1000000)
        self.assertEqual(job.ctx_pct, 2)
        self.assertEqual(job._runtime_session_id, "ses_test")

    def test_output_and_reasoning_are_excluded_from_context(self):
        job = self._job([_STEP_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertNotIn(job.active_context_tokens, (19691, 19991))

    def test_transcript_without_tokens_reports_no_context(self):
        job = self._job([_TEXT_LINE, _TOOL_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertIsNone(job.ctx_pct)

    def test_mixed_session_ids_refuse_to_guess(self):
        other = json.loads(json.dumps(_STEP_LINE))
        other["sessionID"] = "ses_other"
        job = self._job([_STEP_LINE, other])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertEqual(job.association_ambiguity, "multiple-stream-session-ids")
        self.assertIsNone(job.ctx_pct)

    def test_foreign_attempt_log_is_refused(self):
        job = self._job([_STEP_LINE], attempt="att-1")
        job.attempt_id = "att-2"           # log basename no longer binds this attempt
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertIsNone(job.ctx_pct)

    def test_attempt_summary_sidecar_is_readable_for_opencode(self):
        # The sidecar reader shares `_owned_attempt_log_path`; before the fix it failed
        # closed on harness, so an opencode job could never show a subtitle.
        job = self._job([_TEXT_LINE])
        self.assertIsNotNone(dispatch._owned_attempt_log_path(job))


class SessionBindingTest(unittest.TestCase):
    """pid↔session binding when several opencode processes share one directory, and when
    one process opens more than one session (measured 2026-09-29: three panes on the
    primary checkout; one BC_ResNet pane that opened four sessions, whose live
    OPENCODE_SESSION_ID was the fourth)."""

    _DIR = "/repo"

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE session (id TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL, "
            "tokens_input INT, tokens_output INT, tokens_reasoning INT, time_updated INT, "
            "parent_id TEXT, directory TEXT, time_created INT)")

    def _add(self, sid, created, updated, parent=None, directory=_DIR):
        self.con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, sid, None, None, None, None, None, None, updated, parent, directory, created))

    def _bound(self, start, until=None):
        row = opencode._query(self.con.cursor(), self._DIR, start, until)
        return row[0] if row else None

    def test_same_directory_processes_bind_their_own_sessions(self):
        self._add("a", created=110, updated=900)
        self._add("b", created=210, updated=950)
        self._add("c", created=310, updated=990)
        self.assertEqual(self._bound(100, 200), "a")
        self.assertEqual(self._bound(200, 300), "b")
        self.assertEqual(self._bound(300), "c")

    def test_a_process_follows_the_session_it_opened_last(self):
        for sid, created, updated in (("first", 110, 120), ("second", 130, 140),
                                      ("current", 150, 999)):
            self._add(sid, created, updated)
        self.assertEqual(self._bound(100), "current")

    def test_child_sessions_never_bind(self):
        self._add("top", created=110, updated=500)
        self._add("sub", created=120, updated=999, parent="top")
        self.assertEqual(self._bound(100), "top")

    def test_attached_process_keeps_the_directory_fallback(self):
        self._add("old", created=10, updated=999)
        self.assertEqual(self._bound(100), "old")

    def test_prepare_tick_bounds_each_process_by_the_next_same_directory_start(self):
        class _S:
            def __init__(self, pid, cwd, harness="opencode"):
                self.pid, self.cwd, self.harness = pid, cwd, harness
        starts = {1: 300, 2: 100, 3: 200, 4: 50, 5: 60}
        sessions = [_S(1, "/r"), _S(2, "/r"), _S(3, "/r"), _S(4, "/other"),
                    _S(5, "/r", harness="codex")]
        orig = opencode._process_started_ms
        opencode._process_started_ms = lambda sess: starts[sess.pid]
        try:
            until = opencode.prepare_tick(sessions)
        finally:
            opencode._process_started_ms = orig
        self.assertEqual(until, {2: 200, 3: 300, 1: None, 4: None})


class DuplicateSidCollapseTest(unittest.TestCase):
    """Issue #158 — a session leader plus its arg-less helper child bound the
    same sid through the directory fallback and rendered as two identical
    working rows. One session id must keep exactly one row; the loser returns
    to anonymous-process state but its row stays (existence is the backbone's
    decision, PRD §1)."""

    def _row(self, pid, sid, kind=None, started=None, harness="opencode",
             cwd="/repo", title="T", tag="[83]"):
        from types import SimpleNamespace
        return SimpleNamespace(
            pid=pid, harness=harness, cwd=cwd, session_id=sid,
            _opencode_bind_kind=kind,
            proc_start=str(started) if started is not None else None,
            session_tag=tag, slug="db-slug", title=title, summary="S",
            summary_ts=1.0, subagents=[], model="m", effort="high",
            effort_default=False, cost=1.0, tokens=10,
            session_input_tokens=1, session_output_tokens=2,
            session_reasoning_output_tokens=3, session_total_tokens=6,
            active_context_tokens=5, context_window_tokens=100, ctx_pct=5,
            _context_evidence="e", _refresh_source={"session_id": sid},
            mtime=999.0)

    def test_leader_keeps_sid_and_helper_is_unbound(self):
        leader = self._row(100, "ses_1", kind="window", started=1000)
        helper = self._row(200, "ses_1", kind="fallback", started=2000)
        opencode.collapse_duplicate_sids([leader, helper])
        self.assertEqual(leader.session_id, "ses_1")
        self.assertEqual(leader.title, "T")
        self.assertIsNone(helper.session_id)
        self.assertIsNone(helper.session_tag)
        self.assertIsNone(helper.title)
        self.assertIsNone(helper.summary)
        self.assertIsNone(helper.model)
        self.assertIsNone(helper.ctx_pct)
        self.assertIsNone(helper._refresh_source)
        # the process row itself stays, with process-owned fields intact
        self.assertEqual(helper.pid, 200)
        self.assertEqual(helper.cwd, "/repo")
        self.assertEqual(helper.slug, "repo")
        self.assertEqual(helper.mtime, 999.0)

    def test_window_beats_fallback_despite_pid_order(self):
        # The fallback row enriches first (lower pid) but must not steal the
        # session from the process that created it through its window.
        fallback = self._row(50, "ses_1", kind="fallback", started=1000)
        owner = self._row(200, "ses_1", kind="window", started=2000)
        opencode.collapse_duplicate_sids([fallback, owner])
        self.assertEqual(owner.session_id, "ses_1")
        self.assertIsNone(fallback.session_id)

    def test_two_fallbacks_keep_the_earliest_start(self):
        old = self._row(300, "ses_1", kind="fallback", started=1000)
        new = self._row(100, "ses_1", kind="fallback", started=2000)
        opencode.collapse_duplicate_sids([old, new])
        self.assertEqual(old.session_id, "ses_1")
        self.assertIsNone(new.session_id)

    def test_distinct_sids_and_other_harnesses_untouched(self):
        a = self._row(100, "ses_1", kind="window", started=1000)
        b = self._row(200, "ses_2", kind="fallback", started=2000)
        c = self._row(300, "ses_1", kind="fallback", started=3000,
                      harness="codex")
        d = self._row(400, None, kind="fallback", started=4000)
        opencode.collapse_duplicate_sids([a, b, c, d])
        self.assertEqual((a.session_id, b.session_id, c.session_id, d.session_id),
                         ("ses_1", "ses_2", "ses_1", None))
        self.assertEqual(c.title, "T")


if __name__ == "__main__":
    unittest.main()
