#!/usr/bin/env python3
"""별칭은 표시 전용: 전송 본문·실제 수신자·로컬 원장 결속 회귀. 운영 전송 없음."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pm = module("alias_peer_message", "utilities/peer-message.py")


def pinned_source(path):
    # HOME is isolated, so use command-local trust for this exact test checkout.
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", "show", f"1c201125:{path}"],
                                   cwd=ROOT, text=True, timeout=5)


class AliasReceive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_DISPATCH_JOBS": str(self.state / "jobs.log"),
            "HOME": str(self.state / "home"), "CODEX_HOME": str(self.state / "home/.codex"),
            "AGENT_HOME": str(ROOT), "CLAUDE_CONFIG_DIR": str(self.state / "home/.claude"),
            "XDG_STATE_HOME": str(self.state / "xdg"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sender = {"harness": "codex", "session_id": "01a07b2b-9108-7110-bd41-93b7ade4d9c4", "name": "동명"}
        self.recipient = {"harness": "codex", "session_id": "recipient-a"}

    def prepare(self, body="검증할 실제 본문", sender=None, recipient=None):
        return pm.prepare_peer_message(body, sender or self.sender, recipient or self.recipient)

    def parse(self, text, recipient=None):
        return pm.parse_peer_trailer(text, recipient or self.recipient)["session_id"]

    def rows(self):
        return [json.loads(line) for p in (self.state / "peer-messages").glob("*/*.jsonl")
                for line in p.read_text().splitlines()]

    def test_exact_body_recipient_and_transfer_record(self):
        text, ref = self.prepare()
        self.assertNotIn(self.sender["session_id"], text)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse(text + "변조"))
        self.assertIsNone(self.parse(text.replace("실제", "다른")))
        for target in ({"harness": "claude", "session_id": "recipient-a"},
                       {"harness": "codex", "session_id": "recipient-b"},
                       {"harness": "codex", "session_id": ""}):
            self.assertIsNone(self.parse(text, target))
        record = json.loads(pm._transfer_path(ref).read_text())
        self.assertEqual(record["message_id"], ref)
        self.assertEqual(record["from"], self.sender)
        self.assertEqual(record["to"], self.recipient)
        self.assertNotIn("검증할", json.dumps(record, ensure_ascii=False))

    def test_collision_same_name_and_swapped_refs(self):
        # A real 8-bit collision, not a mock alias resolver.
        tag = pm.peer_alias("codex", self.sender["session_id"])
        other = next(f"other-{n}" for n in range(10000) if pm.peer_alias("codex", f"other-{n}") == tag)
        sender2 = dict(self.sender, session_id=other)
        one, r1 = self.prepare("첫 본문")
        two, r2 = self.prepare("둘째 본문", sender=sender2)
        self.assertEqual(self.parse(one), self.sender["session_id"])
        self.assertEqual(self.parse(two), other)
        self.assertNotEqual(r1, r2)
        self.assertIsNone(self.parse(one.replace(r1, r2)))
        self.assertIsNone(self.parse(two.replace(r2, r1)))
        self.assertIsNone(self.parse("복사한 본문\n" + one.splitlines()[-1]))

    def test_missing_corrupt_unknown_and_path_reference(self):
        for damage in ("missing", "json", "shape", "id", "digest"):
            text, ref = self.prepare()
            path = pm._transfer_path(ref)
            if damage == "missing":
                path.rename(path.with_suffix(".missing"))
            elif damage == "json":
                path.write_text("broken")
            elif damage == "shape":
                path.write_text("[]")
            else:
                rec = json.loads(path.read_text())
                rec["message_id" if damage == "id" else "body_sha256"] = "wrong"
                path.write_text(json.dumps(rec))
            self.assertIsNone(self.parse(text), damage)
        text, ref = self.prepare()
        with mock.patch.object(pm, "_ledger_root", side_effect=AssertionError("no path lookup")):
            self.assertIsNone(self.parse(text.replace(ref, "../../secret")))
        unknown, _ = self.prepare(sender={"harness": "unknown", "session_id": "known-id"})
        self.assertIn("[?]", unknown)
        self.assertIsNone(self.parse(unknown))

    def test_legacy_last_trailer_and_pinned_old_receiver(self):
        legacy = "기존 본문\n(peer-from: claude full-legacy-id old-name)"
        self.assertEqual(self.parse(legacy), "full-legacy-id")
        text, _ = self.prepare(legacy)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse("앞에 추가\n" + text))
        self.assertEqual(self.parse(text + "\n(peer-from: claude last-id last-name)"), "last-id")
        # Execute the actual pinned pre-change parser; no mock reinterpretation.
        source = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(source, "pinned-peer-message", "exec"), scope)
        old = scope["parse_peer_trailer"](text)
        self.assertIsNone(old["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])

    def test_claude_derived_then_rename_remembered_tag(self):
        from fleet import titles
        sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True)
        path = sessions / "123.json"
        path.write_text(json.dumps({"sessionId": "claude-id", "name": "project-ab", "nameSource": "derived"}))
        with mock.patch.object(titles, "read_tag", return_value="cd"):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[ab]")
            path.write_text(json.dumps({"sessionId": "claude-id", "name": "renamed-ef", "nameSource": "user"}))
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[cd]")
        with mock.patch.object(titles, "read_tag", return_value=None):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[?]")

    def test_pane_move_and_reuse_do_not_supply_identity(self):
        text, _ = self.prepare(recipient=dict(self.recipient, pane="w1:p1", name="동명"))
        self.assertEqual(self.parse(text, dict(self.recipient, pane="w2:p8")), self.sender["session_id"])
        self.assertIsNone(self.parse(text, dict(self.recipient, pane="w1:p1", session_id="new-occupant")))

    def test_transfer_ref_cannot_be_rebound_or_read_through_symlink(self):
        text, ref = self.prepare()
        path = pm._transfer_path(ref)
        original = path.read_bytes()
        with mock.patch.object(pm.secrets, "token_hex", return_value=ref):
            with self.assertRaises(FileExistsError):
                self.prepare("다른 전송")
        self.assertEqual(path.read_bytes(), original)
        renamed = path.with_suffix(".saved")
        path.rename(renamed)
        path.symlink_to(renamed)
        self.assertIsNone(self.parse(text))

    def _receive_actual(self, harness, text, sid="recipient-a"):
        if harness == "claude":
            hook = module("alias_claude_hook", "hooks/peer-message-record.py")
            hook.handle_prompt({"session_id": sid, "cwd": str(ROOT), "prompt": text})
        elif harness == "codex":
            hook = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
            hook.peer_notice({"session_id": sid}, text, str(ROOT))
        else:
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", harness, "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_distinct_installed_runtime_roots_all_sender_receiver_pairs(self):
        roots = {"claude": self.state / "xdg/hearting/dispatch",
                 "codex": self.state / "home/.codex/.harness/dispatch",
                 "opencode": self.state / "home/.config/opencode/.harness/dispatch"}
        for sender_harness, sender_root in roots.items():
            for receiver_harness, receiver_root in roots.items():
                with self.subTest(sender=sender_harness, receiver=receiver_harness):
                    sender = dict(self.sender, harness=sender_harness,
                                  session_id=f"sender-{sender_harness}-{receiver_harness}")
                    target = dict(self.recipient, harness=receiver_harness)
                    os.environ["AGENT_DISPATCH_JOBS"] = str(sender_root / "jobs.log")
                    text, ref = self.prepare(sender=sender, recipient=target)
                    os.environ["AGENT_DISPATCH_JOBS"] = str(receiver_root / "jobs.log")
                    self.assertEqual(self.parse(text, target), sender["session_id"])
                    self.assertIsNone(self.parse(text + "改", target))
                    self.assertIsNone(self.parse(text, dict(target, session_id="new-occupant")))
                    self._receive_actual(receiver_harness, text)
                    path = receiver_root / "peer-messages" / time.strftime("%Y-%m") / (sender["session_id"] + ".jsonl")
                    row = json.loads(path.read_text().splitlines()[-1])
                    self.assertEqual(row["from"]["session_id"], sender["session_id"])
                    self.assertEqual(row["to"]["session_id"], target["session_id"])
                    transfer = sender_root / "peer-messages/transfers" / (ref + ".json")
                    transfer.rename(transfer.with_suffix(".missing"))
                    self.assertIsNone(self.parse(text, target))

    def test_metadata_types_fail_soft_in_all_receivers(self):
        changes = [(side, field, value) for side in ("from", "to")
                   for field, values in (("session_id", (123, True, [], None, " padded ")),
                                         ("name", ([], {}))) for value in values]
        changes += [(side, None, value) for side in ("from", "to") for value in ([], None)]
        for harness in ("claude", "codex", "opencode"):
            for side, field, value in changes:
                with self.subTest(harness=harness, side=side, field=field, value=value):
                    text, ref = self.prepare(recipient=dict(self.recipient, harness=harness))
                    path = pm._transfer_path(ref)
                    record = json.loads(path.read_text())
                    if field:
                        record[side][field] = value
                    else:
                        record[side] = value
                    path.write_text(json.dumps(record))
                    self._receive_actual(harness, text)
        rows = self.rows()
        self.assertEqual(len(rows), len(changes) * 3)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in rows))

    def test_missing_claude_tag_does_not_remove_proven_identity(self):
        sender = dict(self.sender, harness="claude", session_id="claude-unremembered")
        text, ref = self.prepare(sender=sender)
        self.assertIn("[?]", text)
        self.assertEqual(self.parse(text), sender["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])
        pm._transfer_path(ref).write_text("null")
        self.assertIsNone(self.parse(text))

    def test_duplicate_ref_across_trusted_roots_is_ambiguous(self):
        text, ref = self.prepare()
        other_root = self.state / "home/.codex/.harness/dispatch"
        other = other_root / "peer-messages/transfers" / (ref + ".json")
        other.parent.mkdir(parents=True)
        other.write_bytes(pm._transfer_path(ref).read_bytes())
        self.assertIsNone(self.parse(text))

    def test_pinned_opencode_regex_and_writer_leave_new_sender_unattributed(self):
        source = pinned_source("adapters/opencode/plugins/hearting-guards.js")
        old_python = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(old_python, "pinned-peer-message", "exec"), scope)
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fn = input.source.slice(input.source.indexOf('const peerTrailerRe ='), input.source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice("recipient-a", prompt, root)', {
  path, root: input.root, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for harness in ("codex", "claude"):
            text, _ = self.prepare(sender=dict(self.sender, harness=harness),
                                   recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run(["node", "-e", js],
                                  input=json.dumps({"root": str(ROOT), "source": source, "prompt": text}),
                                  text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            with mock.patch.object(sys, "stdin", io.StringIO(call["body"])):
                self.assertEqual(scope["main"](call["args"][1:]), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in self.rows()))

    def test_python_receive_cli_exact_and_wrong_recipient(self):
        for sid in ("recipient-a", "new-occupant"):
            text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", "opencode", "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["to"]["session_id"]: r["from"]["session_id"] for r in rows},
                         {"recipient-a": self.sender["session_id"], "new-occupant": ""})

    def test_claude_and_codex_actual_receiver_hooks(self):
        claude = module("alias_claude_hook", "hooks/peer-message-record.py")
        codex = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
        for harness, hook in (("claude", claude), ("codex", codex)):
            for target in ("recipient-a", "new-occupant"):
                text, _ = self.prepare(recipient={"harness": harness, "session_id": "recipient-a"})
                payload = {"session_id": target, "cwd": str(ROOT), "prompt": text}
                if harness == "claude":
                    hook.handle_prompt(payload)
                else:
                    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "ambient-wrong"}):
                        hook.peer_notice(payload, text, str(ROOT))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")

    def test_opencode_plugin_forwards_actual_prompt_to_canonical_cli(self):
        # Run the unchanged plugin function in a VM with a recording transport;
        # then execute the captured Python CLI synchronously in the temp root.
        text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.root + '/adapters/opencode/plugins/hearting-guards.js', 'utf8');
const fn = source.slice(source.indexOf('function spawnPeerNotice('), source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice(sid, prompt, root)', {
  path, root: input.root, sid: input.sid, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for sid in ("recipient-a", "new-occupant"):
            proc = subprocess.run(["node", "-e", js], input=json.dumps({"root": str(ROOT), "sid": sid, "prompt": text}),
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            self.assertEqual(call["body"], text)
            self.assertIn("receive", call["args"])
            received = subprocess.run([call["exe"], *call["args"]], input=call["body"], text=True, capture_output=True, timeout=5)
            self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows():
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")


if __name__ == "__main__":
    unittest.main()
