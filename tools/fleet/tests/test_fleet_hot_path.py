#!/usr/bin/env python3
"""Fleet hot path (2026-09-08 audit F): one collector tick took ~40 s against a 2 s
refresh interval, and managed Codex app-server rows drew a blank `[  ]` badge.

Three tick-scoped fixes, each tested here against the measured cause:

1. `artifact_reader.read_scope` — `_artifact_candidates` asked `glob_bucket` once per
   entity x root (80 calls), each re-walking the whole campaign tree through
   `artifact_locator.scan_index`. Inside one projection pass the scan is reused; at the
   pass boundary it is forgotten (no persisted cache, no INDEX file).
2. `dispatch_contract.process_table_scan_scope` — `_dispatch_liveness` reached
   `process_group_observation` and `attempt_tagged_descendants`, each a full `/proc`
   walk, once per attempt (187 rows, two walks each). One collect tick now holds one
   walk; the per-attempt verdict is the same one the single-shot probes give,
   parent-leader exclusion and namespace rules included.
3. `codex.share_managed_tags` — the `app-server --listen` row of a managed session has
   no rollout of its own, so `enrich` never minted its tag. It now shares the tag of the
   `--remote` TUI client on the same exact managed `--state-dir`; a row that cannot pair
   draws `[--]` instead of a blank slot. Existing tags are never rewritten.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TOOLS_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
_UTILITIES = os.path.join(_REPO, "utilities")
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)

import artifact_reader as reader                                  # noqa: E402
import dispatch_contract as D                                     # noqa: E402
from fleet import projection, render                              # noqa: E402
from fleet.collectors import codex                                # noqa: E402
from fleet.collectors import dispatch as dispatch_collector       # noqa: E402
from fleet.model import Session                                   # noqa: E402

CAMP = "camp_" + "a" * 32
CYC = "cyc_" + "b" * 32


def _build_root(root: Path, slug: str = "hot-path") -> Path:
    campaign = root / "campaigns" / CAMP
    campaign.mkdir(parents=True)
    (campaign / "campaign.json").write_text(json.dumps({
        "campaign_id": CAMP, "title": "hot path fixture", "cycles": [CYC],
    }), encoding="utf-8")
    cycle = campaign / "cycles" / CYC
    (cycle / "artifacts" / "plans" / ("2026-09-08_%s" % slug)).mkdir(parents=True)
    (cycle / "manifest.json").write_text(json.dumps({
        "cycle": {"campaign_id": CAMP, "cycle_id": CYC},
    }), encoding="utf-8")
    return root


class _Counter:
    def __init__(self, wrapped):
        self.wrapped, self.calls = wrapped, 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.wrapped(*args, **kwargs)


class ReaderScopeTest(unittest.TestCase):
    def test_one_scan_per_pass_and_none_kept_across_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(Path(tmp))
            counter = _Counter(reader.artifact_locator.scan_index)
            with mock.patch.object(reader.artifact_locator, "scan_index", counter):
                # Outside any scope: every call scans (the pre-fix behaviour, unchanged).
                first = reader.glob_bucket(root, "plans", "*_hot-path")
                reader.glob_bucket(root, "plans", "*_hot-path")
                self.assertEqual(counter.calls, 2)
                # Inside one pass: many calls, one scan, identical answers.
                with reader.read_scope():
                    inside = [reader.glob_bucket(root, "plans", "*_hot-path") for _ in range(5)]
                    reader.cycle_bucket_dirs(root, "spec")          # another bucket, same scan
                self.assertEqual(counter.calls, 3)
                self.assertTrue(all(item == first for item in inside))
                self.assertEqual([p.name for p in first], ["2026-09-08_hot-path"])
                # The pass boundary forgets the memo: the next pass scans the records again.
                with reader.read_scope():
                    reader.glob_bucket(root, "plans", "*_hot-path")
                self.assertEqual(counter.calls, 4)
                self.assertIsNone(reader._READ_SCOPE.get())

    def test_nested_scope_joins_the_enclosing_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(Path(tmp))
            counter = _Counter(reader.artifact_locator.scan_index)
            with mock.patch.object(reader.artifact_locator, "scan_index", counter):
                with reader.read_scope():
                    reader.glob_bucket(root, "plans", "*")
                    with reader.read_scope():
                        reader.glob_bucket(root, "plans", "*")
                    reader.glob_bucket(root, "plans", "*")
                self.assertEqual(counter.calls, 1)

    def test_memo_hands_out_copies_so_bucket_dirs_can_extend_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(Path(tmp))
            with reader.read_scope():
                rows = reader.cycle_bucket_dirs(root, "plans")
                rows.append(("poison", {}))
                rows[0][1]["poison"] = True
                again = reader.cycle_bucket_dirs(root, "plans")
            self.assertEqual(len(again), 1)
            self.assertNotIn("poison", again[0][1])

    def test_a_new_pass_sees_records_written_after_the_previous_pass(self):
        """No persisted cache: identity comes from the records on every pass.

        The memo holds the record scan (which cycles exist, under which locator);
        bucket children are still listed live, so a new plan directory inside a known
        cycle shows up mid-pass, while a new cycle record waits for the next pass.
        """
        late_camp = "camp_" + "f" * 32
        late_cyc = "cyc_" + "e" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(Path(tmp))
            with reader.read_scope():
                self.assertEqual(len(reader.glob_bucket(root, "plans", "*_late*")), 0)
                (root / "campaigns" / CAMP / "cycles" / CYC / "artifacts" / "plans"
                 / "2026-09-08_late-child").mkdir()
                self.assertEqual(len(reader.glob_bucket(root, "plans", "*_late*")), 1)
                campaign = root / "campaigns" / late_camp
                campaign.mkdir()
                (campaign / "campaign.json").write_text(json.dumps({
                    "campaign_id": late_camp, "title": "late", "cycles": [late_cyc],
                }), encoding="utf-8")
                cycle = campaign / "cycles" / late_cyc
                (cycle / "artifacts" / "plans" / "2026-09-08_late-cycle").mkdir(parents=True)
                (cycle / "manifest.json").write_text(json.dumps({
                    "cycle": {"campaign_id": late_camp, "cycle_id": late_cyc},
                }), encoding="utf-8")
                # Same pass: the record scan is a memo and says so.
                self.assertEqual(len(reader.glob_bucket(root, "plans", "*_late*")), 1)
            with reader.read_scope():
                self.assertEqual(len(reader.glob_bucket(root, "plans", "*_late*")), 2)


class ProjectionScopeTest(unittest.TestCase):
    def test_attach_projections_holds_one_reader_scope_for_the_whole_pass(self):
        entered = []

        @contextmanager
        def read_scope():
            entered.append(True)
            yield

        stub = SimpleNamespace(read_scope=read_scope,
                               glob_bucket=lambda *a, **k: (entered.append("glob") or []))
        sessions = [Session(harness="claude", pid=11, cwd="/tmp/x", slug="alpha"),
                    Session(harness="codex", pid=12, cwd="/tmp/y", slug="beta")]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(projection, "_artifact_reader", return_value=stub):
            projection.attach_projections(sessions, [], artifact_root=tmp, now=1000.0)
        self.assertEqual(entered.count(True), 1)
        self.assertGreaterEqual(entered.count("glob"), 2)   # both entities asked inside it
        self.assertEqual(entered[0], True)                  # ...and only after the scope opened

    def test_attach_projections_without_a_reader_still_projects(self):
        sessions = [Session(harness="claude", pid=11, cwd="/tmp/x", slug="alpha")]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(projection, "_artifact_reader", return_value=None):
            out, _ = projection.attach_projections(sessions, [], artifact_root=tmp, now=1000.0)
        self.assertIsNotNone(out[0].work_projection)


class ProcessTableScanScopeTest(unittest.TestCase):
    def _spawn_tagged(self, attempt):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                env=dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=attempt),
                                start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        return proc

    def test_scope_answers_like_the_single_shot_probes_from_one_walk(self):
        attempt = "att-fleet-hot-path-scope-fixture"
        proc = self._spawn_tagged(attempt)
        identity = dict(D.process_launch_identity(os.getpid()), attempt_id=attempt)
        single = None
        for _ in range(50):
            single = D.attempt_tagged_descendants(identity)
            if single.state == "populated":
                break
            time.sleep(0.1)
        self.assertEqual(single.state, "populated", single.reason)
        self.assertIn(proc.pid, [pid for pid, _s, _st in single.members])
        # The child is its own session leader, so its pgid is its pid.
        group_single = D.process_group_observation(proc.pid)
        self.assertEqual(group_single.state, "populated", group_single.reason)
        self.assertEqual(D.process_group_observation(-1).reason, "invalid-pgid")

        counter = _Counter(D.scan_process_table)
        with mock.patch.object(D, "scan_process_table", counter):
            with D.process_table_scan_scope():
                batched = [D.attempt_tagged_descendants(identity) for _ in range(5)]
                groups = [D.process_group_observation(proc.pid) for _ in range(5)]
                invalid = D.process_group_observation(0)
                # Another attempt / an empty group of the same pass share the walk.
                other = D.attempt_tagged_descendants(dict(identity, attempt_id="att-nobody"))
                empty_group = D.process_group_observation(2 ** 22 - 7)
                # SD-OPEN-47: the recorded parent leader is excluded per attempt, here too.
                excluded = D.attempt_tagged_descendants(dict(
                    identity, parent_pid=str(proc.pid),
                    parent_pid_start=D.process_start_ticks(proc.pid)))
                stale = D.attempt_tagged_descendants(dict(
                    identity, parent_pid=str(proc.pid), parent_pid_start="1"))
                with D.process_table_scan_scope():                 # nested: same walk
                    nested = D.attempt_tagged_descendants(identity)
        self.assertEqual(counter.calls, 1)
        for obs in batched + [nested]:
            self.assertEqual((obs.state, obs.members, obs.reason),
                             (single.state, single.members, single.reason))
        for obs in groups:
            self.assertEqual((obs.state, obs.members, obs.reason),
                             (group_single.state, group_single.members, group_single.reason))
        self.assertEqual(invalid.reason, "invalid-pgid")
        self.assertEqual(other.state, D.attempt_tagged_descendants(
            dict(identity, attempt_id="att-nobody")).state)
        self.assertEqual((empty_group.state, empty_group.members), ("empty", ()))
        self.assertEqual(excluded.state, "empty")
        self.assertEqual(stale.state, "populated")
        self.assertIsNone(D._PROCESS_TABLE_SCAN.get())          # the walk dies with the pass

    def test_scan_failures_keep_their_unverifiable_verdicts(self):
        metadata = {"attempt_id": "att-x"}
        with mock.patch.object(D, "scan_process_table",
                               return_value=D.ProcessTableScan({}, {}, error="procfs-enumeration:13")):
            with D.process_table_scan_scope():
                obs = D.attempt_tagged_descendants(metadata)
                group = D.process_group_observation(4242)
        self.assertEqual((obs.state, obs.reason), ("unverifiable", "procfs-enumeration:13"))
        self.assertEqual((group.state, group.reason), ("unverifiable", "procfs-enumeration:13"))
        partial = D.ProcessTableScan({"att-y": ((7, "1", "S"),)},
                                     {9: ((9, "1", "Z"),), 11: ((11, "1", "S"), (12, "1", "Z"))},
                                     incomplete_reason="procfs-member:9:malformed",
                                     group_incomplete_reason="procfs-member:8:13")
        with mock.patch.object(D, "scan_process_table", return_value=partial):
            with D.process_table_scan_scope():
                absent = D.attempt_tagged_descendants(metadata)
                present = D.attempt_tagged_descendants({"attempt_id": "att-y"})
                zombies_only = D.process_group_observation(9)
                live_group = D.process_group_observation(11)
                unknown_group = D.process_group_observation(13)
        self.assertEqual((absent.state, absent.reason),
                         ("unverifiable", "procfs-member:9:malformed"))
        self.assertEqual((present.state, present.members, present.reason),
                         ("populated", ((7, "1", "S"),), "procfs-member:9:malformed"))
        self.assertEqual((zombies_only.state, zombies_only.members, zombies_only.reason),
                         ("unverifiable", ((9, "1", "Z"),), "procfs-member:8:13"))
        self.assertEqual((live_group.state, live_group.reason), ("populated", "procfs-member:8:13"))
        self.assertEqual((unknown_group.state, unknown_group.reason),
                         ("unverifiable", "procfs-member:8:13"))
        self.assertEqual(D.attempt_tagged_descendants({}).reason, "attempt-id-missing")

    def test_collect_tick_takes_exactly_one_walk(self):
        counter = _Counter(D.scan_process_table)
        with tempfile.TemporaryDirectory() as tmp:
            jobs_log = os.path.join(tmp, "jobs.log")
            Path(jobs_log).write_text("", encoding="utf-8")
            with mock.patch.object(D, "scan_process_table", counter), \
                 mock.patch.object(dispatch_collector, "_scan_processes", return_value=[]), \
                 mock.patch.object(dispatch_collector, "_live_attempt_ids", return_value=set()):
                dispatch_collector.collect(jobs_path=jobs_log)
        self.assertEqual(counter.calls, 1)
        self.assertIsNone(D._PROCESS_TABLE_SCAN.get())


def _codex(pid, *, app_server=False, managed_dir=None, tag=None, harness="codex"):
    return SimpleNamespace(harness=harness, pid=pid, app_server=app_server,
                           managed_dir=managed_dir, session_tag=tag, steward=False)


_DIR_A = "/home/u/.codex/.harness/managed-sessions/session-h18c__79"
_DIR_B = "/home/u/.codex/.harness/managed-sessions/session-gsaa6r_a"


class ManagedCodexTagTest(unittest.TestCase):
    def test_app_server_row_shares_its_own_client_tag(self):
        server, client = _codex(100, app_server=True, managed_dir=_DIR_A), _codex(200, managed_dir=_DIR_A, tag="9c")
        other_server, other_client = _codex(300, app_server=True, managed_dir=_DIR_B), _codex(400, managed_dir=_DIR_B, tag="59")
        codex.share_managed_tags([server, client, other_server, other_client])
        self.assertEqual(server.session_tag, "9c")
        self.assertEqual(other_server.session_tag, "59")
        self.assertEqual((client.session_tag, other_client.session_tag), ("9c", "59"))
        self.assertFalse(getattr(server, "_session_tag_unpaired", False))

    def test_join_key_is_the_normalized_state_dir_not_the_thread_id(self):
        server = _codex(100, app_server=True, managed_dir=_DIR_A + "/")
        client = _codex(200, managed_dir=_DIR_A, tag="9c")
        client.session_id = "01a07b2b-9108-7000-8000-000000000000"
        codex.share_managed_tags([server, client])
        self.assertEqual(server.session_tag, "9c")
        self.assertFalse(hasattr(server, "session_id"))     # nothing else moves across

    def test_existing_tags_are_never_rewritten(self):
        server = _codex(100, app_server=True, managed_dir=_DIR_A, tag="ff")
        client = _codex(200, managed_dir=_DIR_A, tag="9c")
        codex.share_managed_tags([server, client])
        self.assertEqual((server.session_tag, client.session_tag), ("ff", "9c"))

    def test_ambiguous_or_missing_peers_stay_untagged_and_are_marked_unpaired(self):
        lonely = _codex(100, app_server=True, managed_dir=_DIR_A)
        crowded = _codex(300, app_server=True, managed_dir=_DIR_B)
        twins = [_codex(400, managed_dir=_DIR_B, tag="59"), _codex(500, managed_dir=_DIR_B, tag="ef")]
        untagged_client = _codex(600, managed_dir="/x/session-zzzz")
        its_server = _codex(700, app_server=True, managed_dir="/x/session-zzzz")
        plain_server = _codex(800, app_server=True)               # no state dir at all
        plain_tui = _codex(900)                                   # plain Codex TUI, not app-server
        claude = _codex(950, app_server=True, managed_dir=_DIR_A, harness="claude")
        rows = [lonely, crowded, *twins, untagged_client, its_server, plain_server, plain_tui, claude]
        codex.share_managed_tags(rows)
        for row in (lonely, crowded, its_server, plain_server):
            self.assertIsNone(row.session_tag)
            self.assertTrue(row._session_tag_unpaired)
        for row in (untagged_client, plain_tui, claude):
            self.assertIsNone(row.session_tag)
            self.assertFalse(getattr(row, "_session_tag_unpaired", False))
        self.assertEqual([t.session_tag for t in twins], ["59", "ef"])

    def test_collect_all_runs_the_share_pass_on_real_session_rows(self):
        from fleet.collectors import collect_all
        server = Session(harness="codex", pid=100, cwd="/tmp/p", app_server=True, managed_dir=_DIR_A)
        client = Session(harness="codex", pid=200, cwd="/tmp/p", managed_dir=_DIR_A)

        def enrich(sess, tick=None):
            if sess.pid == 200:
                sess.session_tag = "9c"

        from fleet.collectors import procscan
        with mock.patch.object(procscan, "scan", return_value=[server, client]), \
             mock.patch.object(codex, "prepare_tick", return_value=SimpleNamespace()), \
             mock.patch.object(codex, "enrich", side_effect=enrich), \
             mock.patch.object(dispatch_collector, "collect", return_value=[]):
            sessions, _jobs = collect_all(harness_filter=["codex"])
        by_pid = {s.pid: s for s in sessions}
        self.assertEqual(by_pid[100].session_tag, "9c")
        self.assertNotIn("_session_tag_unpaired", by_pid[100].to_dict())
        self.assertEqual(by_pid[100].to_dict()["session_tag"], "9c")


class UnpairedBadgeTest(unittest.TestCase):
    def _text(self, segs):
        return "".join(text for text, _style in segs)

    def test_unpaired_app_server_draws_a_visible_placeholder_in_the_same_slot(self):
        row = _codex(100, app_server=True, managed_dir=_DIR_A)
        row._session_tag_unpaired = True
        segs = render._session_tag_chip(row)
        self.assertEqual(self._text(segs), "[--] ")
        self.assertEqual(len(self._text(segs)), render._TAG_W)
        self.assertEqual(segs[1], ("--", "tag_dim"))

    def test_other_untagged_rows_keep_the_blank_slot_and_stewards_keep_their_star(self):
        blank = _codex(100)
        self.assertEqual(self._text(render._session_tag_chip(blank)), " " * render._TAG_W)
        steward = _codex(100, app_server=True)
        steward.steward = True
        steward._session_tag_unpaired = True
        self.assertEqual(self._text(render._session_tag_chip(steward)), "[* ] ")

    def test_a_shared_tag_renders_exactly_like_a_minted_one(self):
        paired = _codex(100, app_server=True, managed_dir=_DIR_A, tag="9c")
        paired._session_tag_unpaired = False
        self.assertEqual(self._text(render._session_tag_chip(paired)), "[9c] ")


if __name__ == "__main__":
    unittest.main()
