"""F-83 (2026-08-26): a supervisor-closed dead depth-1 owner stays on the board — as a ✕
card under its session — while its route still has open work, instead of aging off with
the afterglow window and leaving the session to the legacy multi-line stage surface."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tools.fleet.model import DispatchJob

from tools.fleet.collectors import dispatch

_ROW = ("2026-07-19T00:00:00Z\tdone\t/r\t/w\t{slug}\t"
        "route_id={rid},route_file={rf},owner_route_id={rid},owner_route_file={rf},"
        "owner_route_hash=sha256:f83,"
        "worker_type=owner,attempt_id={att},pid=999999990,pid_start=1,"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,note={note},failure_class=runtime\n")


class DeadTerminalOwnerTest(unittest.TestCase):
    def test_closed_launch_route_does_not_hide_open_adopted_owner_route(self):
        from datetime import datetime, timezone, timedelta
        from route_identity import route_hash
        from owner_route_binding import OwnerRouteBinding

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            old = Path(self._route(td, "rt-old"))
            record = json.loads(old.read_text())
            record["route_hash"] = route_hash(record)
            old.write_text(json.dumps(record))
            old.with_suffix(".outcome.json").write_text(json.dumps({
                "route_id": "rt-old", "route_hash": record["route_hash"],
                "terminal_gate_proven": False}))
            current = Path(td, "current.json")
            successor = dict(record, route_id="rt-current")
            successor["route_hash"] = route_hash(successor)
            current.write_text(json.dumps(successor))
            fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            row = _ROW.format(slug="owner-advanced", rid="rt-old", rf=old,
                              att="att-advanced", note="dead-worker-blocked")
            row = row.replace("2026-07-19T00:00:00Z", fresh).replace(
                "owner_route_hash=sha256:f83,",
                "route_hash={0},owner_route_hash={0},".format(record["route_hash"]))
            binding = OwnerRouteBinding(str(current), "rt-current", successor["route_hash"])
            registry = dispatch._orphan_registry_module()
            with mock.patch("owner_route_binding.resolve_owner_route_lifecycle",
                            return_value=(binding, "owner-route-advance-current")), \
                    mock.patch.object(registry, "resolve_owner_route",
                                      return_value=("rt-current", str(current), "ok")):
                jobs = self._collect(td, home, [row])
                self.assertIn("owner-advanced", [j.slug for j in jobs])
                current.with_suffix(".outcome.json").write_text(json.dumps({
                    "route_id": "rt-current", "route_hash": successor["route_hash"],
                    "terminal_gate_proven": False}))
                jobs = self._collect(td, home, [row])
                self.assertNotIn("owner-advanced", [j.slug for j in jobs])

    def test_exact_route_closure_ends_retention_without_completing_markers(self):
        from datetime import datetime, timezone, timedelta
        from route_identity import route_hash

        for binding in ("direct", "owner"):
            for closure in ("closed", "missing", "malformed", "foreign-id", "foreign-hash", "pending"):
                with self.subTest(binding=binding, closure=closure), tempfile.TemporaryDirectory() as td:
                    home = os.path.join(td, "home")
                    rid = "rt-f83-explicit-close"
                    self._markers(home, rid, ["plan"])
                    rf = Path(self._route(td, rid))
                    record = json.loads(rf.read_text())
                    record.update(cwd="/w", repo="/r", capability="autopilot-code",
                                  capability_mode="dev", artifact_root=str(Path(td) / "artifacts"))
                    record["route_hash"] = route_hash(record)
                    rf.write_text(json.dumps(record))
                    outcome = rf.with_suffix(".outcome.json")
                    closed = {"route_id": rid, "route_hash": record["route_hash"],
                              "terminal_gate_proven": False}
                    if closure == "foreign-id":
                        closed["route_id"] = "rt-other"
                    if closure == "foreign-hash":
                        closed["route_hash"] = "sha256:other"
                    if closure == "pending":
                        closed["inline_finish_id"] = "finish-pending"
                    if closure != "missing":
                        outcome.write_text("{" if closure == "malformed" else json.dumps(closed))
                    fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
                    row = _ROW.format(slug="owner-f83-closed", rid=rid, rf=rf,
                                      att="att-f83-closed", note="dead-worker-blocked")
                    row = row.replace("2026-07-19T00:00:00Z", fresh).replace(
                        "owner_route_hash=sha256:f83,",
                        "route_hash={0},owner_route_hash={0},".format(record["route_hash"]))
                    row = row.rstrip("\n") + (
                        ",unit=_kernel/owner,capability=autopilot-code,capability_mode=dev,"
                        "artifact_root=" + record["artifact_root"] + "\n")
                    if binding == "owner":
                        row = row.replace("route_id={0},route_file={1},".format(rid, rf), "", 1)
                    rows = [row]
                    if binding == "owner":
                        # Historical owner-only rows derive node context from
                        # their exact registered children, as real owners do.
                        rows.append(f"{fresh}\tdone\t/r\t/w\towner-f83-closed-plan\t"
                                    "worker_type=stage,attempt_id=att-f83-plan,"
                                    "parent=owner-f83-closed,parent_attempt_id=att-f83-closed,"
                                    f"route_id={rid},route_file={rf},route_node=plan\n")
                    before = {p: p.read_bytes() for p in (rf, outcome) if p.exists()}
                    marker = Path(home) / ".dispatch/completion" / rid / "plan.json"
                    before[marker] = marker.read_bytes()
                    with mock.patch("inline_finish.pending_state", return_value={
                            "inline_finish_id": "finish-pending", "state": "finishing"}):
                        jobs = self._collect(td, home, rows)
                    retained = [j for j in jobs if j.slug == "owner-f83-closed"]
                    self.assertEqual(bool(retained), closure != "closed")
                    self.assertEqual(Path(td, "jobs.log").read_text(), "".join(rows))
                    self.assertEqual({p: p.read_bytes() for p in before}, before)
                    self.assertFalse((marker.parent / "execute.json").exists())

    def test_repeated_lineage_sources_are_memoized_within_one_retention_pass(self):
        path = "/dispatch/jobs.log"
        route_file = "/routes/shared.json"
        def owner_row(order, attempt):
            return {"order": order, "worktree": "/work", "meta": {
                "owner_route_id": "rt-shared", "owner_route_file": route_file,
                "route_id": "rt-shared", "route_file": route_file,
                "capability": "autopilot-code", "worker_type": "owner",
                "attempt_id": attempt,
            }}

        rows = [owner_row(0, "dead-a"), owner_row(1, "dead-b"),
                owner_row(2, "newer")]
        jobs = []
        for attempt in ("dead-a", "dead-b"):
            job = DispatchJob(key="owner", slug=attempt, attempt_id=attempt,
                              elapsed_min=1, liveness="dead")
            job._dead_terminal_owner = True
            job._registry_path = path
            jobs.append(job)

        with mock.patch.object(dispatch, "_orphan_registry_module", return_value=object()), \
                mock.patch.object(dispatch, "_orphan_registry_rows_for_pass", return_value=rows), \
                mock.patch.object(dispatch, "_route_lineage", return_value={"rt-shared"}) as lineage:
            retained = dispatch._retain_dead_terminal_owners(
                jobs, now=0, jobs_path=path)

        self.assertEqual(retained, [])
        self.assertEqual(lineage.call_count, 1)

    def _route(self, td, rid):
        record = {"route_id": rid, "nodes": [
            {"id": "plan", "depends_on": []}, {"id": "execute", "depends_on": ["plan"]},
            {"id": "test", "depends_on": ["execute"]}, {"id": "report", "depends_on": ["test"]}]}
        path = os.path.join(td, "route.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return path

    def _markers(self, home, rid, nodes):
        d = os.path.join(home, ".dispatch", "completion", rid)
        os.makedirs(d, exist_ok=True)
        for n in nodes:
            with open(os.path.join(d, "%s.json" % n), "w", encoding="utf-8") as fh:
                json.dump({"node_id": n}, fh)

    def _collect(self, td, home, rows):
        jobs_path = os.path.join(td, "jobs.log")
        with open(jobs_path, "w", encoding="utf-8") as fh:
            fh.write("".join(rows))
        with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]), \
             mock.patch.dict(os.environ, {"AGENT_HOME": home}):
            return dispatch.collect(jobs_path=jobs_path)

    def test_route_open_dead_owner_is_retained_as_dead_card(self):
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-open"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
            row = _ROW.format(slug="owner-f83", rid=rid, rf=rf, att="att-f83-owner",
                              note="dead-runtime-exit").replace("2026-07-19T00:00:00Z", fresh)
            jobs = self._collect(td, home, [row])
            owner = next(j for j in jobs if j.slug == "owner-f83")
            self.assertEqual(owner.liveness, "dead")
            self.assertEqual(owner.note, "dead-runtime-exit")
            self.assertEqual(owner.resume_boundary, "execute")
            self.assertTrue(owner._dead_terminal_owner)
            self.assertFalse(owner.afterglow)

    def test_route_complete_dead_owner_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-done"
            self._markers(home, rid, ["plan", "execute", "test", "report"])
            rf = self._route(td, rid)
            jobs = self._collect(td, home, [_ROW.format(
                slug="owner-f83-done", rid=rid, rf=rf, att="att-f83-done", note="dead-runtime-exit")])
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-done"], [])

    def test_superseded_dead_owner_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-sup"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            rows = [_ROW.format(slug="owner-f83-r1", rid=rid, rf=rf, att="att-f83-r1",
                                note="dead-runtime-exit"),
                    _ROW.format(slug="owner-f83-r2", rid=rid, rf=rf, att="att-f83-r2",
                                note="dead-runtime-exit").replace("\tdone\t", "\topen\t")]
            jobs = self._collect(td, home, rows)
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-r1"], [])

    def test_plain_done_owner_still_ages_off(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-plain"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            row = _ROW.format(slug="owner-f83-plain", rid=rid, rf=rf, att="att-f83-plain",
                              note="completed-supervisor").replace(",failure_class=runtime", "")
            jobs = self._collect(td, home, [row])
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-plain"], [])

    def test_continuation_lineage_supersedes_dead_owner(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            self._markers(home, "rt-f83-src", ["plan"])
            self._markers(home, "rt-f83-cont", ["plan"])
            routes = os.path.join(td, "routes"); os.makedirs(routes)
            src = os.path.join(routes, "rt-f83-src.json")
            cont = os.path.join(routes, "rt-f83-cont.json")
            nodes = [{"id": "plan", "depends_on": []}, {"id": "execute", "depends_on": ["plan"]},
                     {"id": "test", "depends_on": ["execute"]}, {"id": "report", "depends_on": ["test"]}]
            with open(src, "w", encoding="utf-8") as fh:
                json.dump({"route_id": "rt-f83-src", "nodes": nodes}, fh)
            with open(cont, "w", encoding="utf-8") as fh:
                json.dump({"route_id": "rt-f83-cont", "nodes": nodes,
                           "source_route_supersession": {"from_route_id": "rt-f83-src"}}, fh)
            rows = [_ROW.format(slug="owner-f83-src", rid="rt-f83-src", rf=src, att="att-f83-src",
                                note="dead-runtime-exit"),
                    _ROW.format(slug="owner-f83-cont", rid="rt-f83-cont", rf=cont, att="att-f83-cont",
                                note="dead-runtime-exit").replace("\tdone\t", "\topen\t")]
            jobs = self._collect(td, home, rows)
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-src"], [])
            self.assertEqual(dispatch._route_lineage("rt-f83-cont", cont), {"rt-f83-cont", "rt-f83-src"})

    def test_new_route_same_worktree_supersedes_dead_owner(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            self._markers(home, "rt-f83-old", ["plan"])
            rf_old = self._route(td, "rt-f83-old")
            rows = [_ROW.format(slug="owner-f83-old", rid="rt-f83-old", rf=rf_old,
                                att="att-f83-old", note="dead-runtime-exit"),
                    _ROW.format(slug="owner-f83-new", rid="rt-f83-new", rf="/absent.json",
                                att="att-f83-new", note="dead-runtime-exit").replace("\tdone\t", "\topen\t")]
            jobs = self._collect(td, home, rows)
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-old"], [])

    def test_dead_owner_older_than_six_hours_folds_away(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            self._markers(home, "rt-f83-aged", ["plan"])
            rf = self._route(td, "rt-f83-aged")
            row = _ROW.format(slug="owner-f83-aged", rid="rt-f83-aged", rf=rf,
                              att="att-f83-aged", note="dead-runtime-exit")
            jobs = self._collect(td, home, [row])
            # fixture timestamp 2026-07-19 is far older than the 6h retention window
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-aged"], [])


if __name__ == "__main__":
    unittest.main()
