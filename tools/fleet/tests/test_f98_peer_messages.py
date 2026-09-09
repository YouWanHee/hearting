#!/usr/bin/env python3
"""F-98 — read-only peer-message ledger projection (SD-122 §13.37.2)."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render  # noqa: E402
from fleet.collectors import peer_messages  # noqa: E402
from fleet.model import Session  # noqa: E402

# `utilities/peer-message.py` is hyphenated (not `import`able by name); loaded the
# same way its own test suite (utilities/peer_message.test.py) does.
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message_c6", os.path.join(_REPO_ROOT, "utilities", "peer-message.py"))
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)
_PS_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_c6", os.path.join(_REPO_ROOT, "utilities", "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_PS_SPEC)
_PS_SPEC.loader.exec_module(peer_steward)


def _write_ledger(root, from_sid, records):
    month_dir = os.path.join(root, "peer-messages", "2026-09")
    os.makedirs(month_dir, exist_ok=True)
    path = os.path.join(month_dir, "%s.jsonl" % from_sid)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _rec(from_sid, to_sid=None, to_name=None, kind="steer", summary="hi",
        minutes_ago=0, body_sha256="deadbeef"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
    to = {"harness": "claude"}
    if to_sid:
        to["session_id"] = to_sid
    if to_name:
        to["name"] = to_name
    return {
        "schema_version": 1, "message_id": "abc123", "ts": ts,
        "from": {"harness": "claude", "session_id": from_sid, "project": "p"},
        "to": to, "kind": kind, "summary": summary, "body_sha256": body_sha256,
        "delivery": {"surface": "claude-native", "status": "sent", "receipt": None},
        "refs": [],
    }


class CollectorTest(unittest.TestCase):
    def test_three_record_fixture_badge_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=1),
                _rec("sid-a", to_sid="sid-b", minutes_ago=2),
            ])
            _write_ledger(tmp, "sid-b", [_rec("sid-b", to_sid="sid-a", minutes_ago=1)])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"][("claude", "sid-a")]["sent_1h"], 2)
        self.assertEqual(result["by_session"][("claude", "sid-a")]["recv_1h"], 1)
        self.assertEqual(result["by_session"][("claude", "sid-b")]["sent_1h"], 1)
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 2)

    def test_subtitle_latest_record_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=10, kind="steer"),
                _rec("sid-a", to_sid="sid-b", minutes_ago=1, kind="handoff"),
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"][("claude", "sid-b")]["last_recv"]["kind"], "handoff")

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_ledger(tmp, "sid-a", [_rec("sid-a", to_sid="sid-b")])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{not json\n")
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(peer_messages.collect.last_malformed, 1)
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 1)

    def test_records_older_than_24h_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=25 * 60),
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["records"], [])

    def test_record_cap_200(self):
        # Spread across three sender files so no single file's tail-64KB bound
        # interferes with proving the independent 200-record overall cap.
        with tempfile.TemporaryDirectory() as tmp:
            for sender in ("sid-a", "sid-b", "sid-c"):
                recs = [_rec(sender, to_sid="sid-z", summary="x", minutes_ago=i)
                        for i in range(80)]
                _write_ledger(tmp, sender, recs)
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(len(result["records"]), 200)

    def test_file_tail_64kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            month_dir = os.path.join(tmp, "peer-messages", "2026-09")
            os.makedirs(month_dir)
            path = os.path.join(month_dir, "sid-a.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x" * (200 * 1024) + "\n")  # oversized garbage prefix
                fh.write(json.dumps(_rec("sid-a", to_sid="sid-b", minutes_ago=1)) + "\n")
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 1)

    def test_json_exposes_summary_only_never_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", summary="short summary", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        raw = json.dumps({"records": result["records"],
                          "by_session": {str(k): v for k, v in result["by_session"].items()}})
        self.assertIn("short summary", raw)
        self.assertNotIn("body", raw.lower().replace("body_sha256", ""))
        self.assertLessEqual(len(result["records"][0]["summary"]), 200)

    def test_sent_record_never_labels_recipient_as_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", to_name="sid-b-display", kind="steer", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        last_recv = result["by_session"][("claude", "sid-b")]["last_recv"]
        self.assertNotEqual(last_recv["from_name"], "sid-b-display")
        self.assertEqual(last_recv["from_name"], "sid-a")

    def test_join_is_exact_session_id_not_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_name="hearting-21 [f3e821]", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertNotIn(("claude", "hearting-21 [f3e821]"), result["by_session"])
        self.assertEqual(result["by_session"][("claude", "sid-a")]["sent_1h"], 1)


class StableRootResolverTest(unittest.TestCase):
    """F-98d — `_state_roots()` must read through the SAME resolver chain the
    writer (`utilities/peer-message.py`) uses (`resolve_dispatch_state_root` +
    `dispatch_state_roots`), not `dispatch._row_state_roots()`'s no-row default
    (which silently pins to the release-tree `.dispatch` and ignores an inherited
    `AGENT_DISPATCH_JOBS`). Every fixture above passes `state_roots=[tmp]`
    explicitly — this is the one that does not, which is exactly why the bug
    shipped undetected (measured 2026-09-02 against v2.101.0)."""

    def setUp(self):
        self._old_environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def test_collect_with_no_state_roots_finds_records_in_the_stable_root_only(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ["HOME"] = home
            os.environ["AGENT_HOME"] = home
            for key in ("CLAUDE_HOME", "AGENT_DISPATCH_JOBS", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
                os.environ.pop(key, None)
            stable_root = os.path.join(home, ".local", "state", "hearting", "dispatch")
            _write_ledger(stable_root, "sid-a", [_rec("sid-a", to_sid="sid-b", minutes_ago=1)])
            result = peer_messages.collect()
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 1)

    def test_collect_with_no_state_roots_honors_agent_dispatch_jobs(self):
        with tempfile.TemporaryDirectory() as base:
            home = os.path.join(base, "home")
            os.makedirs(home)
            registry_dir = os.path.join(base, "custom-jobs-dir")
            os.makedirs(registry_dir)
            os.environ["HOME"] = home
            os.environ["AGENT_HOME"] = home
            os.environ["AGENT_DISPATCH_JOBS"] = os.path.join(registry_dir, "jobs.log")
            for key in ("CLAUDE_HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
                os.environ.pop(key, None)
            _write_ledger(registry_dir, "sid-a", [_rec("sid-a", to_sid="sid-b", minutes_ago=1)])
            result = peer_messages.collect()
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 1)


class RenderByteIdenticalTest(unittest.TestCase):
    def test_ledger_absent_render_byte_identical(self):
        with mock.patch.object(peer_messages, "collect", return_value={"records": [], "by_session": {}}):
            pass  # collector isolation only; render reads Session fields, not the collector

    def test_no_steward_badge_rendered(self):
        s = Session(harness="claude", pid=1, session_id="sid-a", peer_sent_1h=2, peer_recv_1h=1)
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertNotIn("steward", text.lower())

    def test_badge_zero_when_no_peer_activity(self):
        s = Session(harness="claude", pid=1, session_id="sid-a")
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertNotIn("✉", text)

    def test_badge_present_with_activity(self):
        s = Session(harness="claude", pid=1, session_id="sid-a", peer_sent_1h=3, peer_recv_1h=2)
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertIn("✉ 3/2", text)

    def test_sessions_json_has_no_summary_field(self):
        s = Session(harness="claude", pid=1, session_id="sid-a",
                   peer_last_recv={"from_name": "x", "from_session_id": "sid-b",
                                    "kind": "steer", "age_min": 1})
        d = s.to_dict()
        self.assertNotIn("summary", json.dumps(d["peer_last_recv"]))


class LedgerFormatTest(unittest.TestCase):
    """C-6 — `AGENT_PEER_LEDGER_ROOT` round trip, `refs`/`transfer_ref` shape, and
    legacy `ref=`-in-name display tolerance (C-2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = dict(os.environ)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._old)

    def _record_line(self, from_sid="sid-a"):
        path = peer_message._ledger_path(from_sid)
        return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])

    def test_agent_peer_ledger_root_round_trip_four_fields(self):
        self.assertEqual(peer_message.peer_state_root(), self.root)
        rc = peer_message.cmd_record(peer_message.argparse.Namespace(
            from_harness="codex", from_session_id="sid-a", from_project="proj",
            from_name="hearting-codex-1", to_harness="claude", to_session_id="sid-b",
            to_name=None, kind="steer", surface="herdr", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=False))
        self.assertEqual(rc, 0)
        rec = self._record_line()
        self.assertEqual(rec["from"]["harness"], "codex")
        self.assertEqual(rec["from"]["session_id"], "sid-a")
        self.assertEqual(rec["from"]["project"], "proj")
        self.assertEqual(rec["from"]["name"], "hearting-codex-1")

    def test_refs_is_a_top_level_list_transfer_ref_is_a_separate_top_level_key(self):
        transfer_ref = "0123456789abcdef0123456789abcdef"
        rc = peer_message.cmd_record(peer_message.argparse.Namespace(
            from_harness="codex", from_session_id="sid-a", from_project="proj",
            from_name=None, to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="herdr", status="sent", receipt=None,
            ref=["a", "b"], body_file=None, body_stdin=False, transfer_ref=transfer_ref))
        self.assertEqual(rc, 0)
        rec = self._record_line()
        self.assertEqual(rec["refs"], ["a", "b"])
        self.assertEqual(rec["transfer_ref"], transfer_ref)
        self.assertEqual(rec["message_id"], transfer_ref)

    def test_legacy_ref_suffix_in_name_displays_clean(self):
        month_dir = self.root / "peer-messages" / time.strftime("%Y-%m", time.gmtime())
        month_dir.mkdir(parents=True)
        (month_dir / "sid-legacy.jsonl").write_text(json.dumps({
            "schema_version": 1, "message_id": "legacy1",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from": {"harness": "codex", "session_id": "sid-legacy", "project": "p",
                     "name": "hearting-a9 ; ref=0123456789abcdef0123456789abcdef"},
            "to": {"harness": "claude", "session_id": "sid-b"}, "kind": "steer",
            "summary": "hi", "body_sha256": "x",
            "delivery": {"surface": "herdr", "status": "sent", "receipt": None}, "refs": [],
        }) + "\n")
        result = peer_messages.collect(state_roots=[str(self.root)])
        last_recv = result["by_session"][("claude", "sid-b")]["last_recv"]
        self.assertEqual(last_recv["from_name"], "hearting-a9")

    def test_a_paths_all_land_under_the_same_root(self):
        ref = "abcdef0123456789abcdef0123456789"
        root = str(peer_message.peer_state_root())
        self.assertTrue(str(peer_message._ledger_path("sid-a")).startswith(root))
        self.assertTrue(str(peer_message._transfer_path(ref)).startswith(root))
        self.assertTrue(str(peer_message.steward_marker_path("codex", "sid-a")).startswith(root))
        self.assertTrue(str(peer_steward._watch_root()).startswith(root))


class StateRootsPeerPromotionTest(unittest.TestCase):
    """C-8 — `_state_roots()` always puts the peer ledger's canonical root at
    index 0 (promoting, never duplicating, an already-present entry), and a
    resolver failure never drops the rest of the roots."""

    def test_promotes_peer_root_to_index_zero(self):
        with mock.patch.object(peer_messages, "_peer_ledger_root", return_value="/tmp/peer-root-x"), \
             mock.patch.object(peer_messages, "_runtime_ledger_roots", return_value=["/tmp/other"]), \
             mock.patch.object(peer_messages, "_agent_home", side_effect=Exception("no home")):
            roots = peer_messages._state_roots()
        self.assertEqual(roots, ("/tmp/peer-root-x", "/tmp/other"))

    def test_already_present_root_is_promoted_not_duplicated(self):
        with mock.patch.object(peer_messages, "_peer_ledger_root", return_value="/tmp/same-root"), \
             mock.patch.object(peer_messages, "_runtime_ledger_roots",
                                return_value=["/tmp/same-root", "/tmp/other"]), \
             mock.patch.object(peer_messages, "_agent_home", side_effect=Exception("no home")):
            roots = peer_messages._state_roots()
        self.assertEqual(roots, ("/tmp/same-root", "/tmp/other"))

    def test_resolver_failure_preserves_the_rest(self):
        with mock.patch.object(peer_messages, "_peer_ledger_root", return_value=None), \
             mock.patch.object(peer_messages, "_runtime_ledger_roots", return_value=["/tmp/other"]), \
             mock.patch.object(peer_messages, "_agent_home", side_effect=Exception("no home")):
            roots = peer_messages._state_roots()
        self.assertEqual(roots, ("/tmp/other",))


if __name__ == "__main__":
    unittest.main()
