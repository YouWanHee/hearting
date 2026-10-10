"""Fleet preserves typed diagnostics and the origin of memory changes."""
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import fleet, render
from fleet.collectors import dispatch, memory, resource_runs
from fleet.model import Session


def flatten(rows):
    return "\n".join("".join(text for text, _ in row) for row in rows if row)


class ObservationTest(unittest.TestCase):
    def tearDown(self):
        render.set_show_all(False)
        render.set_process_view(False)

    def test_resource_diagnostics_never_become_jobs_log_errors(self):
        diagnostic = {"kind": "missing-registry", "path": "/work/project/old.json",
                      "reason": "registered-path-absent", "blocking": False}
        with mock.patch.object(resource_runs, "_shared") as shared, \
             mock.patch.object(dispatch.collect, "last_malformed", 0):
            shared.return_value.scan.return_value = ([], [diagnostic])
            resource_runs.collect()
            self.assertEqual(render._malformed(), 0)
            self.assertEqual(resource_runs.collect.last_diagnostics, [diagnostic])

    def test_json_keeps_raw_resource_diagnostic_evidence(self):
        evidence = [{"kind": "resource-collector", "error": "PermissionError", "blocking": True}]
        value = json.loads(fleet._snapshot_json([], [], resource_diagnostics=evidence))
        self.assertEqual(value["resource_diagnostics"], evidence)

    def test_both_views_distinguish_past_references_from_unobserved_resources(self):
        missing = {"kind": "missing-registry", "path": "/work/project/old.json",
                   "reason": "registered-path-absent", "blocking": False}
        failure = {"kind": "resource-collector", "error": "PermissionError"}
        for process in (False, True):
            render.set_process_view(process)
            render.set_show_all(False)
            quiet = flatten(render._build_lines([], [], "both", False, 0,
                            term_width=80, resource_diagnostics=[missing]))
            self.assertNotIn("old.json", quiet)
            render.set_show_all(True)
            rows = render._build_lines([], [], "both", False, 2,
                           term_width=80, resource_diagnostics=[missing, failure])
            text = flatten(rows)
            for expected in ("jobs.log", "2", "과거 참조", "비차단", "리소스 관측 미확인", "PermissionError"):
                self.assertIn(expected, text)
            self.assertNotIn("malformed jobs.log", text)
            self.assertTrue(all(render._dw("".join(t for t, _ in r)) <= 80
                                for r in render._diagnostic_rows([missing, failure], 2, 80)))

    def test_memory_collector_keeps_identity_and_origin(self):
        event = {"id": "record-1", "cwd": "/tmp/tmp-1", "scope": "project",
                 "project": "fixture", "ts": "2026-10-10T09:00:00", "snippet": "선택"}
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"MEM_WRITE_EVENTS": td + "/events"}):
            Path(td, "events").write_text(json.dumps(event) + "\n")
            result = memory.collect(datetime.datetime(2026, 10, 10, 12), include_summary=False)
        self.assertEqual(result["recent"][0], {**event, **{key: None for key in
                         ("action", "tier", "type", "actor", "sid")}})
        projected = next(iter(result["by_repo"].values()))[0]
        self.assertEqual(projected["id"], "record-1")
        self.assertEqual(projected["cwd"], "/tmp/tmp-1")

    def test_memory_content_and_origin_precede_storage_metadata_and_fit_width(self):
        events = [{"id": "r1", "cwd": "/tmp/tmp-first", "snippet": "[결정] 범위를 정해 주세요",
                   "ts": "2026-10-10T09:05:34", "action": "decision-record", "actor": "tidy-applier"},
                  {"id": "r2", "cwd": "/tmp/tmp-second", "snippet": "[결정] 범위를 정해 주세요",
                   "ts": "2026-10-10T09:05:35", "action": "decision-record", "actor": "tidy-applier"}]
        rows = render._mem_event_rows({"recent": events}, term_width=80)
        text = flatten(rows)
        self.assertEqual(len(rows), 2)
        for value in ("/tmp/tmp-first", "/tmp/tmp-second", "기억 저장", "범위를 정해 주세요"):
            self.assertIn(value, text)
        self.assertNotIn("tidy-applier", text)
        self.assertTrue(all(render._dw(flatten([row])) <= 80 for row in rows))
        self.assertIn("출처 미확인", flatten(render._mem_event_rows({"recent": [{"snippet": "내용"}]})))

    def test_equal_changes_count_once_only_with_the_same_observed_source(self):
        event = {"id": "r1", "cwd": "/work/project", "snippet": "선택",
                 "action": "decision-record"}
        rows = render._mem_event_rows({"recent": [event, {**event, "id": "r2"}]})
        self.assertEqual(len(rows), 1)
        self.assertIn("2회", flatten(rows))
        self.assertEqual(render._mem_event_rows({"recent": [event]}, excluded_ids={"r1"}), [])

    def test_section_filter_never_hides_a_memory_change_or_displays_it_twice(self):
        event = {"id": "r1", "cwd": "/work/project", "snippet": "저장된 선택",
                 "action": "decision-record", "scope": "project"}
        observed = {"recent": [event], "by_repo": {"project": [event]}}
        session = Session(harness="claude", pid=1, cwd="/work/project", session_id="s1",
                          slug="fixture", title="fixture", liveness="idle")
        render.set_show_all(True)
        for section in ("fleet", "both", "dispatch"):
            with self.subTest(section=section):
                text = flatten(render._build_lines([session], [], section, False, 0,
                               memory=observed, term_width=120, governor=None))
                self.assertEqual(text.count("저장된 선택"), 1)


if __name__ == "__main__":
    unittest.main()
