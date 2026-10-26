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
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertEqual(result["samples"], [])

        ambiguous = self.root / "ambiguous"
        (ambiguous / "bundle").mkdir(parents=True)
        (ambiguous / "bundle" / "record.json").write_text(
            '{"run_id":"target","other":{"run_id":"other"}}')
        (ambiguous / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([ambiguous], run_id="target", now=20)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertEqual(result["samples"], [])

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

    def test_admission_reader_resumes_fairly_and_selects_exact_run(self):
        root = self.root / "many-bundles"
        for name, run_id, stamp in (("bundle-a", "other", 10),
                                     ("bundle-b", "target", 20)):
            bundle = root / name
            bundle.mkdir(parents=True)
            (bundle / "record.json").write_text(json.dumps({
                "run_id": run_id, "started_at": 0}))
            (bundle / "decision.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": stamp}}))
        cursor = None
        original_scandir = os.scandir
        pass_yields = [0]

        class OrderedScandir:
            def __init__(self, entries, counter):
                self.entries = entries
                self.counter = counter
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index >= len(self.entries):
                    raise StopIteration
                entry = self.entries[self.index]
                self.index += 1
                self.counter[0] += 1
                return entry

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
                    return OrderedScandir(entries, pass_yields)
                with self.subTest(reverse_roots=reverse_roots,
                                  reverse_records=reverse_records), \
                        mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 3), \
                        mock.patch.object(metrics.os, "scandir", ordered_scandir):
                    for _ in range(8):
                        pass_yields[0] = 0
                        result = metrics.read_admission_sources([root], run_id="target",
                            started_at=0, now=30, cursor=cursor)
                        cursor = result["cursor"]
                        for sample in result["samples"]:
                            samples[sample["at"]] = sample
                        self.assertEqual(result["entries"], pass_yields[0])
                        self.assertLessEqual(result["entries"], 3)
                    self.assertEqual(set(samples), {20.0})

    def test_admission_reader_does_not_inherit_partial_binding_after_bundle_replacement(self):
        root = self.root / "partial-replacement"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "01-record.json").write_text(
            '{"run_id":"target","started_at":0}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        with mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
            partial = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20)
            self.assertEqual(partial["coverage"], "partial")
            self.assertEqual(partial["samples"], [])
            partial_state = partial["cursor"]["scan"][str(root)]
            self.assertTrue(partial_state["bundle_cursors"]["bundle"]["last_file"])

            stale = root / "bundle-old"
            bundle.rename(stale)
            for previous_file in stale.iterdir():
                previous_file.unlink()
            stale.rmdir()
            bundle.mkdir()
            (bundle / "01-record.json").write_text(
                '{"run_id":"other","started_at":0}')
            (bundle / "02-decision.json").write_text(
                '{"admitted":false,"snapshot":{"t":15}}')
            changed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=partial["cursor"])
            cursor = changed["cursor"]
            emitted = []
            latest = None
            for _ in range(8):
                page = metrics.read_admission_sources([root], run_id="target",
                    started_at=0, now=20, cursor=cursor)
                emitted.extend(page["samples"])
                latest = page.get("latest") or latest
                cursor = page["cursor"]
        self.assertEqual(changed["samples"], [])
        self.assertIn("admission-bundle-generation-changed", changed["warnings"])
        self.assertEqual(emitted, [])
        self.assertIsNone(latest)

    def test_admission_reader_keeps_binding_when_record_and_decision_are_on_separate_pages(self):
        root = self.root / "split-binding-pages"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        for run_first in (True, False):
            for path in bundle.iterdir():
                path.unlink()
            names = (("01-run.json", "02-decision.json") if run_first
                     else ("02-run.json", "01-decision.json"))
            (bundle / names[0]).write_text('{"run_id":"target","started_at":0}')
            (bundle / names[1]).write_text(
                '{"admitted":false,"snapshot":{"t":10}}')
            with self.subTest(run_first=run_first), \
                    mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                    mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
                first = metrics.read_admission_sources([root], run_id="target",
                    started_at=0, now=20)
                self.assertEqual(first["coverage"], "partial")
                self.assertEqual(first["samples"], [])
                expected_first = min(names)
                self.assertEqual(first["cursor"]["scan"][str(root)]
                    ["bundle_cursors"]["bundle"]["last_file"], expected_first)
                cursor = first["cursor"]
                pages = []
                for _ in range(4):
                    page = metrics.read_admission_sources([root], run_id="target",
                        started_at=0, now=20, cursor=cursor)
                    pages.append(page)
                    cursor = page["cursor"]
                    if page["coverage"] == "observed":
                        break
                emitted = [sample for page in pages for sample in page["samples"]]
                self.assertEqual([sample["state"] for sample in emitted], ["denied"])
                self.assertEqual(pages[-1]["latest"]["state"], "denied")

    def test_admission_reader_invalidates_replaced_foreign_binding_file(self):
        root = self.root / "replaced-binding-file"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        binding = bundle / "01-run.json"
        binding.write_text('{"run_id":"target","started_at":0}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        with mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
            partial = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20)
            binding.write_text('{"run_id":"foreign","started_at":0}')
            changed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=partial["cursor"])
            resolved = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=changed["cursor"])
        self.assertIn("admission-bundle-generation-changed", changed["warnings"])
        self.assertEqual(changed["samples"], [])
        self.assertEqual(resolved["coverage"], "unknown")
        self.assertEqual(resolved["samples"], [])
        self.assertIsNone(resolved["latest"])

    def test_admission_reader_keeps_decision_pages_out_of_binding_state(self):
        root = self.root / "bounded-binding-summary"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "01-run.json").write_text(
            '{"run_id":"target","started_at":0}')
        for index in range(8):
            (bundle / f"{index + 2:02d}-decision.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": 10 + index}}))
        result = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=30)
        self.assertEqual(result["coverage"], "observed")
        self.assertNotIn("admission-binding-summary-limit", result["warnings"])
        self.assertEqual(len(result["samples"]), 8)
        binding = (result["cursor"]["scan"][str(root)]["bundle_cursors"]["bundle"]
                   ["binding_state"])
        self.assertNotIn("decisions", binding)

    def test_admission_reader_observes_320_separate_decisions_across_bounded_pages(self):
        root = self.root / "320-separate-admission-files"
        bundle = root / "one-bundle"
        bundle.mkdir(parents=True)
        (bundle / "run-record.json").write_text(json.dumps({
            "run_id": "run-320", "started_at": 1, "row_id": "row-320"}))
        for index in range(320):
            admitted = index < 9
            (bundle / f"admission-{index:03d}.json").write_text(json.dumps({
                "row_id": "row-320",
                "admitted": admitted,
                "snapshot": {"t": 100 + index},
                "reasons": ([] if admitted else ["evaluation GPU capacity is occupied"]),
                "preceding_evaluation_leases": ([] if admitted else [{
                    "task": "evaluation", "host": "gpu-node-a", "gpu": 0,
                    "owner": "evaluation-queue-17"}]),
            }))

        input_bytes = sum(path.stat().st_size for path in bundle.iterdir())
        self.assertLess(input_bytes, metrics.MAX_ADMISSION_PENDING_BYTES)

        def assert_complete_observation(result):
            self.assertEqual(result["coverage"], "observed")
            self.assertNotIn("admission-binding-summary-limit", result["warnings"])
            self.assertEqual(len(result["samples"]), 320)
            denied = [sample for sample in result["samples"]
                      if sample["state"] == "denied"]
            admitted = [sample for sample in result["samples"]
                        if sample["state"] == "admitted"]
            self.assertEqual((len(denied), len(admitted)), (311, 9))
            self.assertEqual(len(denied) / len(result["samples"]), 311 / 320)
            latest = result["latest"]
            self.assertEqual((latest["state"], latest["at"]), ("denied", 419.0))
            self.assertEqual(latest["reasons"], ["evaluation GPU capacity is occupied"])
            self.assertEqual(latest["priority_blockers"], [{
                "task": "evaluation", "host": "gpu-node-a", "gpu": 0,
                "owner": "evaluation-queue-17"}])
            for key in ("progress", "rate", "eta", "eta_seconds",
                        "wait_duration_seconds", "wait_seconds", "time_ratio"):
                self.assertNotIn(key, result)
            for sample in result["samples"]:
                for key in ("progress", "rate", "eta", "eta_seconds",
                            "wait_duration_seconds", "wait_seconds", "time_ratio"):
                    self.assertNotIn(key, sample)

        default = metrics.read_admission_sources([root], run_id="run-320",
            row_key="row-320", started_at=1, now=500)
        self.assertEqual(default["bytes"], input_bytes)
        self.assertLessEqual(default["bytes"], metrics.MAX_ADMISSION_BYTES_PER_PASS)
        assert_complete_observation(default)

        cursor = None
        paged = None
        accumulated = {}
        saw_partial_page = False
        page_byte_limit = 12 * 1024
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", page_byte_limit):
            for _ in range(32):
                paged = metrics.read_admission_sources([root], run_id="run-320",
                    row_key="row-320", started_at=1, now=500, cursor=cursor)
                self.assertLessEqual(paged["bytes"], page_byte_limit)
                accumulated.update({row["sample_id"]: row for row in paged["samples"]})
                scan_state = paged["cursor"]["scan"].get(str(root), {})
                binding_state = scan_state.get("bundle_cursors", {}).get(
                    "one-bundle", {}).get("binding_state")
                if binding_state is not None:
                    summary_bytes = len(json.dumps(binding_state, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                    self.assertLessEqual(summary_bytes, metrics.MAX_ADMISSION_PENDING_BYTES)
                    self.assertLessEqual(len(binding_state["source_files"]),
                                         metrics.MAX_ADMISSION_SOURCE_FINGERPRINTS)
                    self.assertNotIn("decisions", binding_state)
                if paged["coverage"] == "observed":
                    break
                self.assertEqual(paged["coverage"], "partial")
                saw_partial_page = True
                cursor = paged["cursor"]
        self.assertTrue(saw_partial_page)
        paged_complete = {**paged, "samples": list(accumulated.values())}
        assert_complete_observation(paged_complete)

    def test_rootless_target_survives_poisoned_cursor_large_changing_sibling(self):
        root = self.root / "registry-parent-without-output-root"
        registry = root / "resource-runs.json"
        target = root / "arbitrary-target-child"
        sibling = root / "old-unbound-child"
        target.mkdir(parents=True)
        sibling.mkdir()
        registry.write_text("{}\n")
        for index in range(320):
            (sibling / f"decision-{index:03d}.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": index + 1}}))
        (sibling / "notes.md").write_text("[not an admission document]\n")
        (sibling / "unrelated.json").write_text(
            '{"padding":"' + "x" * (metrics.MAX_ADMISSION_RECORD_BYTES + 64) + '"}')
        for index in range(320):
            (target / f"decision-{index:03d}.json").write_text(json.dumps({
                "row_id": "exact-row", "admitted": index >= 9,
                "snapshot": {"t": 1000 + index}}))
        (target / "zz-identity.data").write_text(json.dumps({
            "run_id": "exact-run", "row_id": "exact-row", "started_at": 1,
            "config_sha256": "a" * 64}))
        root_info = root.lstat()
        old_cursor = {"schema_version": 3,
            "root_generations": {str(root): f"{root_info.st_dev}:{root_info.st_ino}"},
            "scan": {str(root): {"root_blocked_generation":
                metrics._admission_stat_token(root_info), "last_bundle": sibling.name,
                "last_file": "decision-255.json", "blocked_bundles": {
                    target.name: "poisoned-generation"}}}, "sample_ids": []}
        cursor = old_cursor
        accumulated = {}
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 12 * 1024):
            for turn in range(64):
                changing = sibling / "decision-000.json"
                changing.write_text(json.dumps({"admitted": False,
                    "snapshot": {"t": 1500 + turn}}))
                result = metrics.read_admission_sources([root], run_id="exact-run",
                    row_key="exact-row", registry_path=registry, started_at=1,
                    now=2000, cursor=cursor)
                self.assertLessEqual(result["bytes"], 12 * 1024)
                self.assertLessEqual(result["entries"], metrics.MAX_ADMISSION_ENTRIES)
                cursor = result["cursor"]
                accumulated.update({row["sample_id"]: row for row in result["samples"]})
                if result["coverage"] == "observed":
                    break
        self.assertEqual(len(accumulated), 320)
        self.assertEqual((result["latest"]["state"], result["latest"]["at"]),
                         ("admitted", 1319.0))
        self.assertNotIn("admission-binding-summary-limit", result["warnings"])
        self.assertNotIn("root_blocked_generation", cursor["scan"][str(root)])

    def test_validated_target_continues_without_broad_root_listing(self):
        root = self.root / "root-with-unrelated-siblings"
        target = root / "arbitrary-target"
        target.mkdir(parents=True)
        (target / "identity.data").write_text(
            '{"run_id":"direct-run","started_at":1}')
        (target / "decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":100}}')
        for index in range(12):
            (root / f"changing-sibling-{index:02d}").mkdir()
        first = metrics.read_admission_sources([root], run_id="direct-run",
            started_at=1, now=110)
        self.assertEqual(first["coverage"], "observed")
        original_scandir = os.scandir

        def unavailable_root_listing(path):
            if Path(path) == root:
                raise OSError("unrelated broad listing is unavailable")
            return original_scandir(path)

        with mock.patch.object(metrics.os, "scandir", unavailable_root_listing):
            continued = metrics.read_admission_sources([root], run_id="direct-run",
                started_at=1, now=111, cursor=first["cursor"])
        self.assertEqual(continued["coverage"], "observed")
        self.assertEqual(continued["latest"]["state"], "denied")
        self.assertEqual(continued["samples"], [])

    def test_multiple_exact_target_bundles_are_unknown_until_one_remains(self):
        root = self.root / "duplicate-exact-bindings"
        for name, admitted, stamp in (("first", False, 10), ("second", True, 11)):
            bundle = root / name
            bundle.mkdir(parents=True)
            (bundle / "identity.data").write_text(json.dumps({
                "run_id": "target-run", "started_at": 1}))
            (bundle / "decision.json").write_text(json.dumps({
                "admitted": admitted, "snapshot": {"t": stamp}}))
        ambiguous = metrics.read_admission_sources([root], run_id="target-run",
            started_at=1, now=12)
        self.assertEqual(ambiguous["coverage"], "unknown")
        self.assertIsNone(ambiguous["latest"])
        self.assertEqual({row["state"] for row in ambiguous["samples"]}, {"unknown"})
        self.assertNotEqual(ambiguous["generation"], None)

        for path in (root / "second").iterdir():
            path.unlink()
        (root / "second").rmdir()
        resolved = metrics.read_admission_sources([root], run_id="target-run",
            started_at=1, now=13, cursor=ambiguous["cursor"])
        self.assertEqual(resolved["coverage"], "observed")
        self.assertEqual(resolved["latest"]["state"], "denied")
        self.assertEqual([row["state"] for row in resolved["samples"]], ["denied"])

    def test_admission_oversized_root_and_child_lists_retry_partial_inventory(self):
        original_scandir = os.scandir

        class CountingScandir:
            def __init__(self, entries, counter):
                self.entries = entries
                self.counter = counter
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index >= len(self.entries):
                    raise StopIteration
                entry = self.entries[self.index]
                self.index += 1
                self.counter[0] += 1
                return entry

        for listing_kind in ("root", "child"):
            for reverse in (False, True):
                root = self.root / f"oversized-{listing_kind}-{reverse}"
                if listing_kind == "root":
                    scan_path = root
                    root.mkdir()
                    for index in range(5):
                        (root / f"entry-{index}.json").write_text("{}")
                else:
                    bundle = root / "bundle"
                    bundle.mkdir(parents=True)
                    scan_path = bundle
                    for index in range(5):
                        (bundle / f"entry-{index}.json").write_text("{}")
                calls = {}
                yielded = [0]

                def ordered_scandir(path):
                    path = Path(path)
                    calls[str(path)] = calls.get(str(path), 0) + 1
                    entries = list(original_scandir(path))
                    entries.sort(key=lambda entry: entry.name, reverse=reverse)
                    return CountingScandir(entries, yielded)

                cursor = None
                results = []
                with self.subTest(listing_kind=listing_kind, reverse=reverse), \
                        mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 3), \
                        mock.patch.object(metrics.os, "scandir", ordered_scandir):
                    for _ in range(4):
                        yielded[0] = 0
                        result = metrics.read_admission_sources([root], run_id="target",
                            now=20, cursor=cursor)
                        cursor = result["cursor"]
                        results.append(result)
                        self.assertEqual(result["entries"], yielded[0])
                        self.assertLessEqual(yielded[0], 3)

                expected_warning = ("admission-root-listing-limit"
                    if listing_kind == "root" else "admission-bundle-listing-limit")
                self.assertTrue(any(expected_warning in item["warnings"] for item in results))
                self.assertEqual(results[-1]["coverage"], "partial")
                self.assertGreater(calls.get(str(scan_path), 0), 1)
                for item in results:
                    root_state = item["cursor"]["scan"].get(str(root), {})
                    self.assertNotIn("root_blocked_generation", root_state)
                    self.assertFalse(root_state.get("blocked_bundles"))

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

    def test_older_timestamp_in_a_different_file_is_not_a_counter_regression(self):
        root = self.root / "historical-file-order"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "run.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "a-newer.json").write_text(
            '{"admitted":false,"snapshot":{"t":100}}')
        (bundle / "z-older.json").write_text(
            '{"admitted":true,"snapshot":{"t":90}}')
        result = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        self.assertEqual(result["coverage"], "observed")
        self.assertEqual({sample["state"] for sample in result["samples"]},
                         {"admitted", "denied"})
        self.assertEqual(result["latest"]["at"], 100.0)

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
        self.assertEqual(damaged["latest"]["state"], "unknown")
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

    def test_transient_slow_listing_recovers_on_same_generation(self):
        root = self.root / "slow-listing-recovery"
        bundle = root / "child"
        bundle.mkdir(parents=True)
        registry = root / "resource-runs.json"
        registry.write_text("{}\n")
        (bundle / "01-identity.json").write_text(
            '{"run_id":"slow-run","started_at":1}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        ticks = [0]

        def temporarily_slow_clock():
            ticks[0] += 1
            return 0.0 if ticks[0] <= 5 else 1.0

        partial = metrics.read_admission_sources([root], run_id="slow-run",
            registry_path=registry, started_at=1, now=20,
            monotonic=temporarily_slow_clock, time_budget=0.25)
        self.assertEqual(partial["coverage"], "partial")
        self.assertNotIn("root_blocked_generation", partial["cursor"]["scan"][str(root)])
        recovered = metrics.read_admission_sources([root], run_id="slow-run",
            registry_path=registry, started_at=1, now=20, cursor=partial["cursor"])
        self.assertEqual(recovered["coverage"], "observed")
        self.assertEqual(recovered["latest"]["state"], "denied")
        self.assertEqual([row["state"] for row in recovered["samples"]], ["denied"])


if __name__ == "__main__":
    unittest.main()
