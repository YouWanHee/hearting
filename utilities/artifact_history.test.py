#!/usr/bin/env python3
"""Tests for `artifact_history.py`: the one recorder's line format, append-only publishing, and replay.

Everything runs in temporary roots; nothing touches a real artifact root.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_history as H  # noqa: E402

TXN = "htxn_" + "5" * 32
EXAMPLE = {"schema_version": 1, "contract": "artifact-history/v1", "event_id": "hevt_" + "4" * 32,
           "transaction_id": TXN, "at": "2026-10-01T09:00:00Z",
           "actor": {"by": "agent", "session": "att-3521eef711234e77a156c5d972e7f628"}, "kind": "meta",
           "target": {"type": "campaign", "id": "camp_" + "2" * 32, "path": "campaigns/2026-10-01_example/meta.json"},
           "operation": "update", "field": "campaign.title", "before": {"value": "명령어 모델 실험"},
           "after": {"value": "명령어 모델 집 적응 비교"}, "reason": "사용자 요청에 따른 제목 수정"}
NOW = 1_790_000_000.0
MONTH_2 = 1_793_000_000.0  # a later UTC month


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def event(**over):
    base = dict(kind="meta", target_type="campaign", target_id="camp_" + "2" * 32,
                target_path="campaigns/x/meta.json", operation="update", field="campaign.title",
                before=H.value_ref("a"), after=H.value_ref("b"), reason="테스트", actor_by="human",
                transaction_id=TXN, now=NOW)
    base["field"] = over.pop("field_name", base["field"])
    base.update(over)
    return H.make_event(**base)


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir()

    def locked(self):
        outer = self

        class Lock:
            def __enter__(self):
                self.fd = adm._acquire_lock(outer.root.resolve(), 5)

            def __exit__(self, *exc):
                adm._release_lock(outer.root.resolve(), self.fd)

        return Lock()

    def files(self):
        base = self.root / H.HISTORY_REL
        return sorted(p for p in base.rglob("*.jsonl")) if base.exists() else []


class EventFormatTest(HistoryBase):
    def test_example_line_is_exactly_the_published_example(self):
        validated = H.validate_event(json.loads(json.dumps(EXAMPLE)))
        line = H.event_bytes(validated).decode("utf-8")
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), EXAMPLE)
        self.assertEqual(list(json.loads(line)), list(H.KEYS))  # key order is part of the format

    def test_small_values_are_kept_and_large_ones_become_digest_and_size(self):
        small = H.value_ref({"k": "가" * 100})
        self.assertEqual(small, {"value": {"k": "가" * 100}})
        big_value = "가" * 400  # 1200 UTF-8 bytes + quotes
        big = H.value_ref(big_value)
        raw = json.dumps(big_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(big, {"digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        edge = "a" * 510  # canonical bytes: 510 + 2 quotes = 512 -> still a value
        self.assertIn("value", H.value_ref(edge))
        self.assertIn("digest", H.value_ref("a" * 511))
        self.assertEqual(H.value_ref(None), {"value": None})

    def test_file_bytes_are_always_a_digest(self):
        self.assertEqual(H.file_ref(b"x"), {"digest": "sha256:" + hashlib.sha256(b"x").hexdigest(), "bytes": 1})
        self.assertEqual(H.file_ref(None), {"value": None})

    def test_every_kind_and_target_type_is_accepted_by_the_one_function(self):
        for kind, target_type in (("meta", "campaign"), ("meta", "cycle"), ("meta", "project"), ("group", "group"),
                                  ("flow", "flow"), ("artifact", "artifact")):
            for operation in ("add", "update", "move", "delete"):
                made = event(kind=kind, target_type=target_type, operation=operation,
                             before=H.file_ref(b"old") if kind == "artifact" else H.value_ref(1),
                             after=H.file_ref(b"new") if kind == "artifact" else H.value_ref(2))
                self.assertEqual((made["kind"], made["target"]["type"], made["operation"]),
                                 (kind, target_type, operation))

    def test_session_is_optional_and_actor_is_closed(self):
        self.assertIsNone(event()["actor"]["session"])
        self.assertEqual(event(session="att-abc")["actor"], {"by": "human", "session": "att-abc"})
        for bad in ({"actor_by": "robot"}, {"session": "bad session"}, {"session": ""}):
            with self.assertRaises(H.HistoryError):
                event(**bad)

    def test_invalid_events_are_refused(self):
        for bad in (dict(kind="note"), dict(target_type="folder"), dict(operation="rename"),
                    dict(target_path="/abs/meta.json"), dict(target_path="a/../b"), dict(reason=""),
                    dict(transaction_id="txn_1"), dict(event_id="evt_1"), dict(field="")):
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                event(**bad)
        loose = dict(EXAMPLE, extra=1)
        with self.assertRaises(H.HistoryError):
            H.validate_event(loose)
        with self.assertRaises(H.HistoryError):
            H.validate_event(dict(EXAMPLE, before={"value": 1, "digest": "x"}))

    def test_ids_have_the_documented_prefixes(self):
        self.assertRegex(H.new_event_id(), r"hevt_[0-9a-f]{32}\Z")
        self.assertRegex(H.new_transaction_id(), r"htxn_[0-9a-f]{32}\Z")


class PublishTest(HistoryBase):
    def test_one_file_per_event_in_its_utc_month_with_one_lf_line(self):
        first, second = event(now=NOW), event(now=MONTH_2)
        with self.locked():
            paths = H.publish_events_locked(self.root, [first, second])
        self.assertEqual(len(paths), 2)
        months = sorted(p.parent.name for p in self.files())
        self.assertEqual(months, [first["at"][:7], second["at"][:7]])
        self.assertNotEqual(months[0], months[1])
        for path, expected in zip(self.files(), sorted((first, second), key=lambda e: e["at"])):
            self.assertEqual(path.name, f"{expected['event_id']}.jsonl")
            raw = path.read_bytes()
            self.assertEqual(raw, H.event_bytes(expected))
            self.assertEqual(raw.count(b"\n"), 1)

    def test_published_files_are_never_modified_and_replay_is_a_no_op(self):
        one = event()
        with self.locked():
            H.publish_events_locked(self.root, [one])
        path = self.files()[0]
        before = (sha(path), path.stat().st_mtime_ns, path.stat().st_ino)
        with self.locked():
            H.publish_events_locked(self.root, [one])
            H.publish_events_locked(self.root, [one])
        self.assertEqual((sha(path), path.stat().st_mtime_ns, path.stat().st_ino), before)
        self.assertEqual(len(self.files()), 1)

    def test_same_event_id_with_other_bytes_is_a_conflict_and_nothing_is_overwritten(self):
        one = event()
        other = dict(one, reason="다른 이유")
        with self.locked():
            H.publish_events_locked(self.root, [one])
            digest = sha(self.files()[0])
            with self.assertRaises(H.HistoryError) as ctx:
                H.publish_events_locked(self.root, [other])
        self.assertEqual(ctx.exception.code, "event-conflict")
        self.assertEqual(sha(self.files()[0]), digest)

    def test_publishing_needs_the_admission_lock_and_unique_ids(self):
        with self.assertRaises(H.HistoryError) as ctx:
            H.publish_events_locked(self.root, [event()])
        self.assertEqual(ctx.exception.code, "admission-lock-required")
        one = event()
        with self.locked(), self.assertRaises(H.HistoryError):
            H.publish_events_locked(self.root, [one, one])
        self.assertEqual(self.files(), [])

    def test_a_new_month_never_touches_existing_files(self):
        old = event(now=NOW)
        with self.locked():
            H.publish_events_locked(self.root, [old])
        snapshot = {p: (sha(p), p.stat().st_mtime_ns) for p in self.files()}
        with self.locked():
            H.publish_events_locked(self.root, [event(now=MONTH_2)])
        for path, expected in snapshot.items():
            self.assertEqual((sha(path), path.stat().st_mtime_ns), expected)
        self.assertEqual(len(self.files()), 2)

    def test_no_partial_line_is_ever_visible_and_staging_is_cleaned(self):
        with self.locked():
            H.publish_events_locked(self.root, [event(), event()])
        staging = self.root / H.STAGING_REL
        self.assertEqual([p for p in staging.iterdir()], [])
        with mock.patch.object(H.os, "link", side_effect=OSError("disk full")), self.locked():
            with self.assertRaises(H.HistoryError) as ctx:
                H.publish_events_locked(self.root, [event()])
        self.assertEqual(ctx.exception.code, "history-write-failed")
        self.assertEqual(len(self.files()), 2)  # only the two published before the failure
        self.assertEqual([p for p in staging.iterdir()], [])

    def test_iter_events_reads_back_what_was_published_and_skips_damage(self):
        one = event()
        with self.locked():
            H.publish_events_locked(self.root, [one])
        broken = self.files()[0].parent / ("hevt_" + "9" * 32 + ".jsonl")
        broken.write_text("{not json}\n", encoding="utf-8")
        self.assertEqual(list(H.iter_events(self.root)), [one])
        self.assertTrue(broken.exists())  # damage is skipped, never repaired or deleted

    def test_history_directory_is_the_documented_location(self):
        self.assertEqual(H.HISTORY_REL, ".runtime/artifact-producer/v1/history")
        self.assertIsNotNone(re.fullmatch(r"\d{4}-\d{2}", event()["at"][:7]))


if __name__ == "__main__":
    unittest.main()
