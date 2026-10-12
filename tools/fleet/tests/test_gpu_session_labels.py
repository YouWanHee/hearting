"""GPU occupancy uses exact session names, never execution labels."""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render


def owner(harness="codex", sid="sid-one"):
    return {"kind": "session", "harness": harness, "id": sid,
            "label": "run:vacant-shared-epoch99-long-run-id"}


def session(harness="codex", sid="sid-one", name="TASLP 메인", alias=None):
    return SimpleNamespace(harness=harness, session_id=sid, runtime_name=name,
                           _herdr_name=alias, title="beta0 noaux epoch99",
                           registry_name="vacant", slug="shared", cwd="/work/epoch99")


class GPUSessionLabelsTest(unittest.TestCase):
    def setUp(self):
        for name in ("_COMPUTE_HOSTS", "_COMPUTE_HOSTS_SET_AT", "_ROUTE_FOLD"):
            self.addCleanup(setattr, render, name, getattr(render, name))
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: True}
        self.tags = {("codex", "sid-one"): "a1", ("codex", "sid-two"): "b2",
                     ("claude", "sid-one"): "c3", ("opencode", "sid-one"): "d4"}
        tag = mock.patch.object(render, "_resolve_session_tag",
                                side_effect=lambda h, sid: self.tags.get((h, sid)))
        tag.start()
        self.addCleanup(tag.stop)
        self.process = {"pid": 701, "proc_start": 22, "pgid": 700,
                        "session_owner": owner(),
                        "owner": {"kind": "run", "id": "beta0-vacant-shared-epoch99",
                                  "label": "run:beta0-vacant-shared-epoch99"},
                        "command": "/venv/bin/python /work/train_beta0.py --epochs 99"}
        self.gpu = {"index": 0, "name": "NVIDIA RTX 6000 Ada Generation",
                    "utilization_gpu_pct": 100, "memory_used_mib": 8192,
                    "memory_total_mib": 49152, "processes": [self.process], "reservations": []}
        self.host = {"host": "cnn", "reachable": True, "gpus": [self.gpu]}
        self.snapshot = {"configured": True, "hosts": [self.host]}

    def rows(self, sessions=(), width=200):
        render.set_compute_hosts(self.snapshot)
        return [render._plain(row) for row in render._compute_host_rows(width, sessions)]

    def test_run_named_vacant_keeps_actual_occupation_and_explicit_session_name(self):
        self.gpu["processes"] += [dict(self.process, pid=702), dict(self.process, pid=703)]
        before = json.dumps(self.snapshot, sort_keys=True)
        line = self.rows([session()])[2]
        self.assertIn("working", line)
        self.assertIn("▸ 3", line)
        self.assertEqual(line.count("TASLP 메인 [a1]"), 1)
        for forbidden in ("vacant", "shared", "epoch99", "beta0", "run:", "×"):
            self.assertNotIn(forbidden, line)
        self.assertEqual(json.dumps(self.snapshot, sort_keys=True), before)

    def test_two_exact_sessions_share_gpu_and_duplicate_processes_do_not_repeat_names(self):
        self.gpu["processes"] += [dict(self.process, pid=702),
                                  dict(self.process, pid=703, session_owner=owner(sid="sid-two"))]
        line = self.rows([session(), session(sid="sid-two", name="평가 담당")])[2]
        self.assertEqual(line.count("TASLP 메인 [a1]"), 1)
        self.assertEqual(line.count("평가 담당 [b2]"), 1)
        self.assertIn("▸ 3", line)

    def test_unbound_or_invalid_session_is_unattributed_while_gpu_remains_working(self):
        for claim in (None, owner("unknown"), {"kind": "session", "harness": "codex", "id": ""}):
            with self.subTest(claim=claim):
                self.process["session_owner"] = claim
                line = self.rows([session()])[2]
                self.assertIn("미귀속", line)
                self.assertIn("working", line)
                self.assertNotIn("idle", line)
                self.assertNotIn("TASLP", line)
                self.assertNotIn("vacant", line)

    def test_exact_session_without_collected_name_uses_tag_without_workload_fallback(self):
        for sessions in ([], [session(name=None)]):
            with self.subTest(sessions=sessions):
                line = self.rows(sessions)[2]
                self.assertIn("codex [a1]", line)
                for forbidden in ("vacant", "shared", "epoch99", "beta0", "sid-one"):
                    self.assertNotIn(forbidden, line)

    def test_verified_alias_is_used_and_explicit_name_has_priority(self):
        self.assertEqual(render._gpu_session_label(owner(), [session(name=None, alias="taslp-main-cx")]),
                         ("taslp-main-cx [a1]", "[a1]"))
        self.assertEqual(render._gpu_session_label(owner(), [session(alias="herdr alias")])[0],
                         "TASLP 메인 [a1]")
        self.assertEqual(render._gpu_session_label(owner(), [session(sid="sid-two", name="foreign")])[0],
                         "codex [a1]")

    def test_full_identity_and_harness_are_preserved_for_all_three_harnesses(self):
        for harness, tag in (("claude", "c3"), ("codex", "a1"), ("opencode", "d4")):
            with self.subTest(harness=harness):
                self.process["session_owner"] = owner(harness)
                line = self.rows([session(harness, name="학습 담당")])[2]
                self.assertIn("학습 담당 [%s]" % tag, line)
                self.assertNotIn("미귀속", line)
        self.tags["codex", "abcdefgh-one"] = "e5"
        self.tags["codex", "abcdefgh-two"] = "f6"
        self.gpu["processes"] = [dict(self.process, session_owner=owner(sid=sid))
                                  for sid in ("abcdefgh-one", "abcdefgh-two")]
        line = self.rows([session(sid="abcdefgh-one", name="첫 세션"),
                          session(sid="abcdefgh-two", name="둘째 세션")])[2]
        self.assertIn("첫 세션 [e5]", line)
        self.assertIn("둘째 세션 [f6]", line)

    def test_narrow_view_keeps_session_tags_and_metric_columns(self):
        before = self.rows(width=80)[2]
        self.gpu["processes"].append(dict(self.process, pid=702, session_owner=owner(sid="sid-two")))
        line = self.rows([session(name="아주 긴 사람이 지정한 이름"),
                          session(sid="sid-two", name="두 번째 아주 긴 이름")], width=80)[2]
        self.assertIn("[a1]", line)
        self.assertIn("[b2]", line)
        for token in ("UTIL", "%", "VRAM", "GB"):
            self.assertEqual(before.index(token), line.index(token))
        self.assertLessEqual(render._dw(line), 79)

    def test_expanded_process_command_keeps_script_and_actual_session_over_reservation_label(self):
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: False}
        self.gpu["reservations"] = [{"pid": 700, "owner": self.process["owner"],
                                    "run_id": "beta0-vacant-shared-epoch99",
                                    "started_at": 1728000000, "task": "training detail"}]
        line = self.rows([session()])[3]
        self.assertIn("TASLP 메인 [a1]", line)
        self.assertIn("train_beta0.py", line)
        self.assertIn("--epochs 99", line)
        self.assertNotIn("run:", line)
        self.assertNotIn("vacant", line)

    def test_idle_and_unknown_metrics_do_not_acquire_false_session_owners(self):
        self.gpu["processes"] = []
        line = self.rows([session()])[2]
        self.assertIn("idle", line)
        self.assertNotIn("미귀속", line)
        self.assertNotIn("TASLP", line)
        self.gpu["processes"] = [dict(self.process, session_owner=None,
                                      gpu_placement="visible-device-access")]
        self.gpu["observation_source"] = "proc-fallback"
        line = self.rows([session()])[2]
        self.assertIn("UTIL/VRAM unknown", line)
        self.assertIn("미귀속", line)
        self.assertNotIn("idle", line)

    def test_stale_observation_keeps_existing_expiry_and_never_rewrites_snapshot(self):
        before = copy.deepcopy(self.snapshot)
        with mock.patch.object(render.time, "monotonic", return_value=10):
            render.set_compute_hosts(self.snapshot)
            self.assertIn(("codex", "sid-one"), render._gpu_session_resources())
        with mock.patch.object(render.time, "monotonic", return_value=41):
            self.assertEqual(render._gpu_session_resources(), {})
            render._compute_host_rows(200, [session()])
        self.assertEqual(self.snapshot, before)


if __name__ == "__main__":
    unittest.main()
