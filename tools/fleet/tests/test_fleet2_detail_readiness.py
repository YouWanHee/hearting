"""First-screen detail readiness is explicit and bound to the current basic pass."""

import copy
import unittest
from unittest import mock

from tools.fleet import details, projection, route_chain
from tools.fleet.collectors import _usage_cache_observation
from tools.fleet.model import DispatchJob, WorkProjection
from tools.fleet.projection import required_projection_details_complete
from tools.fleet.refresh import (
    LiveSnapshot,
    required_details_all_new,
    required_details_complete,
    set_required_details,
)


def _basic(identity):
    return LiveSnapshot(observations={
        "basic": {"state": "ok", "generation_identity": identity,
                  "observed_at": 100.0, "all_new": True},
        "required-details": {"state": "pending", "generation_identity": identity,
                              "all_new": False},
    })


class Fleet2DetailReadinessTests(unittest.TestCase):
    def test_required_details_need_success_and_exact_basic_identity(self):
        pending = _basic("basic-1")
        self.assertFalse(pending.required_details_ready)
        self.assertFalse(required_details_complete(pending.observations))

        set_required_details(pending, "ok", all_new=True, observed_at=101.0)
        self.assertTrue(pending.required_details_ready)
        self.assertTrue(required_details_all_new(pending.observations))

        pending.observations["required-details"]["generation_identity"] = "basic-older"
        self.assertFalse(pending.required_details_ready)

    def test_readiness_and_all_new_are_distinct(self):
        continued = _basic("basic-2")
        set_required_details(continued, "ok", all_new=False, observed_at=102.0)
        self.assertTrue(continued.required_details_ready)
        self.assertFalse(continued.required_details_all_new)

    def test_usage_cache_read_completes_while_http_refresh_remains_pending(self):
        observed = _usage_cache_observation(
            {"freshness": "stale", "observed_at": 99.0}, refresh_pending=True)
        self.assertEqual(observed["state"], "ok")
        self.assertEqual(observed["cache_state"], "stale")
        self.assertEqual(observed["refresh_state"], "pending")

        failed = _usage_cache_observation(None)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["attempted"])

    def test_required_projection_rejects_missing_route_proof_and_required_read_errors(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-1")
        row.work_projection = WorkProjection(
            route_id="route-1", route_hash="sha256:route",
            _route_view={"record": {"route_id": "route-1", "route_hash": "sha256:route"}})
        self.assertTrue(required_projection_details_complete([row]))

        row._required_detail_error = True
        self.assertFalse(required_projection_details_complete([row]))
        row._required_detail_error = False
        row.work_projection = WorkProjection(route_id="route-1", ambiguity="route-record-mismatch")
        self.assertFalse(required_projection_details_complete([row]))

    def test_validated_continuation_needs_matching_route_binding(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-2",
                          route_hash="sha256:route-2")
        row._continuation_validated = True
        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:route-2")
        self.assertTrue(required_projection_details_complete([row]))

        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:stale")
        self.assertFalse(required_projection_details_complete([row]))

    def test_merge_only_adopts_details_from_the_current_basic_identity(self):
        current = _basic("basic-current")
        completed = copy.deepcopy(current)
        set_required_details(completed, "ok", all_new=True, observed_at=103.0)
        merged = details.merge(
            current, details.DetailSnapshot(details.state_key(current), completed))
        self.assertTrue(merged.required_details_ready)
        self.assertTrue(merged.required_details_all_new)

        old_basic = _basic("basic-old")
        old_detail = copy.deepcopy(old_basic)
        set_required_details(old_detail, "ok", all_new=True, observed_at=104.0)
        newer_basic = _basic("basic-newer")
        stale = details.merge(
            newer_basic, details.DetailSnapshot(details.state_key(old_basic), old_detail))
        self.assertFalse(stale.required_details_ready)
        self.assertEqual(stale.observations["required-details"]["state"], "pending")

    def test_complete_basic_pass_skips_a_second_required_detail_pass(self):
        source = _basic("basic-complete")
        set_required_details(source, "ok", all_new=True, observed_at=105.0)
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts",
                           return_value=None) as deliveries, \
                mock.patch("tools.fleet.collectors.dispatch._candidate_jobs_paths",
                           return_value=[]), \
                mock.patch("tools.fleet.collectors.peer_messages.collect", return_value={}) as peers, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required=False)

        route_enrich.assert_not_called()
        attach.assert_not_called()
        degradations.assert_not_called()
        campaigns.assert_called_once()
        child_titles.assert_called_once()
        title_details.assert_called_once()
        work_titles.assert_called_once()
        deliveries.assert_called_once()
        peers.assert_called_once()
        panes.assert_called_once()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_required_only_pass_defers_optional_detail_sources(self):
        source = _basic("basic-pending")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts") as deliveries, \
                mock.patch("tools.fleet.collectors.peer_messages.collect") as peers, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required_only=True)

        route_enrich.assert_called_once()
        attach.assert_called_once()
        degradations.assert_called_once()
        for optional in (campaigns, child_titles, title_details, work_titles,
                         deliveries, peers, panes):
            optional.assert_not_called()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_required_detail_keeps_default_report_verification_policy(self):
        source = _basic("basic-report-policy")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations",
                           return_value={}), \
                mock.patch.object(route_chain, "enrich"), \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch.object(projection, "required_projection_details_complete",
                                  return_value=True):
            details._enrich(source, required_only=True)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], True)

            attach.reset_mock()
            details._enrich(source, required_only=True,
                            include_report_verification=False)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
