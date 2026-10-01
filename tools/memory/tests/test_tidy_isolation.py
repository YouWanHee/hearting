#!/usr/bin/env python3
"""The session-tidy test isolation helper, and the recorded-format fixtures it hosts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

import tidy_isolation  # noqa: E402
from tidy_isolation import IsolationError, isolated_env, scrub  # noqa: E402
import tidy_transcripts  # noqa: E402

FIXTURES = HERE / "fixtures"
FIXTURE_NOW = 1790730300               # 2026-09-30T01:05:00Z, inside the fixture's window


def load_opencode_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript((FIXTURES / "opencode-choice.sql").read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


class IsolationHelperTest(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)

    def test_root_is_under_var_tmp_and_never_tmp(self):
        self.assertTrue(str(self.iso.root).startswith("/var/tmp/"))
        self.assertFalse(str(self.iso.root).startswith("/tmp/"))

    def test_every_state_path_points_below_the_root(self):
        env = self.iso.env()
        for key in tidy_isolation.ISOLATED_PATH_KEYS:
            self.assertIn(key, env)
            self.assertTrue(Path(env[key]).resolve().is_relative_to(self.iso.root), key)
        self.assertNotIn(str(tidy_isolation.REAL_HOME), "".join(v for k, v in env.items() if k != "PATH"))

    def test_remote_exchange_is_disabled_in_the_isolated_config(self):
        setting = Path(self.iso.env()["XDG_CONFIG_HOME"]) / "hearting" / "memory-sync.json"
        self.assertEqual(json.loads(setting.read_text(encoding="utf-8")), {"enabled": False})

    def test_background_exchange_is_pinned_off_unless_a_test_turns_it_on_by_name(self):
        self.assertEqual(self.iso.env()["MEM_EXCHANGE_AUTO"], "0")
        self.assertEqual(self.iso.env({"MEM_EXCHANGE_AUTO": "inline"})["MEM_EXCHANGE_AUTO"], "inline")
        with self.assertRaises(IsolationError):       # an ambient value never gets through
            self.iso.assert_env(dict(self.iso.base_env(), MEM_EXCHANGE_AUTO="inline"))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(self.iso.base_env(), MEM_EXCHANGE_WINDOW_SECONDS="0"))
        self.assertNotIn("MEM_EXCHANGE_WORKER", scrub({"MEM_EXCHANGE_WORKER": "1", "HOME": "/x"}))

    def test_child_env_drops_remote_pane_worker_and_session_markers(self):
        dirty = {
            "MEM_SYNC_REMOTE": "git@example.invalid:x.git", "MEM_DUMP_PUSH": "1",
            "HERDR_PANE_ID": "wB:p1N", "HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x",
            "AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1", "AGENT_DISPATCH_DEPTH": "2",
            "OPENCODE_DISPATCH_SLUG": "s", "CLAUDE_CODE_SESSION_ID": "real",
            "CODEX_THREAD_ID": "real", "OPENCODE_SESSION_ID": "real", "KEEP_ME": "yes",
        }
        clean = scrub(dirty)
        self.assertEqual(clean, {"KEEP_ME": "yes"})
        # the helper itself never inherits them, even with a dirty parent environment
        saved = dict(os.environ)
        os.environ.update(dirty)
        try:
            env = self.iso.env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        for key in dirty:
            self.assertNotIn(key, env)

    def test_a_marker_can_be_passed_on_purpose_and_reaches_the_child(self):
        env = self.iso.env({"HERDR_PANE_ID": "test:pane-1", "AGENT_SESSION_ROLE": "worker"})
        self.assertEqual(env["HERDR_PANE_ID"], "test:pane-1")
        result = self.iso.run([sys.executable, "-c",
                               "import os;print(os.environ.get('HERDR_PANE_ID'),os.environ['HOME'])"],
                              extra={"HERDR_PANE_ID": "test:pane-1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["test:pane-1", str(self.iso.home)])

    def test_assertions_fail_when_a_real_path_would_remain(self):
        base = self.iso.base_env()
        real_store = dict(base, MEM_STORE=str(tidy_isolation.REAL_HOME / ".local/share/agent-memory"))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(real_store)
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(base, XDG_STATE_HOME=str(tidy_isolation.REAL_HOME / ".local/state")))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(base, MEM_SYNC_REMOTE="x"))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(base, HERDR_PANE_ID="wB:p1N"))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(base, AGENT_SESSION_ROLE="worker"))
        with self.assertRaises(IsolationError):
            self.iso.assert_env(dict(base, PATH=str(tidy_isolation.REAL_HOME / ".local/bin")))
        missing = dict(base)
        del missing["MEM_STORE"]
        with self.assertRaises(IsolationError):
            self.iso.assert_env(missing)
        with self.assertRaises(IsolationError):
            self.iso.env({"HOME": str(tidy_isolation.REAL_HOME)})

    def test_patched_environ_replaces_and_restores(self):
        before = dict(os.environ)
        with self.iso.patched_environ({"HERDR_PANE_ID": "test:p"}):
            self.assertEqual(os.environ["HOME"], str(self.iso.home))
            self.assertEqual(os.environ["HERDR_PANE_ID"], "test:p")
            self.assertNotIn("AGENT_SESSION_ROLE", os.environ)
        self.assertEqual(dict(os.environ), before)

    def test_cleanup_removes_the_root(self):
        other = isolated_env()
        root = other.root
        other.cleanup()
        self.assertFalse(root.exists())


class FixtureFormatTest(unittest.TestCase):
    """The three recorded formats parse into the same question/answer records."""

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)

    def test_every_fixture_says_whether_its_format_was_measured(self):
        claude = json.loads((FIXTURES / "claude-choice.jsonl").read_text(encoding="utf-8").splitlines()[0])
        codex = json.loads((FIXTURES / "codex-choice.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(claude["measured"], "actual")
        self.assertEqual(codex["payload"]["fixture"]["measured"], "actual")
        self.assertIn("measured: actual", (FIXTURES / "opencode-choice.sql").read_text(encoding="utf-8").splitlines()[0])

    def test_claude_fixture_yields_answered_questions_with_original_answer_text(self):
        chunk = tidy_transcripts.read_chunk("claude", FIXTURES / "claude-choice.jsonl")
        self.assertTrue(chunk.eof)
        self.assertEqual(chunk.cursor_to, (FIXTURES / "claude-choice.jsonl").stat().st_size)
        by_question = {c["question"]: c for c in chunk.choices}
        self.assertEqual(len(chunk.choices), 3)                 # the declined question is not a choice
        single = by_question["저장 방식을 어떻게 할까요?"]
        self.assertEqual(single["answers"], ["파일로 저장 (권장)"])
        self.assertEqual([o["label"] for o in single["options"]], ["파일로 저장 (권장)", "DB로 저장"])
        self.assertFalse(single["multi_select"])
        multi = by_question["함께 켤 기능을 고르세요"]
        self.assertTrue(multi["multi_select"])
        self.assertEqual(multi["answers"], ["로그 남기기", "자동 재시도, 두 번"])   # a label that holds a comma
        free = by_question["언제 시작할까요?"]
        self.assertEqual(free["answers"], ["내일 오전에 하자"])                    # free text kept as typed
        self.assertNotIn("이 방향으로 진행할까요?", by_question)
        self.assertIn("[user] 정리 시험을 시작합니다", chunk.text)
        self.assertIn("[assistant] 파일 저장으로 진행합니다.", chunk.text)
        self.assertNotIn("Your questions have been answered", chunk.text)

    def test_codex_fixture_yields_answered_questions_and_skips_the_empty_answer(self):
        chunk = tidy_transcripts.read_chunk("codex", FIXTURES / "codex-choice.jsonl")
        self.assertTrue(chunk.eof)
        got = [(c["question"], c["answers"]) for c in chunk.choices]
        self.assertEqual(got, [("이 경로로 진행할까요?", ["진행 (Recommended)"]),
                               ("범위를 정해 주세요", ["한 파일만 하고 나머지는 나중에"])])
        self.assertEqual(chunk.choices[0]["options"][1]["label"], "중단")
        self.assertIn("[user] 정리 시험을 시작합니다", chunk.text)

    def test_codex_call_and_answer_in_different_chunks_still_pair(self):
        path = FIXTURES / "codex-choice.jsonl"
        cursor, choices = 0, []
        while True:
            chunk = tidy_transcripts.read_chunk("codex", path, cursor, limit_bytes=1)   # one row per read
            choices.extend(chunk.choices)
            if chunk.eof:
                break
            self.assertGreater(chunk.cursor_to, cursor)
            cursor = chunk.cursor_to
        self.assertEqual([c["answers"] for c in choices],
                         [["진행 (Recommended)"], ["한 파일만 하고 나머지는 나중에"]])

    def test_opencode_fixture_yields_answered_questions_and_stops_at_the_open_one(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tidy_transcripts.now_epoch
        tidy_transcripts.now_epoch = lambda: FIXTURE_NOW
        self.addCleanup(setattr, tidy_transcripts, "now_epoch", original)
        with self.iso.patched_environ():
            chunk = tidy_transcripts.read_chunk("opencode", self.iso.opencode_db, 0, "ses_fixture0000000000000001")
        self.assertEqual(chunk.blocked, "open-question")
        self.assertFalse(chunk.eof)
        self.assertEqual(chunk.cursor_to, 5)                     # the running question (rowid 6) is not consumed
        got = {c["question"]: c["answers"] for c in chunk.choices}
        self.assertEqual(got, {"저장 방식을 어떻게 할까요?": ["파일로 저장 (권장)"],
                               "함께 켤 기능을 고르세요": ["로그 남기기", "알림 보내기"],
                               "언제 시작할까요?": ["내일 오전에 하자"]})
        self.assertIn("[assistant] 파일 저장으로 진행합니다.", chunk.text)
        self.assertNotIn("Asked", chunk.text)

    def test_all_three_harnesses_produce_the_same_record_shape(self):
        load_opencode_fixture(self.iso.opencode_db)
        chunks = [
            tidy_transcripts.read_chunk("claude", FIXTURES / "claude-choice.jsonl"),
            tidy_transcripts.read_chunk("codex", FIXTURES / "codex-choice.jsonl"),
            tidy_transcripts.read_chunk("opencode", self.iso.opencode_db, 0, "ses_fixture0000000000000001"),
        ]
        keys = {"harness", "call_id", "index", "asked_at", "header", "question", "options",
                "multi_select", "answers", "answer_raw", "note"}
        for chunk in chunks:
            self.assertTrue(chunk.choices, chunk.harness)
            for record in chunk.choices:
                self.assertEqual(set(record), keys)
                self.assertTrue(record["question"] and record["answers"])


if __name__ == "__main__":
    unittest.main()
