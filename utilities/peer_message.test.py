#!/usr/bin/env python3
"""Unit tests for utilities/peer-message.py (SD-122 peer-message ledger)."""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_message", str(_HERE / "peer-message.py"))
peer_message = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_message)


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root / "peer-state")
        # C-9 made `_iter_records` (list/status) read through `steward_marker_roots()`,
        # whose "fleet-reader" branch always adds `stable_state_root(os.environ)`
        # (dispatch_state_roots) AND every installed runtime's own root
        # (_runtime_ledger_roots: ~/.codex, ~/.claude, ~/.config/opencode) as read
        # candidates. Without isolating HOME too, those candidates fall through to
        # this machine's real per-user roots and a list/status assertion here would
        # see live records (CODEX_HOME/CLAUDE_CONFIG_DIR are unset in the real
        # environment, so overriding HOME alone covers all three).
        os.environ["HOME"] = str(self.tmp_root / "home")
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _record(self, body="hello world\nsecond line", **overrides):
        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=True,
        )
        args.update(overrides)
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _FakeStdin(body)
        try:
            return peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin

    def _ledger_file(self, from_sid="sid-a"):
        return peer_message._ledger_path(from_sid)


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def isatty(self):
        return False

    def read(self):
        return self._data


class PeerMessageTest(_TmpRootMixin, unittest.TestCase):
    def test_record_writes_exactly_one_line(self):
        rc = self._record()
        self.assertEqual(rc, 0)
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 1)

    def test_append_only(self):
        self._record()
        first = self._ledger_file().read_text().splitlines()[0]
        self._record()
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first)

    def test_schema_v1_fields(self):
        self._record()
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        for key in ("schema_version", "message_id", "ts", "from", "to", "kind",
                    "summary", "body_sha256", "delivery", "refs"):
            self.assertIn(key, rec)
        self.assertEqual(rec["schema_version"], 1)
        self.assertEqual(len(rec["message_id"]), 16)
        int(rec["message_id"], 16)

    def test_same_second_repeated_records_get_distinct_message_ids(self):
        # Same sender/target/second-resolution ts/summary — the classic
        # steward-wait collision reproduced in evidence/peer-steward-wait.txt.
        to = {"session_id": "sid-b"}
        ts = "2026-09-02T00:00:00Z"
        id_a = peer_message._message_id("sid-a", to, ts, "same summary")
        id_b = peer_message._message_id("sid-a", to, ts, "same summary")
        self.assertNotEqual(id_a, id_b)
        for mid in (id_a, id_b):
            self.assertEqual(len(mid), 16)
            int(mid, 16)

    def test_summary_hard_truncated_at_200(self):
        body = "x" * 500
        self._record(body=body)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(len(rec["summary"]), 200)

    def test_body_text_absent_body_sha256_present(self):
        body = "first line only\nsuper-secret-body-text-marker\nline3"
        self._record(body=body)
        raw = self._ledger_file().read_text()
        self.assertNotIn("super-secret-body-text-marker", raw)
        rec = json.loads(raw.splitlines()[0])
        self.assertEqual(rec["body_sha256"], hashlib.sha256(body.encode()).hexdigest())

    def test_flock_serializes_concurrent_writers(self):
        import multiprocessing

        def worker(n):
            for i in range(20):
                self._record(body=f"worker {n} msg {i}")

        procs = [multiprocessing.Process(target=worker, args=(n,)) for n in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 40)
        for line in lines:
            json.loads(line)

    def test_list_skips_malformed_line(self):
        self._record(body="one")
        self._record(body="two")
        path = self._ledger_file()
        with open(path, "a") as fh:
            fh.write("{not json\n")
        ns = peer_message.argparse.Namespace(since_hours=None, limit=None)
        recs = list(peer_message._iter_records())
        self.assertEqual(len(recs), 2)

    def test_status_skips_malformed_line(self):
        self._record(body="one")
        path = self._ledger_file()
        with open(path, "a") as fh:
            fh.write("{also not json\n")
        ns = peer_message.argparse.Namespace(since_hours=None)
        rc = peer_message.cmd_status(ns)
        self.assertEqual(rc, 0)

    def test_unwritable_state_root_is_fail_soft(self):
        root = peer_message.peer_state_root() / "peer-messages"
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, stat.S_IREAD | stat.S_IEXEC)
        try:
            rc = self._record()
            self.assertNotEqual(rc, 0)
        finally:
            os.chmod(root, stat.S_IRWXU)

    def test_state_root_comes_from_resolver(self):
        self._record()
        path = self._ledger_file()
        self.assertTrue(str(path).startswith(str(self.tmp_root)))

    def test_path_layout(self):
        self._record()
        path = self._ledger_file()
        self.assertEqual(path.name, "sid-a.jsonl")
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}$")
        self.assertEqual(path.parent.parent.name, "peer-messages")


class PeerMessageEnumValidationTest(_TmpRootMixin, unittest.TestCase):
    def test_invalid_kind_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(kind="not-a-kind")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-kind", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_invalid_surface_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(surface="unknown")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-surface", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_invalid_status_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(status="not-a-status")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-status", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_valid_enum_values_all_pass(self):
        for kind in peer_message._KINDS:
            for surface in peer_message._SURFACES:
                for status in peer_message._STATUSES:
                    rc = self._record(kind=kind, surface=surface, status=status,
                                       from_session_id=f"sid-{kind}-{surface}-{status}")
                    self.assertEqual(rc, 0)

    def test_hook_call_site_values_are_in_enum(self):
        self.assertIn("claude-native", peer_message._SURFACES)
        self.assertIn("herdr", peer_message._SURFACES)
        self.assertIn("received", peer_message._STATUSES)
        self.assertIn("sent", peer_message._STATUSES)

    def test_surface_required_by_argparse(self):
        parser_argv = [
            "record",
            "--from-harness", "claude", "--from-session-id", "sid-a",
            "--to-harness", "claude", "--to-name", "peer-1",
        ]
        with self.assertRaises(SystemExit):
            peer_message.main(parser_argv)


class PeerMessageBodyFlagTest(_TmpRootMixin, unittest.TestCase):
    def test_no_body_flag_does_not_touch_stdin_and_returns_empty(self):
        class _RaisingStdin:
            def isatty(self):
                return False

            def read(self):
                raise AssertionError("stdin must not be read when no body flag is given")

        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=False,
        )
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _RaisingStdin()
        try:
            rc = peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(rec["summary"], "")
        self.assertEqual(rec["body_sha256"], hashlib.sha256(b"").hexdigest())

    def test_body_stdin_flag_reads_stdin(self):
        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=True,
        )
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _FakeStdin("piped body\nsecond")
        try:
            rc = peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(rec["summary"], "piped body")


def _rec(sid, to_sid, summary, ts="2026-09-01T00:00:00Z"):
    return {
        "schema_version": 1, "message_id": summary, "ts": ts,
        "from": {"harness": "claude", "session_id": sid, "project": "p"},
        "to": {"harness": "claude", "session_id": to_sid}, "kind": "steer",
        "summary": summary, "body_sha256": "x",
        "delivery": {"surface": "herdr", "status": "sent", "receipt": None}, "refs": [],
    }


class IterRecordsMultiRootTest(_TmpRootMixin, unittest.TestCase):
    """C-9 — `_iter_records` (the engine behind `list`/`status`) reads the SAME
    root set every other peer-ledger consumer reads (`steward_marker_roots`), not
    just the one root new writes land under. C-1 moves only where NEW records
    land; it never migrates history, so a record still sitting under the old
    dispatch-registry root must not silently disappear from `list`/`status`."""

    def _write_shard(self, root, sid, rec):
        month_dir = Path(root) / "peer-messages" / time.strftime("%Y-%m", time.gmtime())
        month_dir.mkdir(parents=True, exist_ok=True)
        with open(month_dir / ("%s.jsonl" % sid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_iter_records_reads_both_the_new_and_the_old_root(self):
        old_root = peer_message._ledger_root()      # AGENT_DISPATCH_JOBS-derived (pre-C-1 root)
        new_root = peer_message.peer_state_root()   # AGENT_PEER_LEDGER_ROOT (C-1 canonical root)
        self.assertNotEqual(str(old_root), str(new_root))
        self._write_shard(old_root, "sid-old", _rec("sid-old", "sid-x", "old-root"))
        self._write_shard(new_root, "sid-new", _rec("sid-new", "sid-y", "new-root"))

        recs = list(peer_message._iter_records())
        self.assertEqual({r["summary"] for r in recs}, {"old-root", "new-root"})

        # A root that appears twice in the resolver's list must not double its records.
        with mock.patch.object(peer_message, "steward_marker_roots",
                                return_value=([Path(new_root), Path(new_root)], "test")):
            recs_dup = list(peer_message._iter_records())
        self.assertEqual(len(recs_dup), 1)
        self.assertEqual(recs_dup[0]["summary"], "new-root")

        # cmd_list/cmd_status go through the same engine and see both records too.
        recs_by_cmd = list(peer_message._iter_records(since_hours=None))
        self.assertEqual(len(recs_by_cmd), 2)


class ReadTransferRecordCrossRootTest(_TmpRootMixin, unittest.TestCase):
    """C-4 — sender and receiver processes commonly run under different
    per-runtime `AGENT_DISPATCH_JOBS` (§2.2: a Codex managed session inherits its
    own dispatch tree). Before C-1, `_transfer_path` followed that same
    per-process value, so a sender and receiver with different
    `AGENT_DISPATCH_JOBS` wrote/read two different transfer-record roots. C-1
    anchors `_transfer_path` to `peer_state_root()` instead, which both
    processes resolve to the SAME value regardless of `AGENT_DISPATCH_JOBS` —
    this is what actually lets the receiver's `parse_peer_trailer` recover the
    sender's identity."""

    def test_transfer_record_written_under_one_dispatch_root_is_read_under_another(self):
        recipient = {"harness": "codex", "session_id": "recipient-a"}
        sender = {"harness": "codex", "session_id": "sender-a", "name": "s"}
        text, ref = peer_message.prepare_peer_message("body", sender, recipient)
        own_path = peer_message._transfer_path(ref)
        self.assertTrue(own_path.exists())
        # Simulate a receiving process with a different AGENT_DISPATCH_JOBS (its own
        # runtime's dispatch root) while AGENT_PEER_LEDGER_ROOT — the value that
        # actually governs `_transfer_path` since C-1 — stays the same.
        other_jobs = self.tmp_root / "other-runtime" / "jobs.log"
        other_jobs.parent.mkdir(parents=True)
        other_jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(other_jobs)
        self.assertEqual(peer_message._transfer_path(ref), own_path)
        parsed = peer_message.parse_peer_trailer(text, recipient)
        self.assertEqual(parsed["session_id"], sender["session_id"])


class F100cSenderAndStewardTest(unittest.TestCase):
    """F-100c — from.name, the herdr sender trailer, and the steward marker."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.root / "jobs.log")
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        # See `_TmpRootMixin.setUp` — isolates every read candidate
        # `steward_marker_roots()`'s fleet-reader branch can add.
        os.environ["HOME"] = str(self.root / "home")
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        (self.root / "jobs.log").touch()
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._old)

    def _ns(self, **over):
        base = dict(from_harness="claude", from_session_id="sid-a", from_project="p",
                    from_name=None, to_harness="codex", to_session_id="sid-c", to_name="child",
                    kind="steer", surface="herdr", status="sent", receipt=None, ref=[],
                    body_file=None, body_stdin=False)
        base.update(over)
        return peer_message.argparse.Namespace(**base)

    def _records(self):
        out = []
        for f in (peer_message.peer_state_root() / "peer-messages").rglob("*.jsonl"):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def test_from_name_is_additive(self):
        self.assertEqual(peer_message.cmd_record(self._ns()), 0)
        self.assertEqual(peer_message.cmd_record(self._ns(from_name="hearting-46")), 0)
        recs = self._records()
        self.assertNotIn("name", recs[0]["from"])
        self.assertEqual(recs[1]["from"]["name"], "hearting-46")

    def test_trailer_round_trip_and_last_wins(self):
        t = peer_message.peer_trailer("claude", "sid-a", "hearting-46")
        self.assertIn("(peer-from: claude [?] hearting-46)", t)
        self.assertEqual(peer_message.parse_peer_trailer("body\n\n" + t),
                         {"harness": "claude", "session_id": None, "name": "hearting-46"})
        self.assertEqual(peer_message.parse_peer_trailer("(peer-from: codex 01a0)")["name"], None)
        two = "(peer-from: codex one)\nx\n(peer-from: claude two n)"
        self.assertEqual(peer_message.parse_peer_trailer(two)["session_id"], "two")
        self.assertIsNone(peer_message.parse_peer_trailer("no trailer here"))
        self.assertIsNone(peer_message.parse_peer_trailer(None))
        # a name with a ')' cannot break the envelope
        self.assertEqual(peer_message.parse_peer_trailer(
            peer_message.peer_trailer("claude", "s", "a) b"))["name"], "a  b".replace("  ", " "))

    def _marker_keys_on_disk(self):
        return {(h, d.get("session_id")) for h, _p, d in
                peer_message._iter_marker_files(peer_message.peer_state_root()) if d}

    def test_record_never_marks_the_sender_whatever_the_kind(self):
        """Role ≠ communication (user 2026-09-06: every session that talked showed d=−1).
        A worker's `[handoff]` to its steward is a message; so is a SendMessage with
        `notify_when_idle`, which the Claude hook records as `kind=watch` (review #1)."""
        for kind in peer_message._KINDS:
            for status in ("sent", "delivered", "received"):
                self.assertEqual(peer_message.cmd_record(self._ns(kind=kind, status=status)), 0)
        self.assertEqual(peer_message.read_steward_markers(), {})
        self.assertEqual(self._marker_keys_on_disk(), set())
        self.assertFalse(peer_message.steward_marker_path("claude", "sid-a").exists())
        self.assertFalse((peer_message.peer_state_root() / "peer-steward").exists())

    def test_mark_steward_requires_an_evidence_source_and_release_clears(self):
        ts = "2026-09-06T00:00:00Z"
        to = {"harness": "codex", "session_id": "sid-c", "name": "child"}
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "handoff", ts))
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "watch", ts))       # no source
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "steer", ts, source="steer"))
        self.assertFalse(peer_message.mark_steward("claude", "", to, "watch", ts, source="watch"))
        self.assertFalse(peer_message.steward_marker_path("claude", "sid-a").exists())
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "start", ts, source="start"))
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "watch", ts, source="watch"))
        self.assertTrue(peer_message.mark_steward("claude", "sid-a",
                                                  {"harness": "unknown", "name": "-"},
                                                  "explicit", ts, source="explicit"))
        marker = peer_message.read_steward_markers()[("claude", "sid-a")]
        self.assertEqual({e["source"] for e in marker["targets"].values()}, {"watch", "explicit"})
        self.assertEqual(marker["targets"]["sid-c"]["harness"], "codex")
        self.assertEqual(marker["schema_version"], 2)
        self.assertEqual(peer_message.cmd_release(peer_message.argparse.Namespace(
            harness="claude", session_id="sid-a")), 0)
        self.assertEqual(peer_message.read_steward_markers(), {})

    def test_schema_version_2_only_when_every_entry_carries_a_source(self):
        path = self._write_marker("claude", "sid-a", {
            "h": {"harness": "claude", "session_id": "h", "name": "h", "kind": "handoff", "ts": "1"}})
        to = {"harness": "codex", "session_id": "w", "name": "w"}
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "watch", "2", source="watch"))
        data = json.loads(path.read_text())
        self.assertEqual(data["schema_version"], 1)      # a source-less entry remains
        self.assertEqual({e.get("source") for e in data["targets"].values()}, {None, "watch"})
        self.assertTrue(peer_message.mark_steward("claude", "sid-b", to, "watch", "2", source="watch"))
        self.assertEqual(json.loads(peer_message.steward_marker_path("claude", "sid-b").read_text())
                         ["schema_version"], 2)

    def _write_marker(self, harness, sid, targets, updated="2026-09-06T00:00:00Z"):
        path = peer_message.steward_marker_path(harness, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "harness": harness, "session_id": sid,
                                    "since": updated, "updated": updated, "targets": targets}))
        return path

    def test_legacy_markers_are_judged_by_kind_and_handoff_only_ones_are_ignored(self):
        """Markers the old rule left behind carry no `source`: a wait/watch entry (kind=watch)
        and the old `steward on` placeholder (kind=watch, name '-') still count; entries from
        steer/handoff/gate-relay sends do not — no migration, the reader just stops believing
        them."""
        self._write_marker("claude", "worker-1", {
            "w1:p15": {"harness": "claude", "session_id": "steward-1", "name": "hearting-b0",
                       "kind": "handoff", "ts": "2026-09-06T01:00:00Z"},
            "x": {"harness": "codex", "session_id": "x", "name": "x", "kind": "steer",
                  "ts": "2026-09-06T02:00:00Z"}})
        self._write_marker("claude", "steward-1", {
            "child-a": {"harness": "codex", "session_id": "child-a", "name": "a", "kind": "handoff",
                        "ts": "2026-09-06T03:00:00Z"},
            "child-b": {"harness": "claude", "session_id": "child-b", "name": "b", "kind": "watch",
                        "ts": "2026-09-06T01:00:00Z"},
            "-": {"harness": "unknown", "session_id": None, "name": "-", "kind": "watch",
                  "ts": "2026-09-06T02:00:00Z"}})
        self._write_marker("opencode", "oc-1", {})
        self._write_marker("claude", "old-launcher", {
            "c": {"harness": "codex", "session_id": "c", "name": "c", "kind": "steer",
                  "ts": "2026-09-06T01:00:00Z"}})          # the old `start` wrote a steer row
        self.assertEqual(set(peer_message.read_steward_markers()), {("claude", "steward-1")})
        self.assertEqual(self._marker_keys_on_disk(),
                         {("claude", "worker-1"), ("claude", "steward-1"), ("opencode", "oc-1"),
                          ("claude", "old-launcher")})
        self.assertFalse(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "start", "ts": "1"}}}))                # legacy kind=start is not evidence
        marker = peer_message.read_steward_markers()[("claude", "steward-1")]
        self.assertEqual([e["session_id"] for e in peer_message.steward_evidence_targets(marker)],
                         ["child-b", None])
        self.assertFalse(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "watch", "source": "handoff"}}}))       # a source that is not evidence
        self.assertTrue(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "handoff", "source": "explicit"}}}))    # source wins over kind
        self.assertEqual(peer_message.steward_evidence_targets(None), [])
        self.assertEqual(peer_message.steward_evidence_targets({"targets": "junk"}), [])

    def test_prune_steward_markers_dry_run_lists_and_apply_removes_only_unevidenced(self):
        """Every run here names the temp root explicitly (`--root`): the default root set is
        the Fleet reader's, which includes the real per-user root."""
        root = str(peer_message.peer_state_root())
        stale = self._write_marker("claude", "worker-1", {
            "s": {"harness": "claude", "session_id": "s", "name": "n", "kind": "handoff", "ts": "1"}})
        empty = self._write_marker("claude", "worker-2", {})
        keep = self._write_marker("claude", "steward-1", {
            "c": {"harness": "codex", "session_id": "c", "name": "c", "kind": "watch", "ts": "1"}})
        broken = keep.parent / "broken.json"
        broken.write_text("{not json")
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_message.main(["prune-steward-markers", "--root", root]), 2)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertEqual(lines[0], f"root={root}")
        self.assertTrue(lines[-1].startswith("prune-steward-markers mode=dry-run roots=1 "
                                             "roots_source=explicit markers=4 unevidenced=2 "
                                             "removed=0 unreadable=1"), lines[-1])
        cands = [l for l in lines if l.startswith("candidate ")]
        self.assertEqual(sorted(l.split()[2] for l in cands), ["worker-1", "worker-2"])
        self.assertIn("kinds=handoff", next(l for l in cands if " worker-1 " in l))
        for p in (stale, empty, keep, broken):
            self.assertTrue(p.exists())
        # a flag raised between the scan and the unlink survives (review #7)
        real_unlink = peer_message._unlink_unevidenced_marker
        def racing_unlink(path):
            if path == empty:
                peer_message.mark_steward("claude", "worker-2", {"harness": "codex", "session_id": "z",
                                                                 "name": "z"}, "watch", "9", source="watch")
            return real_unlink(path)
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_message, "_unlink_unevidenced_marker", side_effect=racing_unlink):
            self.assertEqual(peer_message.main(["prune-steward-markers", "--apply", "--root", root]), 0)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertTrue(lines[-1].startswith("prune-steward-markers mode=apply roots=1 "
                                             "roots_source=explicit markers=4 unevidenced=2 "
                                             "removed=1 unreadable=1"), lines[-1])
        self.assertTrue(any(l.startswith("removed claude worker-1 ") for l in lines))
        self.assertTrue(any(l.startswith("kept:now-evidenced claude worker-2 ") for l in lines))
        self.assertFalse(stale.exists())
        self.assertTrue(empty.exists()); self.assertTrue(keep.exists()); self.assertTrue(broken.exists())
        # clean root → dry-run exits 0
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_message.main(["prune-steward-markers", "--root", root]), 0)
        self.assertIn("unevidenced=0", print_mock.call_args[0][0])

    def test_prune_default_roots_are_the_fleet_readers_and_are_printed(self):
        roots, source = peer_message.steward_marker_roots()
        self.assertEqual(source, "fleet-reader")
        self.assertEqual(roots[0], peer_message.peer_state_root())     # chain index 0 = the writer's root
        fake = self.root / "fake-root"
        (fake / "peer-steward" / "codex").mkdir(parents=True)
        (fake / "peer-steward" / "codex" / "t.json").write_text(json.dumps(
            {"session_id": "t", "targets": {"a": {"kind": "steer", "ts": "1"}}}))
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_message, "steward_marker_roots", return_value=([fake], "fleet-reader")):
            self.assertEqual(peer_message.main(["prune-steward-markers"]), 2)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertEqual(lines[0], f"root={fake}")
        self.assertIn("roots=1 roots_source=fleet-reader markers=1 unevidenced=1", lines[-1])
        self.assertTrue((fake / "peer-steward" / "codex" / "t.json").exists())

    def test_claude_session_name_reads_the_registry_by_session_id(self):
        cfg = self.root / "cfg"
        (cfg / "sessions").mkdir(parents=True)
        (cfg / "sessions" / "123.json").write_text(json.dumps(
            {"sessionId": "sid-a", "name": "hearting-46", "nameSource": "derived"}))
        (cfg / "sessions" / "bad.json").write_text("{not json")
        self.assertEqual(peer_message.claude_session_name("sid-a", config_dir=str(cfg)), "hearting-46")
        self.assertIsNone(peer_message.claude_session_name("sid-zz", config_dir=str(cfg)))
        self.assertIsNone(peer_message.claude_session_name("", config_dir=str(cfg)))


class UsableSessionIdTest(_TmpRootMixin, unittest.TestCase):
    """F-101i input guard — a value that cannot identify a session never becomes one.

    Measured 2026-09-03: a sender emitted the trailer's documentation form verbatim, so
    `(peer-from: claude <sid> hearting-46)` put the literal string `<sid>` in four ledger
    records and created a `<sid>.jsonl` shard beside the real per-session ones.
    """

    def test_real_ids_pass_through_unchanged(self):
        for sid in ("841d1f64-0cff-4c16-8578-e874a45a2bb6", "01a064d8-e0e6-7ac2-97df-877c380ce013",
                    "ses_7f3a19c2b0004e11", "sid-a"):
            with self.subTest(sid=sid):
                self.assertEqual(peer_message.usable_session_id(sid), sid)

    def test_placeholders_and_absent_markers_are_not_identities(self):
        for sid in ("<sid>", "-", "", "   ", None, "<session_id>", ".", ".."):
            with self.subTest(sid=sid):
                self.assertIsNone(peer_message.usable_session_id(sid))

    def test_a_separator_never_reaches_the_ledger_filename(self):
        for sid in ("../../escape", "a/b", "a\\b", "a b"):
            with self.subTest(sid=sid):
                self.assertIsNone(peer_message.usable_session_id(sid))

    def test_trailer_parse_drops_an_unsubstituted_placeholder(self):
        parsed = peer_message.parse_peer_trailer("body\n(peer-from: claude <sid> hearting-46)")
        self.assertEqual(parsed["harness"], "claude")
        self.assertIsNone(parsed["session_id"])
        self.assertEqual(parsed["name"], "hearting-46")

    def test_trailer_parse_keeps_a_real_id(self):
        parsed = peer_message.parse_peer_trailer(
            "body\n(peer-from: claude 841d1f64-0cff-4c16-8578-e874a45a2bb6 hearting-18)")
        self.assertEqual(parsed["session_id"], "841d1f64-0cff-4c16-8578-e874a45a2bb6")

    def test_the_module_own_absent_marker_round_trips_as_absent(self):
        """`peer_trailer` writes `-` when it has no id; parsing it back must not mint one."""
        text = peer_message.peer_trailer("claude", None, "hearting-46")
        self.assertIn("(peer-from: claude [?]", text)
        self.assertIsNone(peer_message.parse_peer_trailer(text)["session_id"])

    def test_unusable_sender_shards_to_unknown_not_to_a_junk_sibling(self):
        for sid in ("<sid>", "-", ""):
            with self.subTest(sid=sid):
                self.assertEqual(peer_message._ledger_path(sid).name, "unknown.jsonl")
        self.assertEqual(peer_message._ledger_path("sid-a").name, "sid-a.jsonl")

    def test_record_stores_no_placeholder_on_either_endpoint(self):
        rc = peer_message.cmd_record(peer_message.argparse.Namespace(
            from_harness="claude", from_session_id="<sid>", from_project="hearting",
            from_name="hearting-46", to_harness="claude", to_session_id="<sid>",
            to_name="hearting-18", kind="notice", surface="herdr", status="received",
            receipt=None, ref=[], body_file=None, body_stdin=False))
        self.assertEqual(rc, 0)
        shard = peer_message._ledger_path("")
        rec = json.loads(shard.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["from"]["session_id"], "")
        self.assertNotIn("session_id", rec["to"])
        # The name is still carried, so `✉` counters and the notice keep working.
        self.assertEqual(rec["from"]["name"], "hearting-46")


if __name__ == "__main__":
    unittest.main()
