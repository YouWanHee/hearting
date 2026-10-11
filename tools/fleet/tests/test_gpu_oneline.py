"""Folded GPU rows summarize observations while preserving expanded detail."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render


def owner(harness, tag):
    return {"kind": "session", "harness": harness, "id": tag}


class GPUOneLineTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, render, "_ROUTE_FOLD", render._ROUTE_FOLD)
        self.addCleanup(setattr, render, "_COMPUTE_HOSTS", render._COMPUTE_HOSTS)
        self.addCleanup(setattr, render, "_COMPUTE_HOSTS_SET_AT", render._COMPUTE_HOSTS_SET_AT)
        self.addCleanup(setattr, render, "_FOLDABLE", render._FOLDABLE)
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: True}
        self.tag = mock.patch.object(render, "_resolve_session_tag", side_effect=lambda h, sid: sid)
        self.tag.start()
        self.addCleanup(self.tag.stop)
        owners = [owner("codex", "bb")] * 3 + [owner("claude", "8e")]
        self.gpu = {"index": 1, "name": "NVIDIA RTX 6000 Ada Generation",
                    "utilization_gpu_pct": 100, "memory_used_mib": 23552,
                    "memory_total_mib": 49152,
                    "processes": [{"pid": n + 10, "session_owner": o,
                                   "command": "python train.py --config detailed-name.yaml"}
                                  for n, o in enumerate(owners)],
                    "reservations": [{"owner": {**o, "label": "saved [%s]" % o["id"]},
                                      "started_at": 1728000000, "task": "detailed task"}
                                     for o in owners]}
        self.host = {"host": "moving4", "reachable": True, "cpu_count": 64,
                     "gpus": [self.gpu]}
        render.set_compute_hosts({"configured": True, "hosts": [self.host]})

    def rows(self, width):
        return [render._plain(row) for row in render._compute_host_rows(width)]

    def test_matching_runs_fold_to_one_line_and_sorted_session_counts(self):
        for width in (80, 200):
            with self.subTest(width=width):
                lines = self.rows(width)
                self.assertEqual(len(lines), 3)
                self.assertIn("▸ 4 · [bb]×3 [8e]×1", lines[2])
                self.assertNotIn("reserved", lines[2])
                self.assertNotIn("detailed", "\n".join(lines))
                self.assertLessEqual(render._dw(lines[2]), width - 1)

    def test_narrow_summary_omits_whole_sessions_without_shifting_metrics(self):
        before = self.rows(80)[2]
        for n, harness in enumerate(("codex", "claude", "opencode")):
            o = owner(harness, "a%d" % n)
            self.gpu["processes"].append({"session_owner": o})
            self.gpu["reservations"].append({"owner": o})
        line = self.rows(80)[2]
        self.assertIn("[bb]×3 +4", line)
        for token in ("UTIL", "%", "VRAM", "GB"):
            self.assertEqual(before.index(token), line.index(token))
        self.assertLessEqual(render._dw(line), 79)

    def test_same_total_with_different_owners_is_a_mismatch(self):
        self.gpu["reservations"][-1]["owner"] = owner("opencode", "ff")
        for width in (80, 200):
            line = self.rows(width)[2]
            self.assertIn("res", line)
            self.assertIn("unres", line)
            self.assertIn("+1", line)
            self.assertLessEqual(render._dw(line), width - 1)

    def test_reserved_only_gpu_is_foldable_and_process_only_is_labelled(self):
        self.gpu["processes"] = []
        line = self.rows(80)[2]
        self.assertIn("reserved+4", line)
        self.assertEqual(len(render._compute_host_rows.fold_rows), 1)
        self.gpu["processes"] = [{"owner": owner("opencode", "ff")}]
        self.gpu["reservations"] = []
        self.assertIn("unreserved+1", self.rows(80)[2])

    def test_unknown_queries_do_not_claim_a_reservation_mismatch(self):
        reservations = self.gpu.pop("reservations")
        self.assertNotIn("unreserved+", self.rows(200)[2])
        self.gpu["reservations"] = reservations
        self.gpu["processes"] = []
        self.host["process_detail"] = "query failed"
        self.assertNotIn("reserved+", self.rows(200)[2])
        self.host.pop("process_detail")
        self.gpu["observation_source"] = "proc-fallback"
        self.assertNotIn("reserved+", self.rows(200)[2])
        self.gpu.pop("observation_source")
        self.gpu["processes"] = [{"session_owner": owner("opencode", "ff")}]
        self.gpu["reservations"] = []
        self.host["reservation_detail"] = "leases unreadable"
        self.assertNotIn("unreserved+", self.rows(200)[2])

    def test_expanded_detail_and_toggle_keep_original_payload(self):
        original = copy.deepcopy(self.gpu)
        render._FOLDABLE = list(render._compute_host_rows.fold_rows)
        render._toggle_gpu_commands()
        lines = self.rows(200)
        self.assertEqual(len(lines), 11)
        self.assertIn("▾ 4", lines[2])
        self.assertEqual(sum("reserved saved" in line for line in lines), 4)
        self.assertEqual(sum("↳" in line for line in lines), 4)
        self.assertIn("10-04", "\n".join(lines))
        self.assertIn("detailed task", "\n".join(lines))
        self.assertIn("detailed-name.yaml", "\n".join(lines))
        self.assertEqual(original, self.gpu)


if __name__ == "__main__":
    unittest.main()
