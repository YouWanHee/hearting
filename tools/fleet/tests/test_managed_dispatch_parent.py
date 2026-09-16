#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import fleet, render, model  # noqa: E402
from fleet.collectors import dispatch, resolve_parent_edges  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402


MANAGED = "/home/u/.codex/.harness/managed-sessions/session-live"


def flatten(lines):
    return "\n".join("".join(text for text, _key in line) for line in lines if line)


class ManagedDispatchParentTest(unittest.TestCase):
    def setUp(self):
        model.reset_parent_edge_tracker()

    def tearDown(self):
        model.reset_parent_edge_tracker()
        render.set_show_all(False)

    def session(self, sid="stale-visible-thread", pid=10, managed_dir=MANAGED):
        return Session(
            harness="codex", pid=pid, cwd="/work/repo", session_id=sid,
            slug="repo", title="managed parent", liveness="working",
            managed_dir=managed_dir,
        )

    def job(self, managed_dir=MANAGED, source="jobs"):
        return DispatchJob(
            key="code", slug="managed-owner", cwd="/work/repo-wt",
            parent_sid="current-thread-not-on-tui-row",
            parent_managed_dir=managed_dir,
            is_child=True, harness="codex", source=source,
            capability_mode="debug", qa="standard", liveness="working",
        )

    def rendered(self, sessions, job):
        return flatten(render._build_lines(
            sessions, [job], "both", False, 0, layout="wide", term_width=180,
        ))

    def test_unique_exact_managed_dir_recovers_parent(self):
        text = self.rendered([self.session()], self.job())
        self.assertIn("managed parent", text)
        self.assertIn("managed-owner", text)
        self.assertNotIn("orphaned dispatch rows", text)
        self.assertNotIn("(orphan)", text)

    def test_collector_hidden_current_thread_attaches_to_original_tui(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                model.reset_parent_edge_tracker()
                tui = self.session()
                job = self.job()
                server = self.session(sid=job.parent_sid, pid=11)
                server.app_server = True
                server._managed_client_present = True
                sessions = [server, tui] if reverse else [tui, server]
                resolve_parent_edges(sessions, [job])
                self.assertEqual(job._parent_edge_sid, tui.session_id)
                self.assertFalse(job._parent_edge_promoted_orphan)
                self.assertEqual(job.parent_sid, "current-thread-not-on-tui-row")
                text = self.rendered(sessions, job)
                self.assertIn("managed-owner", text)
                self.assertNotIn("(orphan)", text)
                self.assertNotIn("orphaned dispatch rows", text)

    def test_same_thread_client_wins_over_hidden_server_in_either_order(self):
        for reverse in (False, True):
            job = self.job()
            tui = self.session(sid=job.parent_sid)
            server = self.session(sid=job.parent_sid, pid=11)
            server.app_server = True
            server._managed_client_present = True
            sessions = [server, tui] if reverse else [tui, server]
            resolve_parent_edges(sessions, [job])
            self.assertEqual(job._parent_edge_sid, tui.session_id)
            self.assertFalse(job._parent_edge_promoted_orphan)

    def test_managed_edge_grace_keeps_original_authority_and_visible_identity(self):
        tui, job = self.session(), self.job()
        resolve_parent_edges([tui], [job])
        for _ in range(3):
            resolve_parent_edges([], [job])
            self.assertEqual(job._parent_edge_sid, tui.session_id)
        resolve_parent_edges([], [job])
        self.assertTrue(job._parent_edge_promoted_orphan)

    def test_dead_exact_parent_is_not_replaced_by_other_live_thread(self):
        job = self.job()
        dead = self.session(sid=job.parent_sid, pid=11)
        dead.liveness = "dead"
        resolve_parent_edges([dead, self.session()], [job])
        self.assertTrue(job._parent_edge_promoted_orphan)

    def test_child_or_unknown_identity_cannot_become_managed_parent(self):
        for kind in ("child", "sidless"):
            with self.subTest(kind=kind):
                tui, job = self.session(), self.job()
                if kind == "child":
                    tui.is_child = True
                else:
                    tui.session_id = None
                resolve_parent_edges([tui], [job])
                self.assertTrue(job._parent_edge_promoted_orphan)

    def test_mismatched_or_ambiguous_managed_dir_stays_orphan(self):
        mismatch = self.rendered(
            [self.session(managed_dir=MANAGED + "-other")], self.job(),
        )
        self.assertIn("(orphan)", mismatch)

        ambiguous = self.rendered(
            [self.session(pid=10), self.session(sid="another", pid=11)], self.job(),
        )
        self.assertIn("(orphan)", ambiguous)

    def test_plugin_queue_does_not_use_managed_dir_fallback(self):
        text = self.rendered([self.session()], self.job(source="plugin-queue"))
        self.assertIn("(orphan)", text)

    def test_exact_parent_sid_remains_stronger_than_managed_dir(self):
        parent = self.session(sid="exact-parent", managed_dir=MANAGED + "-other")
        job = self.job(managed_dir=MANAGED)
        job.parent_sid = "exact-parent"
        text = self.rendered([parent], job)
        self.assertNotIn("(orphan)", text)

    def test_registry_sidecar_path_is_normalized_and_preserved_in_json(self):
        sidecar = MANAGED + "/managed-sidecars/batch.jsonl"
        with tempfile.TemporaryDirectory() as td:
            jobs_path = os.path.join(td, "jobs.log")
            pipe = (
                "capability=autopilot-code,capability_mode=debug,qa=standard,"
                "harness=codex,parent_sid=current,parent_cwd=/work/repo,"
                "managed_sidecar_log=" + sidecar
            )
            with open(jobs_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "2026-08-10T00:00:00+00:00\topen\trepo\t/work/repo-wt\t"
                    "managed-owner\t" + pipe + "\n"
                )
            jobs, malformed = dispatch._scan_jobs_log(jobs_path, set())

        self.assertEqual(malformed, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].parent_managed_dir, MANAGED)
        payload = json.loads(fleet._snapshot_json([], jobs, []))
        self.assertEqual(payload["jobs"][0]["parent_managed_dir"], MANAGED)

    def test_malformed_sidecar_paths_fail_closed(self):
        self.assertIsNone(dispatch._managed_parent_dir("relative/managed-sidecars/x.jsonl"))
        self.assertIsNone(dispatch._managed_parent_dir(
            "/home/u/managed-sessions/session-live/managed-sidecars/x.jsonl"
        ))
        self.assertIsNone(dispatch._managed_parent_dir(
            MANAGED + "/wrong-dir/x.jsonl"
        ))


if __name__ == "__main__":
    unittest.main()
