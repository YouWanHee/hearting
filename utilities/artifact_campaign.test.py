#!/usr/bin/env python3
"""Real producer/CLI closure, native role proof, concurrency and crash boundaries."""
import concurrent.futures
import contextlib
import importlib.util
import io
import hashlib
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
        ledger = Path(self._tmp.name) / "peer-ledger"
        self.ledger_patch = mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(ledger)})
        self.ledger_patch.start(); self.addCleanup(self.ledger_patch.stop)

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

    def provisional_child(self, closed):
        """Seal a second campaign member `state: active` (route open at seal
        time, D-6). `closed`: "proven" closes the route with terminal markers
        written first; "unproven" closes it with none; `None` leaves it open."""
        route = F.compile_for("direct", self.root, slug="provisional-cycle", gate_source="provisional-input")
        binding = F.L.admit_runtime_route(self.root, route)
        route_file = Path(binding.route_file)
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_id=self.campaign)
        self.write_output(child)
        P.finalize(self.root, cycle_id=child["cycle_id"], allow_open_route=True)
        if closed == "proven":
            self.close(route, route_file)
        elif closed == "unproven":
            F.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        return route, route_file, child

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

    def test_abandoned_empty_cycle_record_is_detached_not_membership_drift(self):
        """TF-Rehancer 2026-09-15: three abandoned output-less cycles left records
        with the campaign id but no directory and no `cycles[]` entry, and
        `campaign-status` was blocked forever with `campaign-membership-drift`."""
        empty_route = F.compile_for("direct", self.root, slug="abandoned-attempt", gate_source="abandoned-input")
        empty_binding = F.L.admit_runtime_route(self.root, empty_route)
        empty = P.begin(self.root, route_file=Path(empty_binding.route_file), capability="autopilot-code",
                        intensity="direct", campaign_key="campaign-closure")
        self.assertEqual(empty["campaign_id"], self.campaign)
        outcome = P.finalize(self.root, cycle_id=empty["cycle_id"], state="abandoned",
                             abandon_reason="operator-decision")
        self.assertEqual(outcome["status"], "no-lineage")
        record = P.read_cycle_record(self.root, empty["cycle_id"])
        self.assertEqual((record["state"], record["campaign_id"]), ("abandoned", self.campaign))
        self.assertNotIn(empty["cycle_id"], json.loads(self.path.read_text())["cycles"])
        self.assertFalse(C.is_member_record(record))
        report = C.status(self.root, self.path)
        self.assertEqual(report["status"], "awaiting-user-acceptance")
        self.assertEqual([row["cycle_id"] for row in report["cycles"]], [self.result["cycle_id"]])
        self.assertEqual([(row["cycle_id"], row["state"], row["abandon_reason"]) for row in report["detached_cycles"]],
                         [(empty["cycle_id"], "abandoned", "operator-decision")])
        # The detached record is reported, never folded into the approval digest.
        self.assertNotIn("detached", json.dumps(C._snapshot(self.root, self.path)))
        # A detached id that is still listed is a real drift and stays refused.
        listed = json.loads(self.path.read_text())
        listed["cycles"].append(empty["cycle_id"])
        P._write_campaign(self.root, listed, exclusive=False)
        with self.assertRaisesRegex(C.CampaignError, "campaign-membership-drift"):
            C.status(self.root, self.path)
        listed["cycles"].pop()
        P._write_campaign(self.root, listed, exclusive=False)
        # Closure proceeds with the detached record left in place.
        self.approve()
        self.assertEqual(self.finish()["status"], "satisfied")
        self.assertEqual(P.read_cycle_record(self.root, empty["cycle_id"]), record)

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

    def _claude_store(self, rows):
        base = Path(self._tmp.name) / ("claude-consent-%d" % id(rows))
        path = base / "projects/project" / (SID + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return base

    @staticmethod
    def _ts(second):
        return "2026-09-18T03:05:%02d.000Z" % second

    def _typed(self, text, second, **extra):
        # The shape Claude Code 2.1.27x writes for a prompt the human submitted.
        return {"type": "user", "sessionId": SID, "timestamp": self._ts(second), "origin": {"kind": "human"},
                "promptSource": "typed", "message": {"role": "user", "content": text}, **extra}

    def _shown(self, text, second):
        return {"type": "assistant", "sessionId": SID, "timestamp": self._ts(second),
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    def _tool_round(self, second):
        return [{"type": "assistant", "sessionId": SID, "timestamp": self._ts(second),
                 "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
                {"type": "user", "sessionId": SID, "timestamp": self._ts(second + 1),
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}]

    def _enqueue(self, text, second):
        return {"type": "queue-operation", "operation": "enqueue", "sessionId": SID,
                "timestamp": self._ts(second), "content": text}

    def _queued_command(self, text, typed_second, written_second):
        return {"type": "attachment", "sessionId": SID, "timestamp": self._ts(written_second),
                "attachment": {"type": "queued_command", "prompt": text, "origin": {"kind": "human"},
                               "commandMode": "prompt", "timestamp": self._ts(typed_second)}}

    def _verify(self, rows, ledger_rows=()):
        ledger = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-messages" / "2026-09" / "sender.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ledger_rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self._claude_store(rows))}):
            return C.verify_approval("claude", SID, self.statement)

    def _refused(self, rows, ledger_rows=()):
        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
            self._verify(rows, ledger_rows)

    def test_closing_consent_right_after_the_statement_was_shown_closes(self):
        # 2026-09-18 user instruction: "응 닫아" after the agent showed the
        # statement is the user's approval; no digest retyping (session 5ed5b30b
        # lines 112 -> 116 had exactly this shape and was refused before).
        self.statement = C.status(self.root, self.path)["approval_statement"]
        rows = [self._shown("승인 대상:\n" + self.statement + "\n닫을까요?", 1), self._typed("응 닫아", 30)]
        approval = self._verify(rows)
        self.assertEqual((approval["acceptance_mode"], approval["reply_text"], approval["presentation"]["line"]),
                         ("presented-consent", "응 닫아", 1))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self._claude_store(rows))}):
            self.assertEqual(C.close(self.root, self.path, harness="claude", session=SID)["status"], "satisfied")

    def test_consent_typed_while_the_agent_was_busy_counts_by_its_typing_time(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        rows = [self._shown(self.statement, 1), *self._tool_round(2),
                self._enqueue("응 닫아", 5), self._queued_command("응 닫아", 5, 9)]
        self.assertEqual(self._verify(rows)["acceptance_mode"], "presented-consent")
        # exact statement typed mid-turn (session 5ed5b30b line 154) is accepted too
        rows = [self._enqueue(self.statement, 5), self._queued_command(self.statement, 5, 9)]
        self.assertEqual(self._verify(rows)["acceptance_mode"], "exact-statement")

    def test_consent_that_was_not_an_answer_to_the_shown_statement_is_refused(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        shown = self._shown(self.statement + "\n닫을까요?", 10)
        cases = {
            "no statement shown": [self._typed("응 닫아", 30)],
            "typed before it was shown (queued, dequeued after)": [
                self._enqueue("응 닫아", 5), shown,
                {**self._typed("응 닫아", 12), "promptSource": "queued"}],
            "not the first input after it": [shown, self._typed("사이클 두 개는 뭐야", 20), self._typed("응 닫아", 30)],
            "agent moved on to another question": [
                shown, *self._tool_round(11), self._shown("그리고 v5 학습도 바로 시작할까요?", 15), self._typed("응 닫아", 30)],
            "statement buried in an unrelated question, bare yes": [
                self._shown("테스트도 돌릴까요? (참고: " + self.statement + ")", 10), self._typed("응", 30)],
            "question, not consent": [shown, self._typed("닫아야 하나", 30)],
            "refusal": [shown, self._typed("승인 안 해", 30)],
            "accepted, then withdrawn": [shown, self._typed("응 닫아", 30), self._typed("아 잠깐 취소", 40)],
            "withdrawn while still queued": [shown, self._typed("응 닫아", 30), self._enqueue("아니 닫지 마", 40)],
            "headless sdk prompt": [shown, {**self._typed("응 닫아", 30), "promptSource": "sdk"}],
            "prompt suggestion accepted": [shown, {**self._typed("응 닫아", 30), "promptSource": "suggestion_accepted"}],
            "headless exact statement": [{**self._typed(self.statement, 30), "promptSource": "sdk", "origin": None}],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self._refused(rows)

    def test_task_notifications_and_tool_results_do_not_break_the_answer(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        notice = {**self._typed("<task-notification>done</task-notification>", 20),
                  "origin": {"kind": "task-notification"}, "promptSource": "system"}
        rows = [self._shown(self.statement, 1), *self._tool_round(2), notice, self._typed("응 닫아", 30)]
        self.assertEqual(self._verify(rows)["acceptance_mode"], "presented-consent")

    def test_input_injected_by_another_session_never_approves(self):
        # Pane prompts are stored like typed human input; the peer trailer, a
        # ledger digest (any recipient, whitespace forms) or a recent ledger row
        # whose summary starts the text excludes them.
        self.statement = C.status(self.root, self.path)["approval_statement"]
        shown = self._shown(self.statement, 10)
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        self._refused([shown, self._typed("승인\n\n(peer-from: claude [4a] x ; ref=1)", 30)])
        self._refused([self._typed(self.statement + "\n\n(peer-from: claude [4a] x ; ref=1)", 30)])
        self._refused([shown, self._typed("응 닫아", 30)],
                      [{"to": {"harness": "claude", "name": "wE:p6"}, "body_sha256": digest("응 닫아 ")}])
        self._refused([shown, self._typed("응 닫아", 30)],
                      [{"to": {"harness": "claude", "session_id": SID}, "summary": "응 닫아",
                        "ts": "2026-09-18T03:05:31Z", "body_sha256": digest("different bytes")}])
        # an injected message between the statement and the user's own reply does not count as that reply
        injected = self._typed("[steer] 계속할까\n\n(peer-from: claude [4a] x ; ref=1)", 20)
        self.assertEqual(self._verify([shown, injected, self._typed("응 닫아", 30)])["acceptance_mode"],
                         "presented-consent")

    def test_unreadable_peer_ledger_turns_short_consent_off(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        rows = [self._shown(self.statement, 10), self._typed("응 닫아", 30)]
        with mock.patch.object(C._PeerLedger, "_records", side_effect=OSError("ledger gone")):
            self._refused(rows)
            self.assertEqual(self._verify([self._typed(self.statement, 30)])["acceptance_mode"], "exact-statement")

    def test_rejection_and_acceptance_order(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        rejection = self.statement.replace("campaign-satisfy", "campaign-reject", 1)
        shown = self._shown(self.statement, 10)
        self.assertEqual(self._verify([shown, self._typed("아니", 20), self._typed(self.statement, 30)])["acceptance_mode"],
                         "exact-statement")
        self.assertEqual(self._verify([self._typed(rejection, 5), shown, self._typed("응 닫아", 30)])["acceptance_mode"],
                         "presented-consent")
        self._refused([self._typed(self.statement, 5), shown, self._typed("안 닫아", 30)])

    def test_consent_vocabulary(self):
        for text in ("응 닫아", "닫아", "네 닫아주세요", "승인", "승인합니다", "ㅇㅇ 닫아", "닫아.", "close it", "approve"):
            self.assertEqual(C.consent_verdict(text), "accept", text)
        for text in ("아니", "아니요 닫아", "승인 안 해", "승인 못 해", "승인 불가", "종료하지 않아", "안 닫아",
                     "닫지 마", "ㄴㄴ", "싫어", "don\u2019t close", "never close", "no", "not yet", "취소", "잠깐만"):
            self.assertEqual(C.consent_verdict(text), "reject", text)
        for text in ("응", "ok", "yes", "좋아요", "y", "go back", "닫아야 하나", "닫으면 어떻게 돼", "승인해도 될까",
                     "확인 중", "진행 중이야", "어 그런데…", "ok, but first explain the cycles", "close the other one", ""):
            self.assertIsNone(C.consent_verdict(text), text)

    def test_round_two_review_cases(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        shown = self._shown(self.statement, 10)
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        # a human refusal the peer ledger happens to know still withdraws
        self._refused([shown, self._typed("응 닫아", 20), self._typed("아니", 30)],
                      [{"to": {"harness": "claude", "session_id": "other"}, "body_sha256": digest("아니")}])
        self._refused([shown, self._typed("응 닫아", 20), self._enqueue("취소", 30)],
                      [{"to": {"harness": "claude"}, "body_sha256": digest("취소")}])
        # any later input voids a short consent; long refusals and more refusal words count
        for later in ("거부", "반려", "아직이요", "노노", "nah", "rejected", "deny", "abort", "안됨",
                      "아니, 잠깐만요. 사이클 두 개가 완료 확인 없이 닫혔다는 게 마음에 걸려서 조금 더 보고 다시 판단할게요",
                      "그리고 v5 학습 결과도 알려줘"):
            with self.subTest(later):
                self._refused([shown, self._typed("응 닫아", 20), self._typed(later, 30)])
        # a human "승인" is not mistaken for an unrelated recent peer message
        approval = self._verify([shown, self._typed("승인", 30)],
                                [{"to": {"harness": "claude"}, "summary": "승인 대기 보고", "ts": "2026-09-18T03:05:31Z",
                                  "body_sha256": digest("승인 대기 보고")}])
        self.assertEqual(approval["acceptance_mode"], "presented-consent")
        # a queued input with no typing time is ordered by when it was written
        rejection = self.statement.replace("campaign-satisfy", "campaign-reject", 1)
        self._refused([{**self._typed(self.statement, 20), "promptSource": "queued"}, self._typed(rejection, 30)])
        # a delivered copy is paired with its enqueue even when their clocks differ by a millisecond
        rows = [shown, {**self._enqueue("응 닫아", 20), "timestamp": "2026-09-18T03:05:20.001Z"},
                {**self._queued_command("응 닫아", 20, 25),
                 "attachment": {**self._queued_command("응 닫아", 20, 25)["attachment"],
                                "timestamp": "2026-09-18T03:05:20.000Z"}}]
        self.assertEqual(self._verify(rows)["acceptance_mode"], "presented-consent")
        # notifications and slash commands are not the user answering
        rows = [shown, self._enqueue("<task-notification>done</task-notification>", 15),
                {**self._typed("<command-name>/model</command-name>", 16)}, self._typed("네! 닫아주세요", 30)]
        self.assertEqual(self._verify(rows)["reply_text"], "네! 닫아주세요")

    def test_programmatic_codex_and_opencode_child_sessions_are_not_a_user(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        base = Path(self._tmp.name) / "codex-originator"
        rollout = base / "sessions/2026/09/18" / ("rollout-y-" + SID + ".jsonl")
        rollout.parent.mkdir(parents=True)
        user = {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": self.statement}]}}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(base)}):
            for meta, accepted in (({"source": "vscode", "originator": "Claude Code"}, False),
                                   ({"source": "mcp", "originator": "codex_mcp"}, False),
                                   ({"source": "vscode", "originator": "codex-tui"}, True),
                                   ({"source": "vscode", "originator": "Codex Desktop"}, True)):
                rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID, **meta}}) + "\n"
                                   + json.dumps(user) + "\n")
                with self.subTest(meta):
                    if accepted:
                        self.assertEqual(C.verify_approval("codex", SID, self.statement)["acceptance_mode"], "exact-statement")
                    else:
                        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
                            C.verify_approval("codex", SID, self.statement)
        sid = "ses_child"
        row = {"info": {"id": "msg_user", "sessionID": sid, "role": "user"},
               "parts": [{"type": "text", "text": self.statement}]}
        def export(command, stdout, **kwargs):
            stdout.write(json.dumps({"info": {"id": sid, "parentID": "ses_parent"}, "messages": [row]}).encode())
            stdout.flush()
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.object(C.subprocess, "run", side_effect=export):
            with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
                C.verify_approval("opencode", sid, self.statement)

    def test_codex_exec_sessions_are_not_a_user(self):
        self.statement = C.status(self.root, self.path)["approval_statement"]
        base = Path(self._tmp.name) / "codex"
        rollout = base / "sessions/2026/09/18" / ("rollout-x-" + SID + ".jsonl")
        rollout.parent.mkdir(parents=True)
        user = {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": self.statement}]}}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(base)}):
            for source, accepted in (("exec", False), ("vscode", True), ("cli", True)):
                rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID, "source": source}}) + "\n"
                                   + json.dumps(user) + "\n")
                with self.subTest(source):
                    if accepted:
                        self.assertEqual(C.verify_approval("codex", SID, self.statement)["acceptance_mode"], "exact-statement")
                    else:
                        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
                            C.verify_approval("codex", SID, self.statement)

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

    def test_proven_campaign_rows_and_digest_are_unchanged(self):
        report = C.status(self.root, self.path)
        row = report["cycles"][0]
        self.assertEqual(set(row) - {"disposition"},
                         {"cycle_id", "state", "manifest_digest", "index_digest",
                          "route_id", "manifest_id", "manifest_revision_id"})
        self.assertEqual(row["disposition"], "completed")
        snapshot = C._snapshot(self.root, self.path)
        without_disposition = [{k: v for k, v in r.items() if k != "disposition"} for r in report["cycles"]]
        self.assertEqual(without_disposition, snapshot["cycles"])
        self.assertEqual(report["snapshot_digest"], C.digest(snapshot))
        self.assertNotIn("unproven_cycles", report)

    def test_route_closed_unproven_cycle_is_disclosed_and_closable(self):
        route, route_file, child = self.provisional_child("unproven")
        manifest_before = (Path(child["cycle_dir"]) / "manifest.json").read_bytes()
        record_before = P.read_cycle_record(self.root, child["cycle_id"])
        report = C.status(self.root, self.path)
        self.assertEqual(report["status"], "awaiting-user-acceptance")
        row = next(r for r in report["cycles"] if r["cycle_id"] == child["cycle_id"])
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["disposition"], C.PROVISIONAL_DISPOSITION)
        self.assertIs(row["terminal_gate_proven"], False)
        self.assertTrue(any(reason.endswith(":completion-marker-absent") for reason in row["terminal_gate_reasons"]))
        self.assertTrue(row["route_outcome_digest"].startswith("sha256:"))
        self.assertEqual(report["unproven_cycles"], {"count": 1, "without_terminal_proof": 1})
        self.approve()
        result = self.finish()
        self.assertEqual(result["status"], "satisfied")
        event = json.loads(C._event_path(self.path).read_text())
        snap_row = next(r for r in event["payload"]["snapshot"]["cycles"] if r["cycle_id"] == child["cycle_id"])
        for key in ("route_closed", "terminal_gate_proven", "terminal_gate_reasons", "route_outcome_digest"):
            self.assertIn(key, snap_row)
        self.assertNotIn("disposition", snap_row)
        self.assertIsNotNone(C._load_event(self.root, self.path))
        self.assertEqual((Path(child["cycle_dir"]) / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"]), record_before)

    def test_outcome_bytes_change_invalidates_prior_statement(self):
        route, route_file, child = self.provisional_child("unproven")
        self.approve()
        outcome_path = C.lifecycle.canonical_outcome_path(self.root, route["route_id"])
        outcome = json.loads(outcome_path.read_text())
        outcome["summary"] = "changed after approval"
        outcome_path.write_text(json.dumps(outcome))
        with self.assertRaisesRegex(C.CampaignError, "campaign-user-acceptance-required"):
            self.finish()

    def test_route_closed_with_proof_stays_active_and_sealed_unproven(self):
        route, route_file, child = self.provisional_child("proven")
        report = C.status(self.root, self.path)
        row = next(r for r in report["cycles"] if r["cycle_id"] == child["cycle_id"])
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["disposition"], C.PROVISIONAL_DISPOSITION)
        self.assertIs(row["terminal_gate_proven"], True)
        self.assertEqual(row["terminal_gate_reasons"], [])
        self.assertEqual(report["unproven_cycles"]["without_terminal_proof"], 0)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=child["cycle_id"], state="completed", allow_open_route=True)
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_open_route_provisional_cycle_is_refused_with_next_step(self):
        route, route_file, child = self.provisional_child(None)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-cycle-provisional-active")
        self.assertIn("open=1", caught.exception.detail)
        self.assertIn(f"route={route['route_id']}", caught.exception.detail)
        self.assertIn("route_state=open", caught.exception.detail)
        self.assertIn("complete", caught.exception.detail)
        self.assertIn("--allow-unproven", caught.exception.detail)
        self.assertNotIn("finalize", caught.exception.detail)
        F.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        report = C.status(self.root, self.path)
        self.assertEqual(report["status"], "awaiting-user-acceptance")

    def test_outcome_identity_mismatch_is_typed(self):
        route, route_file, child = self.provisional_child("unproven")
        outcome_path = C.lifecycle.canonical_outcome_path(self.root, route["route_id"])
        outcome = json.loads(outcome_path.read_text())
        outcome["route_hash"] = "sha256:" + "0" * 64
        outcome_path.write_text(json.dumps(outcome))
        with self.assertRaisesRegex(C.CampaignError, "campaign-cycle-route-outcome-mismatch"):
            C.status(self.root, self.path)

    def test_integrity_errors_win_over_open_route_refusal(self):
        self.provisional_child(None)
        self.output.write_bytes(b"bad bytes")
        with self.assertRaisesRegex(C.CampaignError, "campaign-artifact-mismatch"):
            C.status(self.root, self.path)


class OpenRefusalDetailTest(unittest.TestCase):
    def test_open_refusal_detail_is_bounded(self):
        pending = [(f"cyc_{i:032x}", f"rt-{i:016x}") for i in range(12)]
        detail = C._open_refusal_detail(pending)
        self.assertTrue(detail.startswith("open=12 "))
        self.assertIn("+2 more", detail)
        self.assertEqual(detail.count("route_state=open"), 10)
        self.assertNotIn("finalize", detail)


if __name__ == "__main__":
    unittest.main()
