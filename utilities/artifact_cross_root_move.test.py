#!/usr/bin/env python3
"""Cross-root moves on isolated roots; no real TF registries or payloads."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import artifact_cross_root_move as X
import artifact_producer as P
import artifact_campaign as C
import artifact_locator as L
import artifact_reader as Reader

spec = importlib.util.spec_from_file_location("cross_move_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mtime_ns,
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class CrossRootMoveTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.source = self.root
        self.activate()
        self.target = Path(self._tmp.name) / "target"
        self.target.mkdir()
        self.root = self.target
        P.activate(self.target, repository_id="repo_" + "d" * 32, artifact_root_id="root_" + "e" * 32,
                   w7={"campaign_id": "camp_" + "c" * 32})
        self.root = self.source
        self.dst = self.make(self.target, "collision", closed=False)
        self.src = self.make(self.source, "collision", closed=True)

    def make(self, root, slug, *, closed=False, parent=None):
        previous, self.root = self.root, root
        try:
            route, path = self.route(slug=slug, gate_source=slug)
            result = P.begin(root, route_file=path, capability="autopilot-code", intensity="direct",
                             campaign_key="same-key", parent_cycle_id=parent)
            self.write_output(result)
            if closed:
                self.close(route, path)
                P.finalize(root, cycle_id=result["cycle_id"])
            return dict(result, route_file=str(path))
        finally:
            self.root = previous

    def move(self, **kwargs):
        return P.cycle_move(self.source, source_campaign=self.src["campaign_id"],
                            target_artifact_root=self.target, campaign=self.dst["campaign_id"], **kwargs)

    def test_eight_cycles_merge_snapshots_support_ids_and_target_work(self):
        second = self.make(self.source, "second-collision", closed=True)
        target_second = self.make(self.target, "second-collision")
        extra = [self.make(self.source, "extra-" + str(i), closed=i % 2 == 0) for i in range(6)]
        selected = [self.src, second, *extra]
        manifests = {}
        for row in selected:
            path = Path(row["cycle_dir"]) / "manifest.json"
            if path.exists():
                manifests[row["cycle_id"]] = path.read_bytes()
        live_route = Path(self.dst["route_file"]).read_bytes()
        live_record = P.cycle_record_path(self.target, self.dst["cycle_id"]).read_bytes()
        source_routes = {row["route_file"]: Path(row["route_file"]).read_bytes() for row in selected}
        out = self.move()
        self.assertEqual(len(out["cycle_ids"]), 8)
        if os.environ.get("HEARTING_CROSS_MOVE_TEST_EVIDENCE"):
            Path(os.environ["HEARTING_CROSS_MOVE_TEST_EVIDENCE"]).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        self.assertNotIn(self.src["campaign_id"], [row["campaign_id"] for row in P.list_campaign_summaries(self.source)])
        self.assertIn(self.dst["campaign_id"], [row["campaign_id"] for row in P.list_campaign_summaries(self.target)])
        self.assertEqual(P.read_campaign(self.target, self.dst["campaign_id"])["cycles"],
                         [self.dst["cycle_id"], target_second["cycle_id"], *[row["cycle_id"] for row in selected]])
        self.assertEqual(C.campaign_state(self.source, Path(self.src["cycle_dir"]).parent / "campaign.json").state, "superseded")
        for row in selected:
            record = P.read_cycle_record(self.target, row["cycle_id"])
            landed = P.cycle_dir(self.target, record["campaign_id"], row["cycle_id"], record)
            if row in [self.src, second]:
                self.assertTrue(landed.name.endswith("-2"))
            if row["cycle_id"] in manifests:
                before = json.loads(manifests[row["cycle_id"]])
                after = json.loads((landed / "manifest.json").read_bytes())
                self.assertEqual(before["events"], after["events"])
                self.assertEqual(before["artifact_revisions"], after["artifact_revisions"])
                preserved = P.producer_dir(self.target) / "manifests" / row["cycle_id"] / (before["manifest_revision_id"] + ".json")
                self.assertEqual(preserved.read_bytes(), manifests[row["cycle_id"]])
            self.assertFalse(Path(row["cycle_dir"]).exists())
            self.assertEqual(Path(row["route_file"]).read_bytes(), source_routes[row["route_file"]])
            self.assertFalse((self.target / ".runtime/routes" / Path(row["route_file"]).name).exists())
        self.assertEqual(Path(self.dst["route_file"]).read_bytes(), live_route)
        self.assertEqual(P.cycle_record_path(self.target, self.dst["cycle_id"]).read_bytes(), live_record)
        before = snapshot(self.target)
        self.assertEqual(self.move()["operation_id"], out["operation_id"])
        self.assertEqual(snapshot(self.target), before)
        self.assertTrue(any("routes/" in name for name in out["support_inventory"]))

    def test_dry_run_and_historical_read_are_observational_writers_stay_local(self):
        before = [snapshot(self.source), snapshot(self.target)]
        plan = self.move(dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertEqual([snapshot(self.source), snapshot(self.target)], before)
        self.move()
        before = [snapshot(self.source), snapshot(self.target)]
        result = L.resolve_historical(self.source, self.src["cycle_id"])
        self.assertEqual(result["original"]["cycle_id"], self.src["cycle_id"])
        self.assertEqual(result["canonical"]["artifact_root"], str(self.target))
        campaign = L.resolve_historical(self.source, self.src["campaign_id"])
        self.assertEqual(campaign["original"]["campaign_id"], self.src["campaign_id"])
        self.assertEqual(campaign["canonical"]["campaign_id"], self.dst["campaign_id"])
        for selector, value in [("--cycle", self.src["cycle_id"]), ("--campaign", self.src["campaign_id"])]:
            proc = subprocess.run([sys.executable, str(Path(Reader.__file__)), "resolve", "--artifact-root", str(self.source), selector, value], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["resolution"], "relocated")
        old = str(Path(self.src["cycle_dir"]).relative_to(self.source) / "artifacts/plans/cycle/plan.md")
        self.assertEqual(Reader.resolve_path(self.source, old)["resolution"], "relocated")
        self.assertEqual([snapshot(self.source), snapshot(self.target)], before)
        with self.assertRaises(P.ProducerError):
            P.cycle_move(self.source, self.src["cycle_id"], no_parent=True)
        with self.assertRaises(P.ProducerError):
            P.finalize(self.source, cycle_id=self.src["cycle_id"])
        self.assertEqual(snapshot(self.target), before[1])

    def test_retry_after_every_publication_phase_preserves_new_membership(self):
        for phase in ["stage", "publish", "metadata", "closure", "source-cleanup", "history"]:
            with self.subTest(phase=phase):
                # Each phase starts a fresh isolated operation, not a manual journal edit.
                source = self.make(self.source, "retry-" + phase, closed=False)
                def fail(point):
                    if point == phase:
                        raise RuntimeError("injected " + phase)
                with mock.patch.object(X, "_fault", side_effect=fail), self.assertRaises(RuntimeError):
                    P.cycle_move(self.source, source["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])
                extra = self.make(self.target, "concurrent-" + phase)
                result = P.cycle_move(self.source, source["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])
                self.assertEqual(result["status"], "moved")
                self.assertIn(extra["cycle_id"], P.read_campaign(self.target, self.dst["campaign_id"])["cycles"])
                self.assertEqual(P.cycle_move(self.source, source["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])["operation_id"], result["operation_id"])

    def test_attachments_symlinks_and_replay_no_duplicate(self):
        directory = self.source / "campaigns/unregistered"
        (directory / "dev_logs/sub").mkdir(parents=True)
        (directory / "dev_logs/sub/log.md").write_bytes(b"attached log")
        (Path(self.src["cycle_dir"]) / "artifacts/link").symlink_to("/nonexistent/external")
        out = self.move(attach_logs=[str(directory) + "=" + self.src["cycle_id"]])
        dest = self.target / out["attachments"][0]["target"]
        self.assertEqual((dest / "sub/log.md").read_bytes(), b"attached log")
        record = P.read_cycle_record(self.target, self.src["cycle_id"])
        cycle = P.cycle_dir(self.target, record["campaign_id"], self.src["cycle_id"], record)
        self.assertEqual(os.readlink(cycle / "artifacts/link"), "/nonexistent/external")
        rel = str((directory / "dev_logs/sub/log.md").relative_to(self.source))
        self.assertEqual(Reader.resolve_path(self.source, rel)["absolute"], str(dest / "sub/log.md"))
        self.assertEqual(self.move(attach_logs=[str(directory) + "=" + self.src["cycle_id"]])["operation_id"], out["operation_id"])

    def test_changed_source_and_duplicate_identity_do_not_overwrite(self):
        def change(point):
            if point == "stage":
                (Path(self.src["cycle_dir"]) / "artifacts/new.md").write_text("new writer")
        with mock.patch.object(X, "_fault", side_effect=change), self.assertRaises(P.ProducerError):
            self.move()
        self.assertTrue(Path(self.src["cycle_dir"]).exists())
        # The durable stage copy remains; no source removal or silent overwrite.
        self.assertFalse(any((self.target / X.JOURNALS).rglob("new.md")))
        other = self.make(self.target, "duplicate")
        target_record = P.cycle_record_path(self.target, other["cycle_id"])
        P._write_cycle_record(self.source, dict(P.read_cycle_record(self.target, other["cycle_id"]), slug="foreign"), exclusive=True)
        before = target_record.read_bytes()
        with self.assertRaises(P.ProducerError):
            P.cycle_move(self.source, other["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])
        self.assertEqual(target_record.read_bytes(), before)

    def test_satisfied_reopened_stream_is_preserved_then_superseded(self):
        path = Path(self.src["cycle_dir"]).parent / "campaign.json"
        C.close(self.source, path)
        C.reopen(self.source, path, reason="another cycle")
        prior = {p: p.read_bytes() for p in (path.parent / C.EVENTS_DIR).glob("*.json")}
        self.move()
        self.assertEqual(C.campaign_state(self.source, path).state, "superseded")
        for p, raw in prior.items():
            self.assertEqual(p.read_bytes(), raw)
        with self.assertRaises(C.CampaignError):
            C.reopen(self.source, path, reason="do not revive")

    def test_open_checkpoint_reservations_are_copied_with_history(self):
        marker = Path(os.environ["AGENT_HOME"]) / P.INTERIM_SUPPORT_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("fixture interim support")
        cycle = self.make(self.source, "checkpointed")
        checkpoint = P.checkpoint(self.source, cycle_id=cycle["cycle_id"], trigger="explicit")
        self.assertEqual(checkpoint["status"], "emitted", checkpoint)
        reservation = json.loads(P.reservation_path(self.source, cycle["cycle_id"]).read_bytes())
        out = P.cycle_move(self.source, cycle["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])
        copied = json.loads(P.reservation_path(self.target, cycle["cycle_id"]).read_bytes())
        self.assertEqual(copied["cycle_id"], reservation["cycle_id"])
        self.assertEqual(copied.get("artifacts"), reservation.get("artifacts"))
        self.assertTrue(P.open_manifest_path(self.target, cycle["cycle_id"]).is_file())
        self.assertTrue(any("checkpoints/" in path for path in out["support_inventory"]))
        self.assertTrue(any("open-manifests/" in path for path in out["support_inventory"]))

    def test_local_dry_run_cli_keeps_control_and_payload_unchanged(self):
        before = snapshot(self.source)
        proc = subprocess.run([sys.executable, str(Path(P.__file__)), "cycle-move", "--artifact-root", str(self.source),
            "--target-artifact-root", str(self.source / "."), "--cycle", self.src["cycle_id"], "--no-parent", "--dry-run"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["dry_run"])
        self.assertEqual(snapshot(self.source), before)

    def test_attachment_basename_and_existing_log_collisions(self):
        existing = Path(self.src["cycle_dir"]) / "artifacts/dev_logs/relocated/logs"
        existing.mkdir(parents=True)
        (existing / "kept.md").write_bytes(b"keep")
        attachments = []
        for parent in ["one", "two"]:
            directory = self.source / "campaigns" / parent / "logs"
            (directory / "dev_logs").mkdir(parents=True)
            (directory / "dev_logs/attached.md").write_text(parent)
            attachments.append(str(directory) + "=" + self.src["cycle_id"])
        out = self.move(attach_logs=attachments)
        self.assertEqual([Path(row["target"]).name for row in out["attachments"]], ["logs-2", "logs-3"])
        self.assertEqual(self.move(attach_logs=attachments)["attachments"], out["attachments"])

    def test_external_parent_and_root_qualified_manifest_routes(self):
        parent = self.src
        child = self.make(self.source, "external-parent-child", closed=True, parent=parent["cycle_id"])
        before = json.loads((Path(child["cycle_dir"]) / "manifest.json").read_bytes())
        out = P.cycle_move(self.source, child["cycle_id"], target_artifact_root=self.target, campaign=self.dst["campaign_id"])
        record = P.read_cycle_record(self.target, child["cycle_id"])
        after = json.loads((self.target / out["cycles"][0]["target"] / "manifest.json").read_bytes())
        self.assertEqual(after["routes"], before["routes"])
        self.assertEqual(after["artifact_root_id"], "root_" + "e" * 32)
        self.assertEqual(record["relocation"]["external_parent_root"], str(self.source))
        self.assertIsNone(P.read_cycle_record(self.target, parent["cycle_id"]))
        self.assertTrue(Path(parent["cycle_dir"]).exists())
        historical = X.historical_route_ids(self.source)
        self.assertIn(Path(child["route_file"]).stem, historical)
        self.assertNotIn(Path(parent["route_file"]).stem, historical)

    def test_stream_publish_response_loss_replays_one_terminal_event(self):
        original = C._publish_event
        def lost(*args):
            original(*args)
            raise RuntimeError("response lost")
        with mock.patch.object(C, "_publish_event", side_effect=lost), self.assertRaises(RuntimeError):
            self.move()
        result = self.move()
        self.assertEqual(result["status"], "moved")
        path = Path(self.src["cycle_dir"]).parent / "campaign.json"
        events = C.campaign_state(self.source, path).events
        self.assertEqual(sum(event["event_type"] == "campaign.superseded" for _, _, event in events), 1)
        pending = P.campaign_runtime_record(self.source, self.src["campaign_id"]) or {}
        self.assertFalse(pending.get("history_pending"))

    def test_real_cross_filesystem_copy(self):
        if not Path("/dev/shm").is_dir() or os.stat("/dev/shm").st_dev == os.stat(self.source).st_dev:
            self.skipTest("second writable filesystem unavailable")
        with tempfile.TemporaryDirectory(prefix="hearting-cx-", dir="/dev/shm") as directory:
            target = Path(directory)
            P.activate(target, repository_id="repo_" + "d" * 32, artifact_root_id="root_" + "e" * 32,
                       w7={"campaign_id": "camp_" + "c" * 32})
            destination = self.make(target, "cross-filesystem")
            out = P.cycle_move(self.source, self.src["cycle_id"], target_artifact_root=target, campaign=destination["campaign_id"])
            self.assertEqual((target / out["cycles"][0]["target"] / "artifacts/plans/cycle/plan.md").read_bytes(), b"plan body\n")

    def test_target_writer_appearing_after_admission_keeps_source(self):
        original = X._decision
        count = 0
        def observe(*args):
            nonlocal count
            count += 1
            if count >= 3:
                raise P.ProducerError("relocation-live-work", "new resource-run")
            return original(*args)
        with mock.patch.object(X, "_decision", side_effect=observe), self.assertRaises(P.ProducerError):
            self.move()
        self.assertTrue(Path(self.src["cycle_dir"]).exists())



if __name__ == "__main__":
    unittest.main()
