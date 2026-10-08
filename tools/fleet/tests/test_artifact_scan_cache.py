"""Fleet inventory reuse must observe ordinary artifact edits and cleanup."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utilities"))
import artifact_reader as reader
from fleet import model
from fleet.collectors import dispatch
from fleet.tests.test_fleet_hot_path import _build_root, CAMP, CYC


class InventoryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _build_root(Path(self.tmp.name) / "reports")
        self.cache = reader.ReadCache()
        self.scan = mock.patch.object(reader.artifact_locator, "scan_index",
                                      wraps=reader.artifact_locator.scan_index).start()
        self.addCleanup(mock.patch.stopall)

    def query(self):
        with reader.read_scope(self.cache):
            first = reader.glob_bucket(self.root, "plans", "*_hot-path")
            self.assertEqual(reader.glob_bucket(self.root, "plans", "*_hot-path"), first)
            return first

    def test_one_scan_per_tick_and_unchanged_tick_reuses_records(self):
        first = self.query()
        self.assertEqual(self.query(), first)
        self.assertEqual(self.scan.call_count, 1)
        # Generated INDEX is neither a prerequisite nor identity authority.
        index = self.root / "campaigns" / "INDEX.json"
        index.write_text("invalid user-edited derived index")
        self.assertEqual(self.query(), first)
        index.unlink()
        self.assertEqual(self.query(), first)

    def test_qa_payload_edits_and_deletion_are_live_without_record_rescan(self):
        plan = self.query()[0]
        state = plan / "pipeline_state.yaml"
        job = {"slug": "hot-path", "artifact_root": str(self.root)}
        for qa in ("light", "thorough"):
            state.write_text("qa_level: " + qa + "\n")
            with reader.read_scope(self.cache):
                self.assertEqual(dispatch.resolve_plan_qa_artifact(job), qa)
        state.unlink()
        with reader.read_scope(self.cache):
            self.assertIsNone(dispatch.resolve_plan_qa_artifact(job))
        shutil.rmtree(plan)
        self.assertEqual(self.query(), [])
        self.assertEqual(self.scan.call_count, 1)

    def test_cycle_and_campaign_rename_delete_and_recreate(self):
        first = self.query()[0]
        cycle = first.parents[2]
        moved = cycle.with_name("ordinary-moved-cycle")
        cycle.rename(moved)
        self.assertEqual(self.query()[0], moved / "artifacts" / "plans" / first.name)
        campaign = moved.parent.parent
        moved_campaign = campaign.with_name("ordinary-moved-campaign")
        campaign.rename(moved_campaign)
        self.assertTrue(self.query()[0].is_relative_to(moved_campaign))
        shutil.rmtree(moved_campaign)
        self.assertEqual(self.query(), [])
        _build_root(self.root)
        self.assertEqual(self.query(), [first])
        self.assertEqual(self.scan.call_count, 5)

    def test_record_edit_replacement_and_delete_invalidate(self):
        record = self.root / ".runtime" / "artifact-producer" / "v1" / "cycles" / (CYC + ".json")
        record.parent.mkdir(parents=True)
        data = {"cycle_id": CYC, "campaign_id": CAMP, "title": "first"}
        record.write_text(json.dumps(data))
        self.query()
        original = record.stat()
        data["title"] = "other"  # same byte length, even with mtime restored
        record.write_text(json.dumps(data))
        os.utime(record, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.query()
        self.assertEqual(self.cache.entries[str(self.root)][1][1][CYC]["title"], "other")
        replacement = record.with_suffix(".new")
        data["title"] = "third"
        replacement.write_text(json.dumps(data))
        replacement.replace(record)
        self.query()
        self.assertEqual(self.cache.entries[str(self.root)][1][1][CYC]["title"], "third")
        record.unlink()
        self.query()
        self.assertNotIn(CYC + ".json", [p.name for p in record.parent.iterdir()])
        self.assertEqual(self.scan.call_count, 4)

    def test_stat_failure_and_concurrent_edit_do_not_retain_stale_cache(self):
        self.query()
        with mock.patch.object(reader, "_record_stamp", side_effect=PermissionError()):
            self.query()
            self.query()
        self.assertEqual(self.scan.call_count, 3)
        self.assertEqual(self.cache.entries, {})
        stamps = reader._record_stamp(self.root)
        with mock.patch.object(reader, "_record_stamp", side_effect=[stamps, {"changed": None}]):
            self.query()
        self.assertEqual(self.cache.entries, {})
        self.query()
        self.assertEqual(self.scan.call_count, 5)

    def test_deleted_bucket_mid_tick_does_not_break_reader(self):
        with reader.read_scope(self.cache):
            bucket = reader.cycle_bucket_dirs(self.root, "plans")[0][0]
            shutil.rmtree(bucket)
            self.assertEqual(reader.glob_bucket(self.root, "plans", "*"), [])
        self.assertEqual(self.query(), [])


class CollectorCacheTest(unittest.TestCase):
    def test_proc_scan_does_not_resolve_plan_qa_before_retention(self):
        with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[
                "123 claude 00:01 claude -p /autopilot-code"]), \
             mock.patch.object(dispatch.procscan, "read_environ", return_value={}), \
             mock.patch.object(dispatch.os, "readlink", return_value="/fixture/owner"), \
             mock.patch.object(dispatch.procscan, "read_proc_start", return_value="123"), \
             mock.patch.object(dispatch, "_claude_job_model", return_value=None), \
             mock.patch.object(dispatch, "resolve_plan_qa_artifact", side_effect=AssertionError("early QA scan")):
            rows = dispatch._scan_processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].qa_source, "default")

    def test_retained_live_row_resolves_qa_and_hidden_dead_row_does_not(self):
        live = model.DispatchJob(key="code", slug="live", source="proc", harness="codex")
        dead = model.DispatchJob(key="code", slug="old", source="proc", harness="codex")
        with mock.patch.object(dispatch, "_scan_processes", return_value=[live, dead]), \
             mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[]), \
             mock.patch.object(dispatch, "_dispatch_liveness", side_effect=["working", "dead"]), \
             mock.patch.object(dispatch, "resolve_plan_qa_artifact", return_value="light") as qa:
            rows = dispatch.collect()
        self.assertEqual(qa.call_count, 1)
        self.assertEqual(qa.call_args.args[0]["slug"], "live")
        self.assertEqual(rows[0].qa, "light")
        self.assertIsNone(rows[1].qa)


if __name__ == "__main__":
    unittest.main()
