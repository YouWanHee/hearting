#!/usr/bin/env python3

import unittest
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from recipient_lifetime import RecipientCensus, recipient_state
from claude_process_identity import claude_session_census, invocation_kind


class RecipientLifetimeTest(unittest.TestCase):
    def _fake_runtime(self, root, number, *, exe="/claude/versions/2.1.30x",
                      argv=("claude",), uid=None, home=None):
        proc = root / str(number)
        (proc / "ns").mkdir(parents=True)
        (proc / "stat").write_text(f"{number} (claude) S " + " ".join(["0"] * 18 + [str(number)]))
        (proc / "cmdline").write_bytes(b"\0".join(x.encode() for x in argv) + b"\0")
        os.symlink("/ns/pid-host", proc / "ns" / "pid")
        os.symlink(exe, proc / "exe")
        if home is None:
            home = root.parent / f"home-{number}" / ".claude"
        (home / "sessions").mkdir(parents=True)
        (proc / "environ").write_bytes(f"HOME={home.parent}\0".encode())
        return proc, home

    def _fake_proc_root(self, root):
        (root / "self" / "ns").mkdir(parents=True)
        os.symlink("/ns/pid-host", root / "self" / "ns" / "pid")

    def test_exact_positive_evidence_and_successor_precedence(self):
        self.assertEqual(recipient_state(
            harness="claude", session_id="sid", seat_state="ended",
            successor_state="alive"), "alive")
        self.assertEqual(recipient_state(
            harness="codex", session_id="sid", lifetime_state="ended"), "ended")
        self.assertEqual(recipient_state(
            harness="opencode", session_id="sid", lifetime_state="ended"), "ended")

    def test_seatless_codex_and_opencode_without_existing_lifetime_stay_unknown(self):
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(recipient_state(harness=harness, session_id="sid"), "unknown")

    def test_claude_census_is_pass_local_and_exceptions_are_unknown(self):
        calls = []
        census = RecipientCensus(lambda harness, sid: calls.append((harness, sid)) or "ended")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "ended")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "ended")
        self.assertEqual(calls, [("claude", "sid")])
        broken = RecipientCensus(lambda *_args: (_ for _ in ()).throw(OSError("unreadable")))
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=broken), "unknown")

    def test_unknown_probe_values_never_authorize_end(self):
        census = RecipientCensus(lambda *_args: "complete-but-unrecognized")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "unknown")
        self.assertEqual(recipient_state(harness="claude", session_id="sid",
                                         successor_state="conflict", census=census), "unknown")

    def test_fake_proc_exact_current_session_and_unresolved_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            home = Path(temp) / "home" / ".claude"
            process = root / "123"
            (process / "ns").mkdir(parents=True)
            (root / "self" / "ns").mkdir(parents=True)
            (home / "sessions").mkdir(parents=True)
            (home / ".statusline").mkdir(parents=True)
            os.symlink("/ns/pid-host", root / "self" / "ns" / "pid")
            os.symlink("/ns/pid-host", process / "ns" / "pid")
            os.symlink("/usr/bin/claude", process / "exe")
            (process / "stat").write_text("123 (claude) S " + " ".join(["0"] * 18 + ["42"]))
            (process / "cmdline").write_bytes(b"claude\0")
            (process / "environ").write_bytes(f"HOME={home.parent}\0".encode())
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "42", "sessionId": "current-sid"}))

            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"current-sid"})

            (home / "sessions" / "123.json").unlink()
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)
            (process / "cmdline").write_bytes(b"claude\0daemon\0")
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_resume_alias_is_positive_even_when_census_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            home = Path(temp) / "home" / ".claude"
            process = root / "123"
            (process / "ns").mkdir(parents=True)
            (root / "self" / "ns").mkdir(parents=True)
            (home / "sessions").mkdir(parents=True)
            (home / ".statusline").mkdir(parents=True)
            os.symlink("/ns/pid-host", root / "self" / "ns" / "pid")
            os.symlink("/ns/pid-host", process / "ns" / "pid")
            os.symlink("/usr/bin/claude", process / "exe")
            (process / "stat").write_text("123 (claude) S " + " ".join(["0"] * 18 + ["42"]))
            (process / "cmdline").write_bytes(b"claude\0--resume\0wanted-sid\0")
            (process / "environ").write_bytes(f"HOME={home.parent}\0".encode())

            complete, holders, _reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("wanted-sid", holders)

    def test_native_invocation_classifier_separates_commands_from_conversations(self):
        for argv, expected in (
            (["/usr/bin/claude", "daemon"], "command"),
            (["claude", "mcp", "serve"], "command"),
            (["claude", "--resume", "sid"], "session"),
            (["claude", "-p", "prompt"], "session"),
            (["claude", "--unknown-option"], "unknown"),
            (["node", "/opt/@anthropic-ai/claude-code/cli.js", "daemon"], "command"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(invocation_kind(argv), expected)

    def test_fake_proc_native_version_executable_with_claude_argv_is_a_holder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123, argv=("claude", "--resume", "resume-sid"))
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "native-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid", "resume-sid"})

    def test_fake_proc_native_ugrep_helper_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("ugrep", "--files"))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_ignores_unreadable_non_claude_executables_by_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            foreign, _home = self._fake_runtime(root, 123, exe="/usr/sbin/sshd", argv=("sshd",))
            same_uid, _home = self._fake_runtime(root, 124, exe="/usr/sbin/sshd", argv=("sshd",))
            real_stat, real_readlink = Path.stat, os.readlink

            def stat(path, *args, **kwargs):
                value = real_stat(path, *args, **kwargs)
                if path == foreign:
                    return SimpleNamespace(st_uid=os.getuid() + 1)
                return value

            def readlink(path, *args, **kwargs):
                if path in {foreign / "exe", same_uid / "exe"}:
                    raise PermissionError("fixture unreadable exe")
                return real_readlink(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", stat), mock.patch.object(os, "readlink", readlink):
                complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_other_uid_claude_shaped_cmdline_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, _home = self._fake_runtime(root, 123, exe="/usr/bin/node",
                                              argv=("node", "/opt/claude-code/cli.js"))
            real_stat = Path.stat

            def stat(path, *args, **kwargs):
                value = real_stat(path, *args, **kwargs)
                return SimpleNamespace(st_uid=os.getuid() + 1) if path == proc else value

            with mock.patch.object(Path, "stat", stat):
                complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("foreign-claude-shaped-process", reasons)

    def test_fake_proc_unknown_argv_on_native_executable_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("mystery-launcher", "--x"))
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("native-process-argv-unresolved", reasons)

    def test_fake_proc_missing_statusline_is_complete_and_multiple_claims_are_holders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "native-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid"})

            (home / ".statusline").mkdir()
            for sid in ("tap-one", "tap-two"):
                (home / ".statusline" / f"{sid}.json").write_text(json.dumps({
                    "pid": 123, "proc_start": "123", "session_id": sid}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid", "tap-one", "tap-two"})

    def test_fake_proc_session_without_exact_claim_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("claude", "--permission-mode", "bypassPermissions"))
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)


if __name__ == "__main__":
    unittest.main()
