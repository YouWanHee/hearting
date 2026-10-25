"""Row-local evidence checks used by any later detail continuation."""

import unittest
from types import SimpleNamespace

from tools.fleet import details
from tools.fleet.model import DispatchJob, Session, WorkProjection
from tools.fleet.refresh import LiveSnapshot


def _job(route_id):
    row = DispatchJob(
        key="execute", slug="slug-" + route_id, cwd="/repo", route_id=route_id,
        route_hash="sha256:" + route_id[-1] * 64, route_file="/routes/" + route_id + ".json",
        route_node="execute", attempt_id="attempt-" + route_id, pid=200,
        proc_start=10, liveness="working", harness="codex")
    row.work_projection = WorkProjection(route_id=route_id)
    row.route_chain = {"nodes": [{"route_id": route_id}]}
    return row


def _snapshot(evidence, extra_route=False):
    jobs = [_job("route-1")]
    if extra_route:
        jobs.append(_job("route-2"))
    return LiveSnapshot(jobs=jobs, node_evidence=evidence, observations={
        "basic": {"state": "ok", "generation_identity": "basic-1"}})


def _completed_detail(snapshot, source_key=None):
    snapshot.observations["required-details"] = {
        "state": "ok", "generation_identity": "basic-1",
        "all_new": True, "observed_at": 100.0}
    return details.DetailSnapshot(source_key or details.state_key(snapshot), snapshot)


class Fleet2DetailContinuationBoundaryTests(unittest.TestCase):
    def test_optional_steward_detail_merges_by_exact_session_identity(self):
        basic_session = Session(harness="codex", pid=302, session_id="session-steward",
                                cwd="/repo", proc_start="start-302", liveness="working",
                                title="current title")
        basic = LiveSnapshot(
            sessions=[basic_session],
            observations={"basic": {"state": "ok", "generation_identity": "basic-1"}},
        )
        detail_session = Session(harness="codex", pid=302, session_id="session-steward",
                                  cwd="/repo", proc_start="start-302", liveness="working",
                                  title="current title", title_ts=123.0)
        detail_session.steward = True
        detail = details.DetailSnapshot(
            details.state_key(basic),
            LiveSnapshot(sessions=[detail_session], observations=basic.observations),
        )

        merged = details.merge(basic, detail)

        self.assertTrue(merged.sessions[0].steward)
        self.assertFalse(basic.sessions[0].steward)
        self.assertEqual(merged.sessions[0].title_ts, 123.0)
        self.assertIsNone(basic.sessions[0].title_ts)

    def test_required_detail_copy_isolates_mutable_rows_and_reuses_resources_read_only(self):
        resource = SimpleNamespace(parent_attempt_id="attempt-route-1", display_title=None)
        job = _job("route-1")
        job.resource_children = [resource]
        job.state_evidence = {"inputs": {"observed_liveness": {"state": "working"}}}
        session = Session(harness="codex", pid=301, session_id="session-1", liveness="working",
                          state_evidence={"inputs": {"observed_liveness": {"state": "working"}}})
        evidence = {"route-1": {"execute": {"state": "open"}}}
        original = LiveSnapshot(
            sessions=[session], jobs=[job], resources=[resource],
            route_entities=[_job("route-2")], node_evidence=evidence,
            observations={"basic": {"state": "ok", "generation_identity": "basic-1"}},
            tag_metadata=[{"agent": "codex"}],
        )

        copied = details._copy_for_enrichment(original, required_only=True)

        self.assertIsNot(copied, original)
        self.assertIsNot(copied.sessions[0], session)
        self.assertIsNot(copied.jobs[0], job)
        self.assertIsNot(copied.route_entities[0], original.route_entities[0])
        self.assertIs(copied.resources, original.resources)
        self.assertIs(copied.jobs[0].resource_children[0], resource)
        self.assertIs(copied.node_evidence, evidence)
        self.assertIsNot(copied.observations, original.observations)

        copied.jobs[0].state_evidence["inputs"]["observed_liveness"]["state"] = "changed"
        copied.observations["required-details"] = {"state": "ok"}
        self.assertEqual(job.state_evidence["inputs"]["observed_liveness"]["state"], "working")
        self.assertNotIn("required-details", original.observations)
        self.assertIsNone(resource.display_title)

    def test_changed_or_missing_neighbourhood_proof_rejects_the_row(self):
        previous = _snapshot({"route-1": {"execute": {"revision": 1}}})
        current = _snapshot({"route-1": {"execute": {"revision": 2}}})
        self.assertFalse(details.projection_current(
            current.jobs[0], previous.jobs[0], details.state_key(current),
            details.state_key(previous)))

        missing = _snapshot({})
        self.assertFalse(details.projection_current(
            missing.jobs[0], previous.jobs[0], details.state_key(missing),
            details.state_key(previous)))

    def test_unrelated_route_change_does_not_reject_this_rows_neighbourhood(self):
        evidence = {"route-1": {"execute": {"revision": 1}}}
        previous = _snapshot(evidence)
        current = _snapshot({
            **evidence,
            "route-2": {"execute": {"revision": 99}},
        }, extra_route=True)
        self.assertTrue(details.projection_current(
            current.jobs[0], previous.jobs[0], details.state_key(current),
            details.state_key(previous)))

    def test_terminal_or_changed_liveness_is_not_reusable(self):
        previous = _snapshot({"route-1": {"execute": {"revision": 1}}})
        current = _snapshot({"route-1": {"execute": {"revision": 1}}})
        current.jobs[0].liveness = "dead"
        self.assertFalse(details.projection_current(
            current.jobs[0], previous.jobs[0], details.state_key(current),
            details.state_key(previous)))

    def test_display_dto_round_trips_without_source_objects_and_merges_exact_row(self):
        old = _snapshot({"route-1": {"execute": {"revision": 1}}})
        old_source = details.state_key(old)
        old.jobs[0].title = "continued title"
        old.jobs[0].summary = "continued summary"
        old.jobs[0].summary_ts = 10.0
        old.jobs[0].route_chain = {"v": 1, "visible": True, "nodes": [
            {"label": "execute", "route_id": "route-1", "state": "open",
             "_record": {"private": "must-not-persist"},
             "route_file": "/private/route.json"}],
            "current": {"label": "execute", "route_id": "route-1", "state": "open"}}
        old.jobs[0].work_projection = WorkProjection(
            route_id="route-1", route_hash="sha256:" + "1" * 64,
            route_node="execute", stage_label="execute R1")
        payload = details.export_continuation(_completed_detail(old, old_source))
        self.assertIsNotNone(payload)
        serialized = str(payload)
        self.assertNotIn("private", serialized)
        self.assertNotIn("route_file", serialized)
        self.assertNotIn("/repo", serialized)

        current = _snapshot({"route-1": {"execute": {"revision": 1}}})
        continued = details.merge_continuation(current, details.restore_continuation(payload))
        self.assertTrue(continued.continuation_complete)
        merged = details.merge(current, continued)
        self.assertEqual(merged.jobs[0].title, "continued title")
        self.assertEqual(merged.jobs[0].summary, "continued summary")
        self.assertEqual(merged.jobs[0].work_projection.stage_label, "execute R1")
        self.assertEqual(merged.observations["required-details"]["state"], "continued")
        self.assertEqual(merged.jobs[0].liveness, current.jobs[0].liveness)

        fresh = _snapshot({"route-1": {"execute": {"revision": 1}}})
        fresh.jobs[0].summary = "fresh summary"
        fresh.jobs[0].summary_ts = 20.0
        fresh_detail = details.DetailSnapshot(details.state_key(current), fresh)
        replaced = details.merge(current, fresh_detail)
        self.assertEqual(replaced.jobs[0].summary, "fresh summary")

    def test_changed_row_is_rejected_without_losing_safe_unrelated_continuation(self):
        old = _snapshot({"route-1": {"execute": {"revision": 1}},
                         "route-2": {"execute": {"revision": 1}}}, extra_route=True)
        old.jobs[0].summary = "safe route one"
        old.jobs[0]._detail_activity_key = "activity-1"
        old.jobs[1].summary = "old route two"
        old.jobs[1]._detail_activity_key = "activity-2"
        payload = details.export_continuation(_completed_detail(old))
        self.assertIsNotNone(payload)

        current = _snapshot({"route-1": {"execute": {"revision": 1}},
                             "route-2": {"execute": {"revision": 2}}}, extra_route=True)
        current.jobs[0]._detail_activity_key = "activity-1"
        current.jobs[1]._detail_activity_key = "activity-2-changed"
        continued = details.merge_continuation(current, details.restore_continuation(payload))
        self.assertFalse(continued.continuation_complete)
        self.assertEqual(continued.snapshot.jobs[0].summary, "safe route one")
        self.assertIsNone(continued.snapshot.jobs[1].summary)
        self.assertEqual(continued.snapshot.observations["required-details"]["state"], "partial")

        changed_live = _snapshot({"route-1": {"execute": {"revision": 1}},
                                  "route-2": {"execute": {"revision": 1}}}, extra_route=True)
        changed_live.jobs[0]._detail_activity_key = "activity-1"
        changed_live.jobs[1]._detail_activity_key = "activity-2"
        changed_live.jobs[0].liveness = "dead"
        dead_result = details.merge_continuation(
            changed_live, details.restore_continuation(payload))
        self.assertFalse(dead_result.continuation_complete)
        self.assertIsNone(dead_result.snapshot.jobs[0].summary)

    def test_child_activity_is_part_of_the_continuation_witness(self):
        old = _snapshot({"route-1": {"execute": {"revision": 1}}})
        parent = old.jobs[0]
        parent.pid, parent.proc_start = 200, "parent-start"
        parent._runtime_session_id = "child-session"
        parent._child_session_associated = True
        old.sessions = [Session(harness="codex", pid=200, proc_start="parent-start",
                                session_id="child-session", is_child=True, liveness="working")]
        old_source = details.state_key(old)
        parent.summary = "from child"
        payload = details.export_continuation(_completed_detail(old, old_source))
        self.assertIsNotNone(payload)

        current = _snapshot({"route-1": {"execute": {"revision": 1}}})
        current.jobs[0].pid, current.jobs[0].proc_start = 200, "parent-start"
        current.jobs[0]._runtime_session_id = "child-session"
        current.sessions = [Session(harness="codex", pid=200, proc_start="parent-start",
                                    session_id="child-session", is_child=True,
                                    liveness="working")]
        unchanged = details.merge_continuation(current, details.restore_continuation(payload))
        self.assertTrue(unchanged.continuation_complete)

        current.sessions[0]._detail_activity_key = "new-child-activity"
        changed = details.merge_continuation(current, details.restore_continuation(payload))
        self.assertFalse(changed.continuation_complete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
