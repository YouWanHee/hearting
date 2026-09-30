#!/usr/bin/env python3
"""session-tidy shared state: cards, hook injection, ledger, notices, transcript reading.

Every subprocess and every in-process call runs inside ``tidy_isolation`` (a
``/var/tmp`` root for HOME, XDG_*, MEM_STORE, remote disabled, no worker/pane
markers unless a test sets one on purpose).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy as st  # noqa: E402
import tidy_transcripts as tt  # noqa: E402
from test_tidy_isolation import FIXTURES, FIXTURE_NOW, load_opencode_fixture  # noqa: E402

TIDY = ROOT / "utilities" / "session_tidy.py"
DAY = 86400
PANE = "test:pane-a"


class TidyCase(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "proj"
        self.cwd.mkdir()
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"

    def cli(self, *args, pane=PANE, extra=None, input=None, cwd=None):
        env = dict(extra or {})
        if pane:
            env["HERDR_PANE_ID"] = pane
        return self.iso.run([sys.executable, TIDY, *args], input=input, extra=env, cwd=cwd or self.cwd)

    def hook(self, harness, event, sid, *more, pane=PANE, extra=None, cwd=None):
        result = self.cli("hook", "--harness", harness, "--event", event, "--session-id", sid,
                          *more, pane=pane, extra=extra, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def card(self, sid, text, harness="claude", pane=PANE, extra=None, cwd=None):
        result = self.cli("card", "--harness", harness, "--session-id", sid, "--text", text,
                          pane=pane, extra=extra, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def library(self, extra=None):
        env = {"HERDR_PANE_ID": PANE}
        env.update(extra or {})
        return self.iso.patched_environ(env)


class CardWriteTest(TidyCase):

    def test_card_is_written_at_once_with_private_modes(self):
        line = self.card("sid-A", "진행 중인 일: 정리 시험\n기다리는 결정: 없음\n다음 할 일: 병합\n관련: PR 12")
        match = re.fullmatch(r"card=(\S+) seat=([0-9a-f]+)", line)
        self.assertIsNotNone(match, line)
        text_path = Path(match.group(1))
        json_path = text_path.with_suffix(".json")
        for path in (text_path, json_path):
            self.assertTrue(path.is_file(), path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        for directory in (self.state, self.state / "cards", self.state / "sessions", self.state / "locks"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["author"]["sid"], data["author"]["harness"]), (1, "sid-A", "claude"))
        self.assertIn("기다리는 결정", data["body"])
        self.assertIn("기다리는 결정", text_path.read_text(encoding="utf-8"))

    def test_second_card_advances_the_generation_and_keeps_history(self):
        self.card("sid-A", "첫 카드")
        self.card("sid-A", "둘째 카드")
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["body"]), (2, "둘째 카드"))
        history = list((self.state / "card-history").glob("*/*.json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text(encoding="utf-8"))["body"], "첫 카드")

    def test_card_needs_no_argument_the_body_alone_is_enough(self):
        result = self.cli("card", input="본문만 넘김", extra={"CLAUDE_CODE_SESSION_ID": "sid-env"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["author"]["harness"], data["author"]["sid"]), ("claude", "sid-env"))

    def test_each_harness_session_variable_names_the_author(self):
        for name, value, harness in (("CODEX_THREAD_ID", "thr-1", "codex"), ("OPENCODE_SESSION_ID", "ses_1", "opencode")):
            result = self.cli("card", input="본문", extra={name: value}, pane=f"test:{harness}")
            self.assertEqual(result.returncode, 0, result.stderr)
        authors = sorted(
            (json.loads(p.read_text(encoding="utf-8"))["author"]["harness"],
             json.loads(p.read_text(encoding="utf-8"))["author"]["sid"])
            for p in (self.state / "cards").glob("*.json"))
        self.assertEqual(authors, [("codex", "thr-1"), ("opencode", "ses_1")])

    def test_author_falls_back_to_the_latest_ledger_session_of_the_seat(self):
        self.hook("claude", "start", "sid-ledger")
        result = self.cli("card", input="환경변수 없이 작성")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["author"]["harness"], data["author"]["sid"]), ("claude", "sid-ledger"))
        self.assertEqual(self.hook("claude", "prompt", "sid-ledger"), "")     # it is the author

    def test_card_body_is_not_rejected_for_shape_only_capped_at_8_kib(self):
        self.assertTrue(self.card("sid-A", "네 칸 없이 그냥 적음").startswith("card="))
        big = "가" * 6000                                                       # 18,000 bytes
        self.assertTrue(self.card("sid-A", big).startswith("card="))
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertLessEqual(len(data["body"].encode("utf-8")), st.CARD_BODY_MAX_BYTES)
        self.assertEqual(self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "  ").stdout.strip(),
                         "card=none reason=empty-body")

    def test_body_from_a_file(self):
        body = self.iso.root / "card.txt"
        body.write_text("파일 본문", encoding="utf-8")
        result = self.cli("card", "--harness", "codex", "--session-id", "t1", "--file", str(body))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("파일 본문", next((self.state / "cards").glob("*.md")).read_text(encoding="utf-8"))


class CardConsumeTest(TidyCase):

    def test_new_session_receives_the_card_once_at_start_and_not_again(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "진행 중인 일: 표식-7Q")
        got = self.hook("claude", "start", "sid-B", "--source", "clear")
        self.assertIn("표식-7Q", got)
        self.assertIn("[세션 카드]", got)
        self.assertIn(str(self.state / "cards"), got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")

    def test_new_session_that_only_sends_a_prompt_receives_it_once(self):
        self.card("sid-A", "표식-P1")
        self.assertIn("표식-P1", self.hook("codex", "prompt", "sid-C"))
        self.assertEqual(self.hook("codex", "prompt", "sid-C"), "")

    def test_authoring_session_gets_nothing_from_ordinary_prompts_or_restarts(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-A")
        for _ in range(3):
            self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")
        self.assertEqual(self.hook("claude", "start", "sid-A", "--source", "resume"), "")

    def test_card_arrives_once_after_compact_for_the_author_and_the_receiver(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-C")
        after = self.hook("claude", "start", "sid-A", "--source", "compact")
        self.assertIn("표식-C", after)
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")
        self.assertIn("표식-C", self.hook("claude", "start", "sid-B"))
        self.assertIn("표식-C", self.hook("claude", "start", "sid-B", "--source", "compact"))
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")

    def test_compact_event_only_marks_the_round_and_the_next_prompt_delivers(self):
        self.hook("opencode", "start", "ses_A")
        self.card("ses_A", "표식-O", harness="opencode")
        self.assertEqual(self.hook("opencode", "compact", "ses_A"), "")
        self.assertIn("표식-O", self.hook("opencode", "prompt", "ses_A"))
        self.assertEqual(self.hook("opencode", "prompt", "ses_A"), "")

    def test_start_and_compact_event_for_one_compaction_count_once(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-D")
        self.assertIn("표식-D", self.hook("claude", "start", "sid-A", "--source", "compact"))
        self.assertEqual(self.hook("claude", "compact", "sid-A"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")

    def test_a_newer_card_generation_is_delivered_again(self):
        self.card("sid-A", "표식-1")
        self.assertIn("표식-1", self.hook("claude", "start", "sid-B"))
        self.card("sid-A", "표식-2")
        got = self.hook("claude", "start", "sid-B", "--source", "resume")
        self.assertIn("표식-2", got)
        self.assertNotIn("표식-1", got)

    def test_no_card_means_no_output_and_exit_zero(self):
        for event in ("start", "prompt", "compact"):
            result = self.cli("hook", "--harness", "claude", "--event", event, "--session-id", "sid-X")
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_an_empty_state_folder_never_makes_output(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(self.hook("codex", "start", "t-new"), "")

    def test_reread_repeats_the_consumed_text_for_that_session_only(self):
        self.card("sid-A", "표식-R")
        first = self.hook("opencode", "prompt", "ses_B")
        self.assertIn("표식-R", first)
        again = self.hook("opencode", "prompt", "ses_B", "--reread")
        self.assertEqual(again.strip(), first.strip())
        self.assertEqual(self.hook("opencode", "prompt", "ses_C", "--reread"), "")
        self.hook("opencode", "compact", "ses_B")
        self.assertEqual(self.hook("opencode", "prompt", "ses_B", "--reread").strip().count("표식-R"), 0)

    def test_receipt_is_written_only_after_the_output_went_out(self):
        self.card("sid-A", "표식-E")

        def broken(_text):
            raise BrokenPipeError()

        with self.library():
            with self.assertRaises(BrokenPipeError):
                st.run_hook("claude", "start", "sid-B", cwd=str(self.cwd), emit=broken)
            sent = []
            text = st.run_hook("claude", "start", "sid-B", cwd=str(self.cwd), emit=sent.append)
        self.assertIn("표식-E", text)
        self.assertEqual(sent, [text])

    def test_output_never_exceeds_2400_bytes_and_always_names_the_file(self):
        self.card("sid-A", "가나다라 " * 900)
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_notice(seat, "정리 결과: 새 기록 3건. 되돌리기: mem tidy-undo b1", author_harness="claude", author_sid="sid-A")
        got = self.hook("claude", "start", "sid-B")
        self.assertLessEqual(len(got.encode("utf-8")), st.INJECTION_MAX_BYTES)
        self.assertIn("이하 생략", got)
        self.assertIn(str(self.state / "cards"), got)
        self.assertIn("[정리 결과]", got)

    def test_header_carries_the_write_time_and_elapsed_days(self):
        self.card("sid-A", "오래된 카드")
        json_path = next((self.state / "cards").glob("*.json"))
        data = json.loads(json_path.read_text(encoding="utf-8"))
        data["authored_at_epoch"] -= 4 * DAY
        json_path.write_text(json.dumps(data), encoding="utf-8")
        got = self.hook("claude", "start", "sid-B")
        self.assertIn("4일 전", got)
        self.assertIn("오래된 카드", got)                                      # an old card is never dropped


class SeatTest(TidyCase):

    def test_pane_is_the_seat_when_present_else_harness_and_project(self):
        with self.iso.patched_environ({"HERDR_PANE_ID": "test:p9"}):
            pane_a = st.resolve_seat("claude", str(self.cwd))
            pane_b = st.resolve_seat("codex", str(self.cwd))
        self.assertEqual((pane_a.kind, pane_a.key), ("pane", pane_b.key))
        with self.iso.patched_environ():
            claude = st.resolve_seat("claude", str(self.cwd))
            codex = st.resolve_seat("codex", str(self.cwd))
            other_dir = self.iso.root / "other"
            other_dir.mkdir()
            other = st.resolve_seat("claude", str(other_dir))
        self.assertEqual(claude.kind, "project")
        self.assertEqual(len({claude.key, codex.key, other.key, pane_a.key}), 4)

    def test_without_a_pane_the_card_reaches_only_the_same_harness_and_project(self):
        self.card("sid-A", "표식-S", pane=None)
        self.assertIn("표식-S", self.hook("claude", "start", "sid-B", pane=None))
        self.assertEqual(self.hook("codex", "start", "t-B", pane=None), "")
        elsewhere = self.iso.root / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.hook("claude", "start", "sid-C", pane=None, cwd=elsewhere), "")
        self.assertEqual(self.hook("claude", "start", "sid-D", pane="test:other-pane"), "")

    def test_different_panes_do_not_share_cards(self):
        self.card("sid-A", "표식-pane-a", pane="test:pane-a")
        self.assertEqual(self.hook("claude", "start", "sid-B", pane="test:pane-b"), "")
        self.assertIn("표식-pane-a", self.hook("claude", "start", "sid-B2", pane="test:pane-a"))


class WorkerTest(TidyCase):

    MARKERS = ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
               {"AGENT_DISPATCH_DEPTH": "2"}, {"OPENCODE_DISPATCH_SLUG": "slug-1"},
               {"FLEET_TITLE_REFRESH": "1"}, {"MEM_DISTILL": "1"})

    def test_worker_that_inherited_the_supervisor_pane_gets_nothing_and_leaves_no_trace(self):
        self.card("sid-A", "표식-W")                       # written by the main session in that pane
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                got = self.hook("claude", "start", "sid-worker", extra=marker)      # pane PANE inherited
                self.assertEqual(got, "")
                self.assertEqual(self.hook("claude", "prompt", "sid-worker", extra=marker), "")
                ledgers = "".join(p.read_text(encoding="utf-8") for p in (self.state / "sessions").glob("*.jsonl"))
                self.assertNotIn("sid-worker", ledgers)
        self.assertFalse((self.state / "consumed").exists())
        self.assertIn("표식-W", self.hook("claude", "start", "sid-B"))               # the real next session still gets it

    def test_worker_writes_no_card_and_sees_no_notice(self):
        self.card("sid-A", "원래 카드")
        for marker in self.MARKERS:
            result = self.cli("card", "--harness", "claude", "--session-id", "sid-worker", "--text", "워커 카드", extra=marker)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "card=none reason=worker"))
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["body"]), (1, "원래 카드"))
        with self.library():
            st.write_notice(st.resolve_seat("claude", str(self.cwd)), "결과 한 줄")
        self.assertEqual(self.hook("claude", "prompt", "sid-worker", extra={"AGENT_SESSION_ROLE": "worker"}), "")

    def test_the_worker_check_runs_before_the_pane_check_in_process(self):
        self.assertTrue(st.is_worker({"AGENT_SESSION_ROLE": "worker", "HERDR_PANE_ID": "wB:p1N"}))
        self.assertFalse(st.is_worker({"HERDR_PANE_ID": "wB:p1N"}))
        with self.library({"AGENT_SESSION_ROLE": "worker"}):
            self.assertEqual(st.run_hook("claude", "start", "s1", cwd=str(self.cwd)), "")
        self.assertFalse(self.state.exists())


class NoticeTest(TidyCase):

    def notice(self, text="정리 결과: 새 기록 2건. 되돌리기: mem tidy-undo b7", author=("claude", "sid-A")):
        with self.library():
            st.write_notice(st.resolve_seat(author[0], str(self.cwd)), text,
                            author_harness=author[0], author_sid=author[1])

    def test_notice_is_shown_once_at_the_next_prompt_of_the_authoring_session(self):
        self.hook("claude", "start", "sid-A")
        self.notice()
        got = self.hook("claude", "prompt", "sid-A")
        self.assertIn("[정리 결과]", got)
        self.assertIn("mem tidy-undo b7", got)
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")

    def test_notice_goes_to_a_new_session_when_the_author_is_gone(self):
        self.hook("claude", "start", "sid-A")
        self.notice()
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-B", "start", now=st.now_epoch() + 5)
        got = self.hook("claude", "start", "sid-B", "--source", "clear")
        self.assertIn("mem tidy-undo b7", got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")            # already shown once

    def test_notice_waits_for_a_living_author_in_a_paneless_seat(self):
        self.hook("claude", "start", "sid-A", pane=None)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_notice(seat, "결과 한 줄", author_harness="claude", author_sid="sid-A")
        self.assertEqual(self.hook("claude", "start", "sid-B", pane=None), "")   # sid-A was active a moment ago
        self.assertIn("결과 한 줄", self.hook("claude", "prompt", "sid-A", pane=None))

    def test_notice_and_card_arrive_together_within_the_cap(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-N")
        self.notice()
        got = self.hook("claude", "start", "sid-B")
        self.assertIn("[정리 결과]", got)
        self.assertIn("표식-N", got)
        self.assertLessEqual(len(got.encode("utf-8")), st.INJECTION_MAX_BYTES)


class LedgerTest(TidyCase):

    def test_hook_records_the_session_transcript_and_cwd(self):
        self.hook("codex", "start", "thr-1", "--transcript", "/x/rollout.jsonl", "--source", "startup")
        with self.library():
            seat = st.resolve_seat("codex", str(self.cwd))
            row = st.session_summary(seat)[("codex", "thr-1")]
        self.assertEqual((row["transcript"], row["cwd"], row["epoch"]), ("/x/rollout.jsonl", str(self.cwd), 0))
        self.assertGreater(row["first_seen"], 0)

    def test_compact_raises_the_epoch_and_prompts_are_throttled(self):
        self.hook("claude", "start", "sid-A")
        for _ in range(4):
            self.hook("claude", "prompt", "sid-A")
        self.hook("claude", "compact", "sid-A")
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            row = st.session_summary(seat)[("claude", "sid-A")]
            lines = st._read_ledger_lines(seat)
        self.assertEqual(row["epoch"], 1)
        self.assertLessEqual(len(lines), 4)                                       # start, one prompt, compact (+ slack)

    def test_a_long_ledger_folds_without_losing_epochs_or_first_seen(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            base = 1_700_000_000.0
            st.record_event(seat, "claude", "old", "start", now=base)
            st.record_event(seat, "claude", "old", "compact", now=base + 1, bump_epoch=True)
            for index in range(st.LEDGER_FOLD_LINES + 50):
                st.record_event(seat, "claude", "busy", "prompt", now=base + 100 + index * 60)
            lines = st._read_ledger_lines(seat)
            rows = st.session_summary(seat)
        self.assertLess(len(lines), st.LEDGER_FOLD_LINES)
        self.assertEqual(rows[("claude", "old")]["epoch"], 1)
        self.assertEqual(rows[("claude", "old")]["first_seen"], base)
        self.assertIn(("claude", "busy"), rows)

    def test_latest_session_picks_the_most_recent_of_a_harness(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "s1", "start", now=1000.0)
            st.record_event(seat, "codex", "s2", "start", now=2000.0)
            st.record_event(seat, "claude", "s3", "start", now=3000.0)
            self.assertEqual(st.latest_session(seat)["sid"], "s3")
            self.assertEqual(st.latest_session(seat, "codex")["sid"], "s2")

    def test_status_prints_state_location_and_card(self):
        self.card("sid-A", "카드")
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"state_root={self.state}", result.stdout)
        self.assertIn("card=gen1", result.stdout)
        info = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual((info["card"]["generation"], info["worker"]), (1, False))


class StateSafetyTest(TidyCase):

    def test_a_hook_is_silent_when_the_state_folder_is_unusable(self):
        blocker = self.iso.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        result = self.iso.run([sys.executable, TIDY, "hook", "--harness", "claude", "--event", "start",
                               "--session-id", "s"], extra={"HERDR_PANE_ID": PANE, "XDG_STATE_HOME": str(blocker)})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_symlinked_state_folder_is_refused_and_its_target_untouched(self):
        elsewhere = self.iso.root / "elsewhere"
        elsewhere.mkdir()
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(elsewhere)
        self.assertEqual(self.hook("claude", "start", "s"), "")
        result = self.cli("card", "--harness", "claude", "--session-id", "s", "--text", "x")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_bad_hook_arguments_still_exit_zero(self):
        for args in (["hook"], ["hook", "--harness", "claude", "--event", "start", "--bogus"], ["hook", "--event", "x"]):
            result = self.cli(*args)
            self.assertEqual((result.returncode, result.stdout), (0, ""), args)


def jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def claude_row(kind: str, text: str, cwd: str = "/w") -> dict:
    return {"type": kind, "cwd": cwd, "sessionId": "s", "message": {"role": kind, "content": text}}


class ChunkReaderTest(TidyCase):

    def test_a_large_input_is_read_in_whole_row_chunks_and_the_cursor_moves_only_that_far(self):
        path = self.iso.root / "big.jsonl"
        rows = [claude_row("user" if i % 2 == 0 else "assistant", f"행 {i:04d} " + "가" * 700) for i in range(400)]
        jsonl(path, rows)
        limit, cursor, seen, texts = 64 * 1024, 0, 0, []
        raw = path.read_bytes()
        while True:
            chunk = tt.read_chunk("claude", path, cursor, limit_bytes=limit)
            self.assertLessEqual(chunk.cursor_to - cursor, limit)
            self.assertEqual(raw[chunk.cursor_to - 1:chunk.cursor_to], b"\n")     # always on a row boundary
            seen += chunk.rows
            texts.append(chunk.text)
            if chunk.eof:
                break
            self.assertGreater(chunk.cursor_to, cursor)
            cursor = chunk.cursor_to
        self.assertEqual((seen, chunk.cursor_to), (400, len(raw)))
        joined = "\n".join(texts)
        self.assertEqual([m for m in re.findall(r"행 (\d{4})", joined)], [f"{i:04d}" for i in range(400)])
        first = tt.read_chunk("claude", path, 0, limit_bytes=limit)
        self.assertNotIn("행 0399", first.text)                                     # nothing skipped to the tail

    def test_only_the_part_after_the_watermark_is_read(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", f"메시지 {i}") for i in range(6)])
        with self.library():
            self.assertEqual(tt.read_watermark("claude", "sid-1"), {"cursor": 0})
            first = tt.read_chunk("claude", path, 0, limit_bytes=1)                 # one row
            tt.write_watermark("claude", "sid-1", first.cursor_to, source=str(path))
            mark = tt.read_watermark("claude", "sid-1")
            self.assertEqual(mark["cursor"], first.cursor_to)
            self.assertEqual(stat.S_IMODE(tt.watermark_path("claude", "sid-1").stat().st_mode), 0o600)
            rest = tt.read_pending("claude", "sid-1", path)
            self.assertNotIn("메시지 0", rest.text)
            self.assertIn("메시지 1", rest.text)
            self.assertIn("메시지 5", rest.text)
            tt.write_watermark("claude", "sid-1", rest.cursor_to, source=str(path))
            self.assertEqual(tt.read_pending("claude", "sid-1", path).text, "")
            with open(path, "a", encoding="utf-8") as handle:                       # the session goes on
                handle.write(json.dumps(claude_row("user", "메시지 새로"), ensure_ascii=False) + "\n")
            self.assertEqual(tt.read_pending("claude", "sid-1", path).text, "[user] 메시지 새로")

    def test_a_replaced_or_shorter_file_restarts_from_the_beginning(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "옛날 " + "x" * 200) for _ in range(5)])
        with self.library():
            tt.write_watermark("claude", "sid-2", path.stat().st_size, source=str(path))
            path.unlink()
            jsonl(path, [claude_row("user", "새 파일")])
            self.assertEqual(tt.read_pending("claude", "sid-2", path).text, "[user] 새 파일")

    def test_a_half_written_last_row_does_not_move_the_cursor(self):
        path = self.iso.root / "s.jsonl"
        good = json.dumps(claude_row("user", "완성된 행"), ensure_ascii=False) + "\n"
        partial = json.dumps(claude_row("user", "쓰는 중"), ensure_ascii=False)
        path.write_text(good + partial[:20], encoding="utf-8")
        chunk = tt.read_chunk("claude", path, 0)
        self.assertEqual((chunk.rows, chunk.cursor_to, chunk.text), (1, len(good.encode("utf-8")), "[user] 완성된 행"))
        self.assertFalse(chunk.eof)
        path.write_text(good + partial, encoding="utf-8")                          # complete, but no newline yet
        chunk = tt.read_chunk("claude", path, chunk.cursor_to)
        self.assertEqual((chunk.rows, chunk.eof, chunk.text), (1, True, "[user] 쓰는 중"))

    def test_a_single_row_longer_than_the_limit_is_read_whole(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "길다 " + "나" * 5000), claude_row("user", "짧다")])
        chunk = tt.read_chunk("claude", path, 0, limit_bytes=100)
        self.assertEqual(chunk.rows, 1)
        self.assertIn("길다", chunk.text)
        self.assertGreater(chunk.cursor_to, 5000)
        second = tt.read_chunk("claude", path, chunk.cursor_to, limit_bytes=100)
        self.assertEqual((second.text, second.eof), ("[user] 짧다", True))

    def test_a_row_beyond_the_hard_cap_is_stepped_over_and_counted(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "앞"), claude_row("user", "거대 " + "z" * 3000), claude_row("user", "뒤")])
        original = tt.MAX_ROW_BYTES
        tt.MAX_ROW_BYTES = 1000
        self.addCleanup(setattr, tt, "MAX_ROW_BYTES", original)
        first = tt.read_chunk("claude", path, 0, limit_bytes=200)
        self.assertIn("앞", first.text)
        second = tt.read_chunk("claude", path, first.cursor_to, limit_bytes=200)
        self.assertEqual((second.rows, second.skipped_oversize), (0, 1))
        third = tt.read_chunk("claude", path, second.cursor_to, limit_bytes=200)
        self.assertEqual((third.text, third.eof), ("[user] 뒤", True))

    def test_a_missing_record_is_an_empty_unread_chunk(self):
        chunk = tt.read_chunk("claude", self.iso.root / "gone.jsonl", 7)
        self.assertEqual((chunk.cursor_to, chunk.text, chunk.choices), (7, "", []))
        self.assertTrue(chunk.error)

    def test_an_open_opencode_question_holds_the_cursor_until_it_is_answered(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW
        self.addCleanup(setattr, tt, "now_epoch", original)
        sid = "ses_fixture0000000000000001"
        first = tt.read_chunk("opencode", self.iso.opencode_db, 0, sid)
        self.assertEqual((first.blocked, first.cursor_to), ("open-question", 5))
        connection = sqlite3.connect(self.iso.opencode_db)
        row = connection.execute("SELECT data FROM part WHERE rowid = 6").fetchone()[0]
        part = json.loads(row)
        part["state"].update({"status": "completed", "metadata": {"answers": [["진행 (권장)"]]},
                              "output": "User has answered your questions."})
        connection.execute("UPDATE part SET data = ? WHERE rowid = 6", (json.dumps(part, ensure_ascii=False),))
        connection.commit()
        connection.close()
        second = tt.read_chunk("opencode", self.iso.opencode_db, first.cursor_to, sid)
        self.assertEqual([(c["question"], c["answers"]) for c in second.choices],
                         [("이 방향으로 진행할까요?", ["진행 (권장)"])])
        self.assertEqual((second.cursor_to, second.eof, second.blocked), (6, True, ""))

    def test_a_stale_open_opencode_question_does_not_block_forever(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW + 2 * tt.OPEN_QUESTION_STALE_SEC
        self.addCleanup(setattr, tt, "now_epoch", original)
        chunk = tt.read_chunk("opencode", self.iso.opencode_db, 0, "ses_fixture0000000000000001")
        self.assertEqual((chunk.blocked, chunk.cursor_to, chunk.eof), ("", 6, True))

    def test_opencode_rows_are_chunked_by_rowid_with_the_cursor_at_the_last_row_read(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW + 2 * tt.OPEN_QUESTION_STALE_SEC
        self.addCleanup(setattr, tt, "now_epoch", original)
        sid, cursor, order = "ses_fixture0000000000000001", 0, []
        while True:
            chunk = tt.read_chunk("opencode", self.iso.opencode_db, cursor, sid, limit_bytes=1)
            order.append(chunk.cursor_to)
            if chunk.eof:
                break
            cursor = chunk.cursor_to
        self.assertEqual(order, [1, 2, 3, 4, 5, 6])


class RecentSessionsTest(TidyCase):

    def make_claude_record(self, sid: str, cwd: Path, age_days: float, now: float) -> Path:
        directory = self.iso.claude_dir / "projects" / "-proj"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sid}.jsonl"
        jsonl(path, [claude_row("user", "이전 대화", cwd=str(cwd))])
        os.utime(path, (now - age_days * DAY, now - age_days * DAY))
        return path

    def test_three_day_boundary_uses_the_ledger_and_is_inclusive(self):
        now = 1_800_000_000.0
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "just-in", "start", now=now - 3 * DAY + 1)
            st.record_event(seat, "claude", "on-edge", "start", now=now - 3 * DAY)
            st.record_event(seat, "claude", "just-out", "start", now=now - 3 * DAY - 1)
            st.record_event(seat, "claude", "today", "start", now=now - 5)
            got = [r["sid"] for r in tt.select_recent_sessions(seat, now=now)]
        self.assertEqual(got, ["today", "just-in", "on-edge"])

    def test_the_record_mtime_can_keep_a_ledger_session_inside_the_window(self):
        now = 1_800_000_000.0
        path = self.make_claude_record("long-running", self.cwd, 0.5, now)
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "long-running", "start", transcript=str(path), now=now - 9 * DAY)
            got = tt.select_recent_sessions(seat, now=now)
        self.assertEqual([r["sid"] for r in got], ["long-running"])

    def test_unledgered_past_records_join_only_when_the_project_matches_and_no_pane_is_set(self):
        now = 1_800_000_000.0
        other = self.iso.root / "other"
        other.mkdir()
        self.make_claude_record("same-project", self.cwd, 1, now)
        self.make_claude_record("other-project", other, 1, now)
        self.make_claude_record("too-old", self.cwd, 3.01, now)
        self.make_claude_record("edge-in", self.cwd, 2.99, now)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            got = {r["sid"]: r["source"] for r in tt.select_recent_sessions(seat, now=now)}
        self.assertEqual(got, {"same-project": "project-key", "edge-in": "project-key"})
        with self.library():                                                        # a pane seat: no guessing from paths
            pane_seat = st.resolve_seat("claude", str(self.cwd))
            self.assertEqual(tt.select_recent_sessions(pane_seat, now=now), [])

    def test_ledger_entries_win_over_discovery_for_the_same_session(self):
        now = 1_800_000_000.0
        path = self.make_claude_record("both", self.cwd, 1, now)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "both", "start", transcript=str(path), now=now - DAY)
            got = tt.select_recent_sessions(seat, now=now)
        self.assertEqual([(r["sid"], r["source"]) for r in got], [("both", "ledger")])

    def test_codex_and_opencode_records_are_found_by_their_own_project_marker(self):
        now = FIXTURE_NOW
        rollout = self.iso.codex_home / "sessions" / "2026" / "09" / "30" / \
            "rollout-2026-09-30T01-00-00-22222222-2222-4222-8222-222222222222.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text((FIXTURES / "codex-choice.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        os.utime(rollout, (now - 3600, now - 3600))
        load_opencode_fixture(self.iso.opencode_db)
        with self.iso.patched_environ():
            codex_seat = st.seat_for_project("codex", st.project_key_for("/work/fixture-project"))
            open_seat = st.seat_for_project("opencode", st.project_key_for("/work/fixture-project"))
            claude_seat = st.seat_for_project("claude", st.project_key_for("/work/fixture-project"))
            codex = tt.select_recent_sessions(codex_seat, now=now)
            opencode = tt.select_recent_sessions(open_seat, now=now)
            claude = tt.select_recent_sessions(claude_seat, now=now)
        self.assertEqual([r["sid"] for r in codex], ["22222222-2222-4222-8222-222222222222"])
        self.assertEqual([r["sid"] for r in opencode], ["ses_fixture0000000000000001"])
        self.assertEqual(claude, [])


class LocateTest(TidyCase):

    def test_transcripts_are_found_under_each_harness_home(self):
        claude = self.iso.claude_dir / "projects" / "-proj" / "sid-9.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text("{}\n", encoding="utf-8")
        codex = self.iso.codex_home / "sessions" / "2026" / "09" / "30" / "rollout-2026-09-30T00-00-00-abc-def.jsonl"
        codex.parent.mkdir(parents=True)
        codex.write_text("{}\n", encoding="utf-8")
        load_opencode_fixture(self.iso.opencode_db)
        with self.iso.patched_environ():
            self.assertEqual(tt.locate_transcript("claude", "sid-9"), claude)
            self.assertEqual(tt.locate_transcript("codex", "abc-def"), codex)
            self.assertEqual(tt.locate_transcript("opencode", "ses_x"), self.iso.opencode_db)
            self.assertIsNone(tt.locate_transcript("claude", "nope"))
            self.assertEqual(tt.locate_transcript("codex", "zzz", hint=str(claude)), claude)


if __name__ == "__main__":
    unittest.main()
