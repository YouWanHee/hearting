"""Operator-language titles and stable failure fallback; no live model calls."""
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import config, refresh_title as rt, titles
from fleet.collectors import opencode
from fleet.model import Session
from fleet.tests.test_f17_title_refresh import _ConfigHomeMixin


class TitleConsistencyTest(_ConfigHomeMixin, unittest.TestCase):
    def _transcript(self, harness):
        message = "Fleet 세션 제목 일관성"
        shapes = {
            "claude": {"type": "user", "message": {"role": "user", "content": message}},
            "codex": {"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": message}]}},
            "opencode": {"type": "text", "part": {"type": "text", "text": message}},
        }
        path = Path(self._tmp.name) / (harness + ".jsonl")
        path.write_text(json.dumps(shapes[harness], ensure_ascii=False) + "\n")
        return path

    def test_tool_heavy_tail_keeps_visible_dialogue_for_all_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self._transcript(harness)
                with path.open("a") as handle:
                    handle.write(json.dumps({"type": "tool", "payload": "x" * (2 << 20)}) + "\n")
                text, offset = rt.read_delta(str(path), 0, harness=harness)
                self.assertIn("Fleet 세션 제목 일관성", text)
                self.assertLessEqual(len(text), rt.TEXT_CAP)
                self.assertEqual(offset, path.stat().st_size)

    def test_codex_task_anchor_survives_tool_heavy_tail(self):
        path = self._transcript("codex")
        with path.open("a") as handle:
            # Put the current intent beyond the separate 64 KiB head anchor too.
            handle.write(json.dumps({"type": "tool", "payload": "x" * (128 << 10)}) + "\n")
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Latest Fleet subject"}]}}) + "\n")
            handle.write(json.dumps({"type": "tool", "payload": "x" * (2 << 20)}) + "\n")
        self.assertEqual(rt.read_origin(str(path), "codex"), "Latest Fleet subject")

    def test_auto_uses_now_language_and_explicit_config_changes_only_title(self):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            self.assertIn("in Korean.", rt._prompt("d"))
            self.assertEqual(rt.validate_title("TITLE: 세션 제목 일관성\nNOW: 시험 중", rt._title_lang()), "세션 제목 일관성")
            config.ensure()
            config.config_path().write_text('{"title_language": "en"}\n')
            prompt = rt._prompt("d")
            self.assertIn("in English.", prompt)
            self.assertIn("NOW: one sentence, in Korean,", prompt)
            self.assertIsNone(rt.validate_title("세션 제목 일관성", rt._title_lang()))

    def test_language_limits_and_localized_failure_outputs(self):
        for language, title in (("Korean", "세션 제목 일관성"), ("Japanese", "セッションのタイトル"),
                                ("Chinese", "会话标题一致性"), ("French", "Résumé des sessions")):
            with self.subTest(language=language):
                self.assertEqual(rt.validate_title(title, language), title)
        self.assertEqual(len(rt.validate_title("가" * 30, "Korean")), 20)
        for bad in ("진행 중 제목 갱신", "제목 없음", "알 수 없습니다", "Awaiting worker result"):
            self.assertIsNone(rt.validate_title(bad, "Korean"))

    def test_auto_normalizes_now_language_codes_without_changing_now(self):
        for code, name in (("ko", "Korean"), ("en", "English"), ("ja", "Japanese")):
            with self.subTest(code=code), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": code}):
                self.assertEqual(rt._title_lang(), name)
                self.assertIn("NOW: one sentence, in " + code + ",", rt._prompt("d"))

    def test_config_seed_is_once_and_invalid_file_degrades_without_rewriting(self):
        self.assertEqual(config.ensure(dry_run=True)["status"], "would-create")
        self.assertFalse(config.config_path().exists())
        self.assertEqual(config.ensure()["status"], "created")
        self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.validate()["status"], "valid")
        path = config.config_path()
        path.write_text('{"title_language":"ko"}\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(config.ensure()["status"], "preserved")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(rt._title_lang(), "Korean")
        path.write_text('{"title_language": ["ko"]}')
        self.assertEqual(config.validate()["status"], "invalid")
        self.assertEqual(config.title_language(), "auto")
        self.assertEqual(path.read_text(), '{"title_language": ["ko"]}')

    def test_installer_registry_uses_the_same_preference_reader(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "install"))
        import fleet_config
        import user_config
        self.assertEqual(fleet_config.ensure()["path"], str(config.config_path()))
        config.config_path().write_text('{"title_language": "ko"}\n')
        row = user_config.status(["fleet"])[0]
        self.assertEqual((row["status"], row["path"]), ("valid", str(config.config_path())))
        self.assertIn("title_language=ko", row["detail"])

    def test_failed_refresh_keeps_title_or_blank_with_one_existing_call(self):
        for harness in ("claude", "codex", "opencode"):
            for prior in ("세션 제목 일관성", ""):
                with self.subTest(harness=harness, prior=prior):
                    path = self._transcript(harness)
                    sid = harness + ("-prior" if prior else "-blank")
                    titles.write(sid, prior, harness=harness, now=time.time() - 90000)
                    with mock.patch.object(rt, "run_worker", return_value="") as worker, \
                         mock.patch.object(rt, "_provider_source", return_value="refresher:fixture"):
                        rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                 "--slotdir", str(Path(self._tmp.name) / "slot")])
                    self.assertEqual(worker.call_count, 1)
                    self.assertEqual(titles.last_title(sid, harness=harness), prior or None)

    def test_same_korean_title_contract_for_three_harnesses_in_one_call_each(self):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    path = self._transcript(harness)
                    sid = harness + "-korean"
                    with mock.patch.object(rt, "run_worker", return_value="TITLE: 세션 제목 일관성\nNOW: 회귀 확인 중") as worker, \
                         mock.patch.object(rt, "_provider_source", return_value="refresher:fixture"):
                        rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                 "--slotdir", str(Path(self._tmp.name) / "slot")])
                    self.assertEqual(worker.call_count, 1)
                    self.assertIn("in Korean.", worker.call_args.args[0])
                    self.assertEqual(titles.last_title(sid, harness=harness), "세션 제목 일관성")

    def test_opencode_native_title_never_fills_gap_and_stale_success_survives(self):
        db = Path(self._tmp.name) / "opencode.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE session (id, slug, agent, model, cost, tokens_input, tokens_output, "
                        "tokens_reasoning, time_updated, parent_id, directory, time_created, title)")
            con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_title", "fixture", None, None, 0, 0, 0, 0, 1000, None, "/fixture", 1000, "지엽적인 자체 제목"))
        before = db.read_bytes()
        with mock.patch.object(opencode, "_db", return_value=str(db)), \
             mock.patch.object(opencode.session_registry, "read", return_value=None), \
             mock.patch.object(opencode, "session_of_process", return_value=("ses_title", "opencode-tui-selection")), \
             mock.patch.object(opencode, "_process_started_ms", return_value=None):
            sess = Session(harness="opencode", pid=99999999, cwd="/fixture")
            opencode.enrich(sess)
            self.assertIsNone(sess.title)
            titles.write("ses_title", "세션 제목 일관성", harness="opencode", now=time.time() - 90000)
            opencode.enrich(sess)
            self.assertEqual(sess.title, "세션 제목 일관성")
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
