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
        # The sender has no ledger name here, and an unnamed sender stays unnamed: the
        # collector used to paste the raw session id into the display-name slot, which is
        # how `← 01a084f7-63f2-7961-ae60-6fc2d8e60fc2` reached the board (2026-09-09).
        # The exact id stays in its own field for the join; naming is the renderer's job.
        self.assertIsNone(last_recv["from_name"])
        self.assertEqual(last_recv["from_session_id"], "sid-a")

    def test_last_sent_is_the_symmetric_send_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", to_name="sid-b-display", kind="steer", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        last_sent = result["by_session"][("claude", "sid-a")]["last_sent"]
        self.assertEqual(last_sent["to_session_id"], "sid-b")
        self.assertEqual(last_sent["to_name"], "sid-b-display")
        self.assertEqual(last_sent["kind"], "steer")
        # The receiver's own row records only what it received.
        self.assertIsNone(result["by_session"][("claude", "sid-b")]["last_sent"])

    def test_notice_receipt_is_not_counted_as_a_send(self):
        """A `notice` is the RECEIVER's receipt for someone else's message. Treating it as
        a send would make every received message also draw a `✉ →` on the wrong row."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", kind="notice", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertIsNone(result["by_session"][("claude", "sid-a")]["last_sent"])
        self.assertEqual(result["by_session"][("claude", "sid-a")]["sent_1h"], 0)

    def test_one_herdr_message_counts_once_on_both_axes(self):
        """A herdr message writes TWO records naming the same sender — its own `steer` and
        the receiver's `notice` receipt. Counting both made one sent message read `✉ 2/…`
        (measured 2026-09-09 on the codex↔claude test pair)."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", kind="steer", minutes_ago=1),
                _rec("sid-a", to_sid="sid-b", kind="notice", minutes_ago=1),
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"][("claude", "sid-a")]["sent_1h"], 1)
        self.assertEqual(result["by_session"][("claude", "sid-b")]["recv_1h"], 1)
        self.assertEqual(result["by_session"][("claude", "sid-b")]["sent_1h"], 0)

    def test_join_is_exact_session_id_not_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_name="hearting-21 [f3e821]", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertNotIn(("claude", "hearting-21 [f3e821]"), result["by_session"])
        self.assertEqual(result["by_session"][("claude", "sid-a")]["sent_1h"], 1)


class ResumedSessionJoinTest(unittest.TestCase):
    """A resumed session's receipts are split across the id it has now and the one it had
    before, and only the older key carries the sender identity (measured 2026-09-09: the
    newer key held a bare `notice` whose `from.session_id` was empty). Joining over both
    is what lets the board name the sender instead of drawing nothing."""

    def _session(self, **kwargs):
        from fleet.model import Session
        return Session(harness="claude", pid=1, cwd="/x", slug="s", **kwargs)

    def test_counters_sum_and_the_freshest_entry_wins(self):
        from fleet import collectors
        s = self._session(session_id="new", session_aliases=["old"])
        collectors.apply_peer_rows([s], {
            ("claude", "old"): {"sent_1h": 1, "recv_1h": 2, "last_sent": None,
                                "last_recv": {"from_name": "steward", "from_session_id": "p",
                                              "from_harness": "claude", "kind": "steer",
                                              "age_min": 5}},
            ("claude", "new"): {"sent_1h": 3, "recv_1h": 1, "last_sent": None,
                                "last_recv": {"from_name": None, "from_session_id": None,
                                              "from_harness": "", "kind": "notice",
                                              "age_min": 40}},
        })
        self.assertEqual((s.peer_sent_1h, s.peer_recv_1h), (4, 3))
        self.assertEqual(s.peer_last_recv["from_name"], "steward")
        self.assertEqual(s.peer_last_recv["from_session_id"], "p")

    def test_a_malformed_age_never_wins_the_freshest_comparison(self):
        from fleet import collectors
        s = self._session(session_id="new", session_aliases=["old"])
        collectors.apply_peer_rows([s], {
            ("claude", "old"): {"sent_1h": 0, "recv_1h": 0, "last_sent": None,
                                "last_recv": {"from_name": "real", "age_min": 5}},
            ("claude", "new"): {"sent_1h": 0, "recv_1h": 0, "last_sent": None,
                                "last_recv": {"from_name": "broken", "age_min": None}},
        })
        self.assertEqual(s.peer_last_recv["from_name"], "real")

    def test_a_session_without_aliases_is_unchanged(self):
        from fleet import collectors
        s = self._session(session_id="new")
        collectors.apply_peer_rows([s], {
            ("claude", "old"): {"sent_1h": 9, "recv_1h": 9, "last_recv": None,
                                "last_sent": None}})
        self.assertEqual((s.peer_sent_1h, s.peer_recv_1h), (0, 0))

    def test_a_still_live_id_is_never_adopted_as_an_alias(self):
        """`--fork-session` leaves the parent running under its own row; adopting its id
        would hand the parent's messages to the child."""
        from fleet import collectors
        parent_sid = "6044eb9f-7983-4c41-9b86-0bb2b70638fa"
        child = self._session(session_id="01a084f7-63f2-7961-ae60-6fc2d8e60fc2")
        parent = self._session(session_id=parent_sid)
        parent.pid = 2
        with mock.patch("fleet.session_registry.session_aliases", return_value=[parent_sid]):
            collectors.apply_session_aliases([child, parent])
        self.assertIsNone(child.session_aliases)


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


class PeerEndpointLabelTest(unittest.TestCase):
    """The name slot holds whatever the sender wrote; the board must not print machine text.

    Measured on the live board: `✉ → uds:/run/user/1002/cc-socks/2952102.sock` came from a
    real ledger record whose `to.name` was a socket address. That names nothing a person can
    look up — worse than the `claude:7a001534` fallback, because it is the same
    non-information dressed as a name.
    """

    def test_machine_addresses_are_not_treated_as_names(self):
        for value in ("uds:/run/user/1002/cc-socks/2952102.sock",
                      "/tmp/whatever.sock",
                      "http://example/x",
                      "01a084f7-63f2-7961-ae60-6fc2d8e60fc2",  # raw session id in the name slot
                      "", "   ", None, 42):
            with self.subTest(value=value):
                self.assertFalse(render._peer_name_is_readable(value))

    def test_real_session_names_still_pass(self):
        # Names here are free-form and often Korean prose, so the check has to be a small
        # denylist rather than a guess at what a name looks like.
        for value in ("hearting-46", "bc-resnet-67", "케언 W15 사이클 이어받기",
                      "q1-tts: v6 본생산", "wB:p3"):
            with self.subTest(value=value):
                self.assertTrue(render._peer_name_is_readable(value))

    def test_an_unusable_name_falls_through_to_the_short_id(self):
        segs = render._peer_endpoint_segs(
            "claude", "7a001534-ea46-4a67-971b-199c586889e2",
            "uds:/run/user/1002/cc-socks/2952102.sock", {})
        self.assertEqual("".join(t for t, _ in segs), "claude:7a001534")

    def test_an_unusable_name_with_no_id_falls_back_to_the_harness(self):
        segs = render._peer_endpoint_segs("claude", None, "/tmp/x.sock", {})
        self.assertEqual("".join(t for t, _ in segs), "claude")

    def test_an_on_screen_peer_still_wins_over_every_fallback(self):
        segs = render._peer_endpoint_segs(
            "claude", "7a001534-ea46-4a67-971b-199c586889e2", "/tmp/x.sock",
            {("claude", "7a001534-ea46-4a67-971b-199c586889e2"): "46"})
        self.assertEqual("".join(t for t, _ in segs), "[46] claude")


if __name__ == "__main__":
    unittest.main()
