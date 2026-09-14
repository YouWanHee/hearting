#!/usr/bin/env python3
"""Real producer/CLI closure, native role proof, concurrency and crash boundaries."""
import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import artifact_campaign as C
import artifact_producer as P

spec = importlib.util.spec_from_file_location("campaign_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

SID = "11111111-2222-3333-4444-555555555555"


class CampaignTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.result = self.begin(campaign_key="campaign-closure")
        self.output = self.write_output(self.result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.result["cycle_id"])
        self.campaign = self.result["campaign_id"]
        self.path = Path(self.result["cycle_dir"]).parent / "campaign.json"
        self.manifest = Path(self.result["cycle_dir"]) / "manifest.json"
        self.original = self.manifest.read_bytes()
        self.home = Path(self._tmp.name) / "codex-home"
        self.native = self.home / "sessions/2026/09/13" / f"rollout-2026-09-13T00-00-00-{SID}.jsonl"
        self.native.parent.mkdir(parents=True)
        self.patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.patch.start(); self.addCleanup(self.patch.stop)

    def child(self, by_key=False):
        route = F.compile_for("direct", self.root, slug="later-cycle", gate_source="later-input")
        binding = F.L.admit_runtime_route(self.root, route)
        return P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                       intensity="direct", **({"campaign_key": "campaign-closure"} if by_key else {"campaign_id": self.campaign}))

    def approve(self, statement=None, role="user"):
        statement = statement or C.status(self.root, self.path)["approval_statement"]
        rows = [{"type": "session_meta", "payload": {"id": SID}},
                {"type": "response_item", "payload": {"type": "message", "role": role,
                  "content": [{"type": "input_text", "text": statement}]}}]
        self.native.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return statement

    def finish(self):
        return C.close(self.root, self.path, harness="codex", session=SID)

    def test_cli_and_idempotency_preserve_sealed_bytes_and_reject_new_cycle(self):
        self.approve()
        args = ["campaign-close", "--artifact-root", str(self.root), "--campaign", str(self.path),
                "--approval-harness", "codex", "--approval-session", SID]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(P.main(args), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "satisfied")
        event = C._event_path(self.path).read_bytes()
        self.assertEqual(self.finish()["event_id"], result["event_id"])
        self.assertEqual(C._event_path(self.path).read_bytes(), event)
        self.assertEqual(self.manifest.read_bytes(), self.original)
        with self.assertRaisesRegex(P.ProducerError, "campaign-not-active"):
            self.child()

    def test_no_native_user_approval_is_read_only(self):
        self.approve(role="assistant")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
            self.finish()
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(C._event_path(self.path).exists())

    def test_closed_key_does_not_silently_create_another_campaign(self):
        self.approve(); self.finish()
        before = list((self.root / "campaigns").iterdir())
        with self.assertRaisesRegex(P.ProducerError, "campaign-not-active"):
            self.child(by_key=True)
        self.assertEqual(before, list((self.root / "campaigns").iterdir()))

    def test_legacy_formatted_seal_and_canonical_index_are_verified_separately(self):
        document = json.loads(self.original)
        raw = json.dumps(document, indent=4, ensure_ascii=False).encode()
        self.manifest.write_bytes(raw)
        record = P.read_cycle_record(self.root, self.result["cycle_id"])
        record["manifest_digest"] = "sha256:" + C.hashlib.sha256(raw).hexdigest()
        P._write_cycle_record(self.root, record, exclusive=False)
        report = C.status(self.root, self.path)
        self.assertNotEqual(report["cycles"][0]["manifest_digest"], report["cycles"][0]["index_digest"])
        self.approve(); self.finish()
        self.assertEqual(self.manifest.read_bytes(), raw)

    def test_index_disagreement_does_not_become_success(self):
        original = C.admission.load_index
        def altered(root):
            index = original(root)
            index.cycles[self.result["cycle_id"]]["manifest_digest"] = "sha256:" + "0" * 64
            return index
        with mock.patch.object(C.admission, "load_index", side_effect=altered):
            with self.assertRaisesRegex(C.CampaignError, "campaign-index-mismatch"):
                C.status(self.root, self.path)

    def test_campaign_runlog_is_digest_bound_metadata_not_a_cycle(self):
        source_cycle = "cyc_" + "f" * 32
        body = b"# aggregate run log\n"
        runlog_path = self.path.parent / "RUNLOG.md"
        runlog_path.write_bytes(body)
        value = json.loads(self.path.read_text())
        value["runlog"] = {
            "contract": "campaign-runlog/v1", "path": "RUNLOG.md",
            "sha256": "sha256:" + C.hashlib.sha256(body).hexdigest(),
            "source_cycle_id": source_cycle, "source_locator": "experiments/_RUNLOG.md",
        }
        self.path.write_text(json.dumps(value))
        report = C.status(self.root, self.path)
        self.assertEqual([row["cycle_id"] for row in report["cycles"]], [self.result["cycle_id"]])
        runlog_path.write_bytes(b"drift\n")
        with self.assertRaisesRegex(C.CampaignError, "campaign-runlog-digest-mismatch"):
            C.status(self.root, self.path)

    def test_legacy_cycle_layout_closes_without_moving_or_rewriting_sealed_bytes(self):
        cid = self.result["cycle_id"]; old = self.manifest.parent
        target = old.parent / "cycles" / cid; target.parent.mkdir()
        old.rename(target); (target / ".cycle.json").unlink()
        record = P.read_cycle_record(self.root, cid); record.pop("locator")
        P._write_cycle_record(self.root, record, exclusive=False)
        index = C.admission.load_index(self.root)
        index.cycles[cid]["cycle_path"] = str(target.relative_to(self.root))
        C.admission._write_index(self.root, index)
        C.locator.rebuild_indexes(self.root)
        self.approve(); self.finish()
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "manifest.json").read_bytes(), self.original)

    def test_changed_goal_invalidates_old_acceptance(self):
        self.approve()
        value = json.loads(self.path.read_text()); value["goal"] += " and another goal"
        self.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
            self.finish()

    def test_native_later_rejection_wins(self):
        statement = self.approve()
        with self.native.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": statement.replace("campaign-satisfy", "campaign-reject")}]}}) + "\n")
        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
            self.finish()

    def test_artifact_drift_open_cycle_and_unlisted_member_are_blocked(self):
        self.approve()
        self.output.write_bytes(b"bad bytes")
        with self.assertRaisesRegex(C.CampaignError, "campaign-artifact-mismatch"):
            self.finish()
        self.output.write_bytes(b"plan body\n")
        child = self.child()
        with self.assertRaisesRegex(C.CampaignError, "campaign-cycle-not-sealed"):
            C.status(self.root, self.path)
        value = json.loads(self.path.read_text()); value["cycles"].remove(child["cycle_id"])
        self.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(C.CampaignError, "campaign-membership-drift"):
            C.status(self.root, self.path)

    def test_abandoned_is_sealed_not_success_and_residual_is_not_a_gate(self):
        residual = self.path.parent / "retained-notes" / "unclassified.txt"
        residual.parent.mkdir(); residual.write_text("not a declared cycle")
        child = self.child()
        self.write_output(child, data=b"Residual 843; abandoned work, no success claimed\n")
        P.finalize(self.root, cycle_id=child["cycle_id"], state="abandoned", abandon_reason="route-unrecoverable", allow_open_route=True)
        report = C.status(self.root, self.path)
        self.assertEqual(sorted(row["state"] for row in report["cycles"]), ["abandoned", "completed"])
        self.approve(); self.finish()
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["cycle_state"], "abandoned")
        self.assertEqual(residual.read_text(), "not a declared cycle")

    def test_crash_after_event_commit_blocks_begin_and_recovery_needs_no_new_approval(self):
        self.approve()
        with mock.patch.object(C, "_materialize", side_effect=OSError("crash")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-committed-recovery-required"):
                self.finish()
        self.assertEqual(json.loads(self.path.read_text())["state"], "active")
        self.assertTrue(C.status(self.root, self.path)["projection_pending"])
        self.assertEqual(P.read_campaign(self.root, self.campaign)["state"], "satisfied")
        _, view = C.locator.scan_index(self.root)
        self.assertEqual(view[self.campaign]["status"], "satisfied")
        with self.assertRaisesRegex(P.ProducerError, "campaign-not-active"):
            self.child()
        self.native.unlink()  # committed user acceptance survives transcript retention
        self.assertFalse(C.close(self.root, self.path, recover=True)["projection_pending"])
        self.assertEqual(self.manifest.read_bytes(), self.original)

    def test_conflicting_projection_survives_recovery(self):
        self.approve(); self.finish()
        changed = json.loads(self.path.read_text()); changed["title"] = "foreign successor"
        raw = json.dumps(changed).encode(); self.path.write_bytes(raw)
        with self.assertRaisesRegex(C.CampaignError, "campaign-projection-conflict"):
            C.close(self.root, self.path, recover=True)
        self.assertEqual(raw, self.path.read_bytes())

    def test_invalid_event_is_refused_before_commit(self):
        value = json.loads(self.path.read_text()); value["goal"] = "goal" * 20000
        self.path.write_text(json.dumps(value)); self.approve()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(C.CampaignError, "campaign-event-invalid"):
            self.finish()
        self.assertFalse(C._event_path(self.path).exists())
        self.assertEqual(before, self.path.read_bytes())

    def test_committed_event_corruption_and_native_malformed_input_are_typed(self):
        self.native.write_text("[]\n")
        with self.assertRaisesRegex(C.CampaignError, "approval-native-record-invalid"):
            self.finish()
        self.approve(); self.finish()
        path = C._event_path(self.path); event = json.loads(path.read_text())
        event["recorded_at"] = "2026-09-14T00:00:00Z"
        raw = C.canonical(event) + b"\n"; path.write_bytes(raw)
        with self.assertRaisesRegex(C.CampaignError, "campaign-event-invalid"):
            C.close(self.root, self.path, recover=True)
        self.assertEqual(raw, path.read_bytes())

    def test_crash_before_publish_and_concurrent_retry(self):
        self.approve()
        with mock.patch.object(C.os, "link", side_effect=OSError("precommit")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-not-committed"):
                self.finish()
        self.assertEqual(P.read_campaign(self.root, self.campaign)["state"], "active")
        self.assertFalse(C._event_path(self.path).exists())
        args = ["python3", str(Path(P.__file__)), "campaign-close", "--artifact-root", str(self.root),
                "--campaign", str(self.path), "--approval-harness", "codex", "--approval-session", SID]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: subprocess.run(args, text=True, capture_output=True), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(C._event_path(self.path).parent.glob("campaign.satisfied*.json"))), 1)

    def test_symlink_payload_and_symlink_event_are_not_followed(self):
        data = self.output.read_bytes(); self.output.unlink()
        foreign = Path(self._tmp.name) / "foreign"; foreign.write_bytes(data)
        self.output.symlink_to(foreign)
        with self.assertRaisesRegex(C.CampaignError, "campaign-symlink"):
            C.status(self.root, self.path)
        self.output.unlink(); self.output.write_bytes(data)
        event = C._event_path(self.path); event.symlink_to(foreign)
        with self.assertRaisesRegex(C.CampaignError, "campaign-symlink"):
            C.close(self.root, self.path, recover=True)
        self.assertEqual(foreign.read_bytes(), data)

    def test_readiness_drift_while_waiting_for_lock_preserves_state(self):
        self.approve()
        original = C.admission._acquire_lock
        def acquire(*args, **kw):
            fd = original(*args, **kw)
            value = json.loads(self.path.read_text()); value["title"] += " changed"
            self.path.write_text(json.dumps(value))
            return fd
        with mock.patch.object(C.admission, "_acquire_lock", side_effect=acquire):
            with self.assertRaisesRegex(C.CampaignError, "campaign-approval-snapshot-changed"):
                self.finish()
        self.assertFalse(C._event_path(self.path).exists())

    def test_claude_native_user_and_meta_tool_rejections(self):
        statement = C.status(self.root, self.path)["approval_statement"]
        base = Path(self._tmp.name) / "claude"
        path = base / "projects/project" / (SID + ".jsonl"); path.parent.mkdir(parents=True)
        row = {"type": "user", "sessionId": SID, "uuid": "message", "message": {"role": "user", "content": statement}}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(base)}):
            path.write_text(json.dumps({**row, "isMeta": True}) + "\n")
            with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
                C.verify_approval("claude", SID, statement)
            path.write_text(json.dumps(row) + "\n")
            self.assertEqual(C.close(self.root, self.path, harness="claude", session=SID)["status"], "satisfied")

    def test_opencode_native_export_and_synthetic_rejection(self):
        statement = C.status(self.root, self.path)["approval_statement"]; sid = "ses_fixture"
        row = {"info": {"id": "msg_user", "sessionID": sid, "role": "user"},
               "parts": [{"type": "text", "text": statement}]}
        def export(command, stdout, **kwargs):
            self.assertEqual(command, ["opencode", "export", sid])
            stdout.write(json.dumps({"info": {"id": sid}, "messages": [row]}).encode())
            stdout.flush()
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.object(C.subprocess, "run", side_effect=export):
            row["parts"][0]["synthetic"] = True
            with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
                C.verify_approval("opencode", sid, statement)
            del row["parts"][0]["synthetic"]
            self.assertEqual(C.close(self.root, self.path, harness="opencode", session=sid)["status"], "satisfied")


if __name__ == "__main__":
    unittest.main()
