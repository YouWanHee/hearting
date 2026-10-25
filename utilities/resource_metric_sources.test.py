#!/usr/bin/env python3
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import resource_metric_sources as metrics


def vi(value):
    out = bytearray()
    while value > 0x7f:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, wire, value):
    tag = vi(number * 8 + wire)
    if wire == 0:
        return tag + vi(value)
    if wire == 2:
        return tag + vi(len(value)) + value
    return tag + value


def event(step, tag, tensor, dtype=1):
    shape = b""  # TensorShapeProto with no dimensions: scalar.
    tensor_proto = field(1, 0, dtype) + field(2, 2, shape) + tensor
    value = field(1, 2, tag.encode()) + field(8, 2, tensor_proto)
    summary = field(1, 2, value)
    return field(2, 0, step) + field(5, 2, summary)


class MetricSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fixed_json_sources_decode_and_keep_progress_separate(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "epochs.jsonl").write_text(
            '{"epoch":1,"split":"valid","losses":{"L_se":-9.0}}\n'
            '{"epoch":2,"train":{"losses":{"loss":0.4}},"valid":{"losses":{"loss":0.3}}}\n'
            '{"step":2,"split":"train","name":"loss","value":0.5}\n')
        (self.root / "valid-epoch-0031.json").write_text(
            json.dumps({"epoch": 31, "losses": {"L_se": -8.77, "SISNRi": 14.62},
                        "seconds": 848.9}))
        (self.root / "train-epoch-0031.json").write_text(
            json.dumps({"epoch": 31, "losses": {"L_se": -9.1}, "seconds": 800.0}))
        (self.root / "progress.json").write_text(
            '{"reached_epoch":31,"total_epochs":100,"best_updated":true}')
        value = metrics.read_sources(self.root)
        valid = next(row for row in value["samples"]
                     if row["split"] == "valid" and row["epoch"] == 31)
        self.assertEqual(valid["metrics"], {"L_se": -8.77, "SISNRi": 14.62})
        self.assertEqual(valid["elapsed_seconds"], 848.9)
        self.assertEqual(value["progress"]["total_epochs"], 100)
        self.assertEqual({row["split"] for row in value["samples"]}, {"valid", "train"})
        self.assertEqual(next(row for row in value["samples"]
                              if row["split"] == "valid" and row["epoch"] == 2)["metrics"],
                         {"loss": 0.3})
        self.assertEqual(next(row for row in value["samples"]
                              if row["split"] == "valid" and row["epoch"] == 1)["format"],
                         "epochs-jsonl")
        self.assertIn("epoch-json-files", {row["format"] for row in value["sources"]})
        self.assertNotIn("metrics.jsonl", [row["path"] for row in value["sources"]])

    def test_jsonl_partial_tail_is_not_consumed_until_newline(self):
        logs = self.root / "logs"
        logs.mkdir()
        path = logs / "epochs.jsonl"
        path.write_bytes(b'{"step":1,"split":"valid","name":"loss","value":2}\n'
                         b'{"step":2,"split":"valid","name":"loss","value":1}')
        samples, cursor, warnings, paths = metrics.decode_jsonl(self.root)
        self.assertEqual([item["epoch"] for item in samples], [1])
        self.assertIn("epochs-jsonl-partial-record", warnings)
        path.write_bytes(path.read_bytes() + b"\n")
        samples2, cursor2, _, _ = metrics.decode_jsonl(self.root, cursor)
        self.assertEqual([item["epoch"] for item in samples2], [2])
        self.assertGreater(cursor2["offset"], cursor["offset"])

    def test_tensorboard_bytes_decode_simple_and_tensorproto_scalars(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        rows = [
            metrics.encode_tfrecord(event(4, "valid/L_se", field(5, 5, struct.pack("<f", -8.5)))),
            metrics.encode_tfrecord(event(5, "train/accuracy", field(5, 5, struct.pack("<f", 0.75)))),
            metrics.encode_tfrecord(event(6, "valid/score", field(6, 1, struct.pack("<d", 12.25)), 2)),
            metrics.encode_tfrecord(event(7, "valid/count", field(10, 0, 42), 9)),
            metrics.encode_tfrecord(event(8, "valid/content", field(4, 2, struct.pack("<f", 3.5)))),
        ]
        path.write_bytes(b"".join(rows))
        samples, warnings, paths, _cursor = metrics.decode_tfevents(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(len(samples), 5)
        values = {(row["epoch"], row["split"], next(iter(row["metrics"].items())))
                  for row in samples}
        self.assertIn((4, "valid", ("L_se", -8.5)), values)
        self.assertIn((5, "train", ("accuracy", 0.75)), values)
        self.assertIn((6, "valid", ("score", 12.25)), values)
        self.assertIn((7, "valid", ("count", 42.0)), values)
        self.assertIn((8, "valid", ("content", 3.5)), values)
        self.assertEqual(paths, [str(path)])

    def test_crc_bad_record_and_symlinked_source_are_reported(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        raw = bytearray(metrics.encode_tfrecord(event(1, "valid/loss",
                                      field(5, 5, struct.pack("<f", 1.0)))))
        raw[-1] ^= 1
        path.write_bytes(raw)
        samples, warnings, _, _cursor = metrics.decode_tfevents(self.root)
        self.assertEqual(samples, [])
        self.assertIn("tfevents-data-crc-invalid", warnings)
        path.unlink()
        outside = self.root / "outside"
        outside.write_text('{"step":1,"split":"valid","name":"loss","value":1}\n')
        (self.root / "logs").symlink_to(self.root)
        _, _, warnings, _ = metrics.decode_jsonl(self.root)
        self.assertTrue(warnings)

    def test_tfevents_cursor_resumes_partial_record_and_resets_on_rotation(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        first = metrics.encode_tfrecord(event(1, "valid/loss", field(5, 5, struct.pack("<f", 2.0))))
        second = metrics.encode_tfrecord(event(2, "valid/loss", field(5, 5, struct.pack("<f", 1.0))))
        path.write_bytes(first + second[:8])
        samples, warnings, _, cursor = metrics.decode_tfevents(self.root)
        self.assertEqual([row["epoch"] for row in samples], [1])
        self.assertIn("tfevents-partial-record", warnings)
        path.write_bytes(first + second)
        samples, _, _, cursor = metrics.decode_tfevents(self.root, cursor)
        self.assertEqual([row["epoch"] for row in samples], [2])
        replacement = path.with_suffix(".rotated")
        replacement.write_bytes(first)
        replacement.replace(path)
        samples, warnings, _, reset_cursor = metrics.decode_tfevents(self.root, cursor)
        self.assertEqual([row["epoch"] for row in samples], [1])
        self.assertTrue(any("generation-changed" in warning for warning in warnings))
        self.assertEqual(reset_cursor[path.name]["offset"], len(first))

    def test_admission_reader_binds_run_content_and_deduplicates_snapshots_and_leases(self):
        root = self.root / "registry-parent"
        bundle = root / "arbitrary-bundle-name"
        bundle.mkdir(parents=True)
        registry = root / "registry.json"
        registry.write_text("{}\n")
        (bundle / "execution-record.data").write_text(json.dumps({
            "run_id": "run-a", "started_at": 50, "row_id": "row-a"}))
        admission = {
            "row_id": "row-a",
            "admitted": False,
            "snapshot": {"t": 100},
            "first_snapshot": {"t": 20},
            "reasons": ["gpu capacity", "gpu capacity"],
            "preceding_evaluation_leases": [
                {"task": "evaluation", "host": "host-a", "gpu": 0},
                {"task": "evaluation", "host": "host-a", "gpu": 0},
                {"kind": "shared_admission", "task": "shared"},
            ],
        }
        (bundle / "unrelated-file-prefix").write_text(json.dumps(admission))
        result = metrics.read_admission_sources([root], run_id="run-a", row_key="row-a",
            registry_path=registry, started_at=50, now=110)
        self.assertEqual(result["coverage"], "observed")
        sample, = result["samples"]
        self.assertEqual((sample["state"], sample["at"]), ("denied", 100.0))
        self.assertEqual(sample["reasons"], ["gpu capacity"])
        self.assertEqual(sample["priority_blockers"], [
            {"task": "evaluation", "host": "host-a", "gpu": 0}])
        self.assertNotIn("wait_duration_seconds", sample)
        self.assertNotIn("time_ratio", sample)
        replay = metrics.read_admission_sources([root], run_id="run-a", row_key="row-a",
            registry_path=registry, started_at=50, now=111, cursor=result["cursor"])
        self.assertEqual(replay["samples"], [])
        self.assertEqual(replay["latest"]["sample_id"], sample["sample_id"])

    def test_row_query_does_not_inherit_run_wide_decision_from_execution_record(self):
        root = self.root / "row-with-run-wide-decision"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "execution.json").write_text(json.dumps({
            "run_id": "run-1", "row_id": "row-1", "started_at": 0}))
        (bundle / "admission.json").write_text(json.dumps({
            "run_id": "run-1", "admitted": False, "snapshot": {"t": 20},
            "reasons": ["run-wide capacity"]}))

        row = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=21)
        self.assertEqual(row["coverage"], "observed")
        self.assertEqual(row["latest"]["state"], "unknown")
        self.assertEqual([sample["state"] for sample in row["samples"]], ["unknown"])
        self.assertEqual(row["latest"]["reasons"], [])

        run = metrics.read_admission_sources([root], run_id="run-1", started_at=0, now=21)
        self.assertEqual(run["coverage"], "observed")
        self.assertEqual(run["latest"]["state"], "denied")
        self.assertEqual([sample["state"] for sample in run["samples"]], ["denied"])
        self.assertEqual(run["latest"]["reasons"], ["run-wide capacity"])

    def test_row_query_accepts_decision_with_its_own_exact_row_identity(self):
        root = self.root / "exact-row-decision"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "admission.json").write_text(json.dumps({
            "run_id": "run-1", "row_id": "row-1", "admitted": False,
            "snapshot": {"t": 20}}))

        row = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=21)
        self.assertEqual(row["coverage"], "observed")
        self.assertEqual(row["latest"]["state"], "denied")
        self.assertEqual(len(row["samples"]), 1)

    def test_row_queries_select_each_decision_identity_from_one_bundle(self):
        root = self.root / "multiple-row-decisions"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "admissions.json").write_text(json.dumps([
            {"run_id": "run-1", "row_id": "row-1", "admitted": False,
             "snapshot": {"t": 20}},
            {"run_id": "run-1", "row_id": "row-2", "admitted": True,
             "snapshot": {"t": 21}},
        ]))

        first = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=22)
        second = metrics.read_admission_sources([root], run_id="run-1", row_key="row-2",
            started_at=0, now=22)
        self.assertEqual((first["latest"]["state"], len(first["samples"])), ("denied", 1))
        self.assertEqual((second["latest"]["state"], len(second["samples"])), ("admitted", 1))
        self.assertNotEqual(first["latest"]["sample_id"], second["latest"]["sample_id"])

    def test_admission_reader_rejects_wrong_foreign_and_ambiguous_bindings(self):
        wrong = self.root / "wrong"
        (wrong / "bundle").mkdir(parents=True)
        (wrong / "bundle" / "record.json").write_text('{"run_id":"other"}')
        (wrong / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([wrong], run_id="target", now=20)
        self.assertEqual((result["coverage"], result["latest"]), ("unknown", None))

        ambiguous = self.root / "ambiguous"
        (ambiguous / "bundle").mkdir(parents=True)
        (ambiguous / "bundle" / "record.json").write_text(
            '{"run_id":"target","other":{"run_id":"other"}}')
        (ambiguous / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([ambiguous], run_id="target", now=20)
        self.assertEqual((result["coverage"], result["latest"]), ("unknown", None))

        foreign_row = self.root / "foreign-row"
        (foreign_row / "bundle").mkdir(parents=True)
        (foreign_row / "bundle" / "record.json").write_text(
            '{"run_id":"target","row_id":"row-other","started_at":0}')
        (foreign_row / "bundle" / "admission.json").write_text(
            '{"admitted":false,"row_id":"row-other","snapshot":{"t":10}}')
        result = metrics.read_admission_sources([foreign_row], run_id="target",
            row_key="row-target", started_at=0, now=20)
        self.assertEqual((result["coverage"], result["latest"]), ("unknown", None))

    def test_admission_reader_reports_invalid_links_and_json_as_partial_unknown(self):
        root = self.root / "malformed"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "duplicate-keys.json").write_text(
            '{"admitted":false,"admitted":true,"snapshot":{"t":10}}')
        (bundle / "nonfinite.json").write_text(
            '{"admitted":false,"snapshot":{"t":NaN}}')
        result = metrics.read_admission_sources([root], run_id="target", started_at=0, now=20)
        self.assertEqual(result["coverage"], "partial")
        self.assertIsNone(result["latest"])
        self.assertTrue(any("duplicate-key" in warning for warning in result["warnings"]))
        self.assertTrue(any("nonfinite" in warning for warning in result["warnings"]))
        link_root = self.root / "link-root"
        link_root.symlink_to(root, target_is_directory=True)
        linked = metrics.read_admission_sources([link_root], run_id="target", now=20)
        self.assertEqual(linked["coverage"], "partial")
        self.assertTrue(any("symlink" in warning for warning in linked["warnings"]))

    def test_admission_reader_resumes_after_entry_budget_and_observes_both_bundles(self):
        root = self.root / "many-bundles"
        for name, stamp in (("bundle-a", 10), ("bundle-b", 20)):
            bundle = root / name
            bundle.mkdir(parents=True)
            (bundle / "record.json").write_text(json.dumps({"run_id": "target", "started_at": 0}))
            (bundle / "decision.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": stamp}}))
        cursor = None
        original_scandir = os.scandir

        class OrderedScandir:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *_exc):
                return False

        for reverse_roots in (False, True):
            for reverse_records in (False, True):
                cursor = None
                samples = {}
                def ordered_scandir(path):
                    entries = list(original_scandir(path))
                    path = Path(path)
                    reverse = ((path == root and reverse_roots)
                               or (path.parent == root and reverse_records))
                    entries.sort(key=lambda entry: entry.name, reverse=reverse)
                    return OrderedScandir(entries)
                with self.subTest(reverse_roots=reverse_roots,
                                  reverse_records=reverse_records), \
                        mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 3), \
                        mock.patch.object(metrics.os, "scandir", ordered_scandir):
                    for _ in range(4):
                        result = metrics.read_admission_sources([root], run_id="target",
                            started_at=0, now=30, cursor=cursor)
                        cursor = result["cursor"]
                        for sample in result["samples"]:
                            samples[sample["at"]] = sample
                        self.assertLessEqual(result["entries"], 3)
                    self.assertEqual(set(samples), {10.0, 20.0})

    def test_admission_reader_does_not_inherit_partial_binding_after_bundle_replacement(self):
        root = self.root / "partial-replacement"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "01-record.json").write_text(
            '{"run_id":"target","started_at":0}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        with mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 2):
            partial = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20)
            self.assertEqual(partial["coverage"], "partial")
            self.assertEqual(partial["samples"], [])

            stale = root / "bundle-old"
            bundle.rename(stale)
            bundle.mkdir()
            (bundle / "01-record.json").write_text(
                '{"run_id":"other","started_at":0}')
            (bundle / "02-decision.json").write_text(
                '{"admitted":false,"snapshot":{"t":15}}')
            changed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=partial["cursor"])
        self.assertEqual(changed["samples"], [])
        self.assertIsNone(changed["latest"])
        self.assertIn("admission-bundle-generation-changed", changed["warnings"])

    def test_admission_reader_keeps_backward_time_unknown_until_new_valid_sample(self):
        root = self.root / "time-order"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision = bundle / "decision.json"
        decision.write_text('{"admitted":false,"snapshot":{"t":100}}')
        first = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        self.assertEqual(first["latest"]["state"], "denied")
        import shutil
        shutil.rmtree(bundle)
        vanished = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=first["cursor"])
        self.assertIsNone(vanished["latest"])
        bundle.mkdir()
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision.write_text('{"admitted":false,"snapshot":{"t":100}}')
        reappeared = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=vanished["cursor"])
        self.assertEqual(reappeared["latest"]["state"], "unknown")
        decision.write_text('{"admitted":true,"snapshot":{"t":50}}')
        backward = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110, cursor=reappeared["cursor"])
        self.assertEqual(backward["latest"]["state"], "unknown")
        repeat = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=backward["cursor"])
        self.assertEqual(repeat["latest"]["state"], "unknown")
        replacement = bundle / "replacement.json"
        replacement.write_text('{"admitted":true,"snapshot":{"t":120}}')
        replacement.replace(decision)
        recovered = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=121, cursor=repeat["cursor"])
        self.assertEqual(recovered["latest"]["state"], "admitted")
        self.assertIsNone(recovered["cursor"]["uncertainty_reason"])

    def test_admission_reader_rejects_boolean_future_oversized_and_linked_records(self):
        root = self.root / "invalid-samples"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision = bundle / "decision.json"
        decision.write_text('{"admitted":false,"snapshot":{"t":true}}')
        boolean = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=100)
        self.assertEqual(boolean["latest"]["state"], "unknown")
        decision.write_text('{"admitted":false,"snapshot":{"t":1000}}')
        future = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=100, cursor=boolean["cursor"])
        self.assertEqual(future["latest"]["state"], "unknown")
        decision.write_text('{"admitted":true,"snapshot":{"t":110}}')
        valid = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=future["cursor"])
        self.assertEqual(valid["latest"]["state"], "admitted")

        outside = self.root / "outside.json"
        outside.write_text('{"admitted":true,"snapshot":{"t":111}}')
        (bundle / "linked.json").symlink_to(outside)
        (bundle / "oversized.json").write_text('{"padding":"' + "x" * 70000 + '"}')
        try:
            os.mkfifo(bundle / "device.json")
        except (AttributeError, NotImplementedError, OSError):
            pass
        damaged = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=valid["cursor"])
        self.assertEqual(damaged["coverage"], "partial")
        self.assertIsNone(damaged["latest"])
        self.assertTrue(any("symlink" in warning for warning in damaged["warnings"]))
        self.assertTrue(any("unbounded" in warning for warning in damaged["warnings"]))

    def test_admission_reader_obeys_time_and_byte_budgets(self):
        root = self.root / "bounded"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "decision.json").write_text(
            json.dumps({"admitted": False, "snapshot": {"t": 10}, "padding": "x" * 400}))
        clock = [0.0]
        def monotonic():
            clock[0] += 0.0002
            return clock[0]
        timed = metrics.read_admission_sources([root], run_id="target", started_at=0,
            now=20, monotonic=monotonic, time_budget=0.0008)
        self.assertEqual(timed["coverage"], "partial")
        self.assertLessEqual(timed["entries"], metrics.MAX_ADMISSION_ENTRIES)
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 128):
            sized = metrics.read_admission_sources([root], run_id="target", started_at=0, now=20)
        self.assertLessEqual(sized["bytes"], 128)
        self.assertEqual(sized["coverage"], "partial")


if __name__ == "__main__":
    unittest.main()
