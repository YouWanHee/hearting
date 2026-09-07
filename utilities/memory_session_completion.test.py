#!/usr/bin/env python3
"""Synthetic Linux completion tests; no user store, sessions, config, or model."""
from __future__ import annotations

import concurrent.futures
import copy
import errno
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import memory_session_completion as completion


class CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="memory-completion-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Build from an allowlist: no inherited dispatch, credentials, model,
        # state, session, configuration, network proxy, or memory-store paths.
        self.env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONWARNINGS": "error::ResourceWarning",
                    "HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_DATA_HOME": str(self.root / "data"), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_CACHE_HOME": str(self.root / "cache"), "CODEX_HOME": str(self.root / "codex"),
                    "CLAUDE_CONFIG_DIR": str(self.root / "claude"), "OPENCODE_DB": str(self.root / "opencode.db"),
                    "MEM_STORE": str(self.root / "store"), "TMPDIR": str(self.root / "tmp"),
                    "MEM_SESSION_COMPLETION_RECEIPTS": str(self.root / "receipts"),
                    "OUT": str(self.root / "invocations"), "SLOW": "0"}
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "MEM_STORE", "TMPDIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        self.runners: list[dict] = []
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.cleanup_processes)
        self.exe = self.root / "worker.py"
        self.exe.write_text(f"#!{sys.executable}\n" + '''import json,os,signal,subprocess,sys,time
with open(os.environ['OUT'], 'a') as stream: stream.write('invoked\\n')
if os.environ.get('ENV_OUT'):
    with open(os.environ['ENV_OUT'],'w') as stream: json.dump(dict(os.environ),stream)
if os.environ.get('ARGV_OUT'):
    with open(os.environ['ARGV_OUT'],'w') as stream: json.dump(sys.argv[1:],stream)
if os.environ.get('NOISE'):
    os.write(1, (os.environ['NOISE'] * 100000).encode())
    os.write(2, (os.environ['NOISE'] * 100000).encode())
if os.environ.get('DESCENDANT'):
    child = subprocess.Popen([sys.executable, '-c', "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(os.environ['DESC_READY'],'w').write(str(os.getpid())); time.sleep(3); open(os.environ['LATE'],'w').write('leaked')"])
    while not os.path.exists(os.environ['DESC_READY']): time.sleep(.01)
if os.environ.get('IGNORE_TERM'): signal.signal(signal.SIGTERM,signal.SIG_IGN)
time.sleep(float(os.environ.get('SLOW', '0')))
sys.exit(int(os.environ.get('EXIT','0')))
''', encoding="utf-8")
        self.exe.chmod(0o700)

    def cleanup_processes(self) -> None:
        identities = list(self.runners)
        if (self.root / "receipts").is_dir() and not (self.root / "receipts").is_symlink():
            for path in (self.root / "receipts").glob("*.json"):
                try:
                    row = json.loads(path.read_text())
                    identities.extend(row.get(key) for key in ("command", "runner"))
                except (OSError, ValueError, AttributeError):
                    pass
        for identity in identities:
            if (identity and identity.get("pid") != os.getpid()
                    and completion.identity_alive(identity)):
                try:
                    os.killpg(identity["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for child in self.children:
            if child.poll() is None:
                completion._terminate(child, grace=0)
            child.wait(timeout=2)
        # Reaper threads belong only to finite synthetic runners; join before
        # removing the fixture to avoid cleanup racing a terminal publication.
        import threading
        for thread in threading.enumerate():
            if thread.name == "memory-completion-reap":
                thread.join(timeout=2)

    def launch(self, sid: str = "session", **kwargs) -> dict:
        env = kwargs.pop("env", self.env)
        argv = kwargs.pop("argv", [])
        result = completion.launch("codex", sid, str(self.root), str(self.exe), argv, env=env, **kwargs)
        if result.get("runner"):
            self.runners.append(result["runner"])
        return result

    def wait(self, sid: str = "session", seconds: float = 4) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            row = completion.read_receipt("codex", sid, self.env)
            if row and row["state"] in ("completed", "failed"):
                return row
            time.sleep(.02)
        self.fail("no terminal receipt within synthetic test deadline")

    def row(self, sid: str = "session") -> dict:
        return {"schema": completion.SCHEMA, "key": completion._key("codex", sid), "nonce": "a" * 32,
                "harness": "codex", "session_id_hash": completion.hashlib.sha256(sid.encode()).hexdigest(),
                "state": "started", "reason": "launching", "launcher": completion._identity(os.getpid()),
                "runner": None, "command": None, "started_at": time.time(), "timeout": 1.0,
                "exit_code": None, "runner_exit": None, "duration_ms": 0, "memory_apply": "not-asserted",
                "input_generation": completion.DEFAULT_INPUT_GENERATION}

    def write_row(self, row: dict | bytes, sid: str = "session") -> Path:
        directory = self.root / "receipts"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (completion._key("codex", sid) + ".json")
        path.write_bytes(row if isinstance(row, bytes) else json.dumps(row).encode())
        path.chmod(0o600)
        return path

    def test_all_worker_flags_and_recursion_create_nothing(self) -> None:
        flags = {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1", "AGENT_DISPATCH_DEPTH": "0",
                 "OPENCODE_DISPATCH_SLUG": "synthetic", "FLEET_TITLE_REFRESH": "1", "MEM_DISTILL": "1",
                 "MEM_SESSION_COMPLETION": "1"}
        for key, value in flags.items():
            with self.subTest(key=key), mock.patch.object(completion.subprocess, "Popen") as spawn:
                self.assertEqual(self.launch(env=dict(self.env, **{key: value}))["state"], "excluded")
                spawn.assert_not_called()
                self.assertFalse((self.root / "receipts").exists())

    def test_empty_environment_never_falls_back_to_process_environment(self) -> None:
        with mock.patch.dict(os.environ, dict(self.env, AGENT_SESSION_ROLE="worker"), clear=True):
            self.assertFalse(completion.excluded({}))
            with self.assertRaisesRegex(completion.CompletionError, "missing-state-home"):
                self.launch(env={})
        self.assertFalse((self.root / "receipts").exists())

    def test_reader_is_read_only_when_missing(self) -> None:
        self.assertIsNone(completion.read_receipt("codex", "missing", self.env))
        self.assertFalse((self.root / "receipts").exists())
        env = dict(self.env)
        env.pop("MEM_SESSION_COMPLETION_RECEIPTS")
        self.assertIsNone(completion.read_receipt("codex", "missing", env))
        self.assertFalse((self.root / "state" / "agent-memory").exists())

    def test_timeout_input_bounds_and_nonfinite_rejection(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), 10 ** 1000, True, "1", 0, 3601):
            with self.subTest(explicit=value), self.assertRaises(completion.CompletionError):
                completion._timeout({}, value)
        for value in ("NaN", "Infinity", "-inf", "nonsense"):
            with self.subTest(config=value), self.assertRaises(completion.CompletionError):
                completion._timeout({"MEM_SESSION_COMPLETION_TIMEOUT": value}, None)
        self.assertEqual(completion._timeout({}, None), 900)
        self.assertEqual(completion._timeout({"MEM_SESSION_COMPLETION_TIMEOUT": "-10"}, None), 30)
        self.assertEqual(completion._timeout({"MEM_SESSION_COMPLETION_TIMEOUT": "4000"}, None), 3600)
        self.assertEqual(completion._timeout({}, .1), .1)

    def test_invalid_command_and_identifiers_do_not_create_state(self) -> None:
        for harness, sid in (("../bad", "s"), ("codex", ""), ("codex", "x\n"), ("codex", "x" * 161), ("codex", "\ud800")):
            with self.subTest(harness=harness, sid=repr(sid)), self.assertRaises(completion.CompletionError):
                completion.launch(harness, sid, str(self.root), str(self.exe), [], env=self.env)
        for executable in (str(self.root / "missing"), "relative", str(self.root)):
            with self.subTest(executable=executable), self.assertRaises(completion.CompletionError):
                completion.launch("codex", "s", str(self.root), executable, [], env=self.env)
        for argv in (["\0"], ["x"] * 65, "not-list"):
            with self.subTest(argv=repr(argv)), self.assertRaises(completion.CompletionError):
                completion.launch("codex", "s", str(self.root), str(self.exe), argv, env=self.env)
        self.assertFalse((self.root / "receipts").exists())

    def test_slow_success_is_detached_private_and_duplicate(self) -> None:
        env = dict(self.env, SLOW=".45", ENV_OUT=str(self.root / "worker-env"))
        start = time.monotonic()
        self.assertEqual(self.launch(env=env)["state"], "started")
        self.assertLess(time.monotonic() - start, .4)
        self.assertEqual(self.launch(env=env)["state"], "duplicate")
        row = self.wait()
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["memory_apply"], "not-asserted")
        for path in (self.root / "receipts").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.launch()["reason"], "terminal")
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 1)
        worker_env = json.loads((self.root / "worker-env").read_text())
        self.assertEqual(worker_env["MEM_SESSION_COMPLETION"], "1")
        self.assertNotIn("AGENT_SESSION_ROLE", worker_env)
        self.assertEqual(worker_env["MEM_STORE"], self.env["MEM_STORE"])

    def test_concurrent_duplicate_launches_once(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.launch(env=dict(self.env, SLOW=".3")), range(8)))
        self.assertEqual(sum(row["state"] == "started" for row in results), 1)
        self.assertTrue(all(row["state"] in ("started", "duplicate") for row in results))
        self.assertEqual(self.wait()["state"], "completed")
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 1)

    def test_lease_survives_launcher_exit_and_child_terminal_is_not_overwritten(self) -> None:
        script = ("import json,sys; sys.path.insert(0,sys.argv[1]); import memory_session_completion as c; "
                  "print(json.dumps(c.launch('codex','session',sys.argv[2],sys.argv[3],[],timeout=2)))")
        child = subprocess.Popen([sys.executable, "-c", script, str(Path(completion.__file__).parent), str(self.root), str(self.exe)],
                                 env=dict(self.env, SLOW=".4"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.children.append(child)
        stdout, stderr = child.communicate(timeout=2)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stderr, b"")
        result = json.loads(stdout)
        self.runners.append(result["runner"])
        self.assertEqual(self.launch()["state"], "duplicate")
        self.assertEqual(self.wait()["state"], "completed")
        self.assertEqual(self.launch()["reason"], "terminal")

    def test_nonzero_exit_is_terminal_without_automatic_retry(self) -> None:
        self.launch(env=dict(self.env, EXIT="7"))
        row = self.wait()
        self.assertEqual((row["state"], row["reason"], row["exit_code"]), ("failed", "command-failed", 7))
        self.assertEqual(self.launch()["reason"], "terminal")

    def test_command_spawn_failure_is_terminal(self) -> None:
        self.exe.write_text("#!/nonexistent-synthetic-interpreter\n")
        self.launch()
        row = self.wait()
        self.assertEqual((row["state"], row["reason"]), ("failed", "spawn-failed"))
        self.assertIsNone(row["command"])

    def test_launcher_spawn_failure_is_not_success(self) -> None:
        with mock.patch.object(completion.subprocess, "Popen", side_effect=OSError("private-payload")):
            result = self.launch()
        self.assertEqual(result["state"], "failed")
        row = self.wait()
        self.assertEqual(row["reason"], "spawn-failed")
        self.assertNotIn("private-payload", json.dumps(row))
        self.assertEqual(self.launch()["reason"], "terminal")

    def test_timeout_kills_ignoring_command_and_descendant(self) -> None:
        env = dict(self.env, SLOW="5", IGNORE_TERM="1", DESCENDANT="1",
                   DESC_READY=str(self.root / "descendant"), LATE=str(self.root / "late"))
        self.launch(env=env, timeout=.3)
        row = self.wait()
        self.assertEqual((row["state"], row["reason"]), ("failed", "timeout"))
        self.assertLess(row["duration_ms"], 2000)
        self.assertFalse((self.root / "late").exists())
        self.assert_process_dead(int((self.root / "descendant").read_text()))

    def assert_process_dead(self, pid: int) -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
                if fields[0] == "Z":
                    return
            except FileNotFoundError:
                return
            time.sleep(.01)
        self.fail("synthetic descendant survived group cleanup")

    def test_normal_leader_exit_also_cleans_surviving_descendants(self) -> None:
        self.launch(env=dict(self.env, DESCENDANT="1", DESC_READY=str(self.root / "descendant"), LATE=str(self.root / "late")))
        self.assertEqual(self.wait()["state"], "completed")
        self.assert_process_dead(int((self.root / "descendant").read_text()))
        self.assertFalse((self.root / "late").exists())

    def test_large_output_and_arguments_are_absent_from_receipts(self) -> None:
        secret = "DISTINCTIVE_SYNTHETIC_PAYLOAD"
        result = completion.launch("codex", secret, str(self.root), str(self.exe), [secret], env=dict(self.env, NOISE=secret))
        self.runners.append(result["runner"])
        self.assertEqual(self.wait(secret)["state"], "completed")
        files = list((self.root / "receipts").iterdir())
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertNotIn(secret.encode(), path.read_bytes())
            self.assertLess(path.stat().st_size, completion.MAX_RECEIPT)

    def test_malformed_oversized_and_exact_schema_rejection(self) -> None:
        values = [b"{bad", b" " * (completion.MAX_RECEIPT + 1), b"[]"]
        original = self.row()
        for field, value in (("schema", True), ("key", "0" * 64), ("nonce", ""), ("nonce", "z" * 32),
                             ("harness", "claude"), ("session_id_hash", "0" * 64), ("timeout", float("nan")),
                             ("started_at", float("inf")), ("runner", {"pid": 1}), ("launcher", None),
                             ("duration_ms", True), ("state", "bogus"), ("memory_apply", "success"),
                             ("input_generation", None), ("input_generation", "invalid")):
            row = copy.deepcopy(original)
            row[field] = value
            values.append(json.dumps(row).encode())
        row = dict(original, raw_payload="forbidden")
        values.append(json.dumps(row).encode())
        values.append(json.dumps(original).replace('"schema": 1', '"schema": 1, "schema": 1').encode())
        for value in values:
            with self.subTest(value=value[:60]):
                path = self.write_row(value)
                with self.assertRaises(completion.CompletionError):
                    completion.read_receipt("codex", "session", self.env)
                with self.assertRaises(completion.CompletionError):
                    self.launch()
                self.assertEqual(path.read_bytes(), value)

    def test_private_leaf_and_nofollow_parent_or_leaf(self) -> None:
        root = self.root / "receipts"
        canary = self.root / "canary"
        canary.mkdir(mode=0o700)
        for env in (dict(self.env, MEM_SESSION_COMPLETION_RECEIPTS="relative"),
                    dict(self.env, MEM_SESSION_COMPLETION_RECEIPTS=str(self.root / ".." / "outside"))):
            with self.assertRaises(completion.CompletionError):
                self.launch(env=env)
        root.symlink_to(canary)
        with self.assertRaises(completion.CompletionError):
            self.launch()
        root.unlink()
        root.mkdir(mode=0o755)
        with self.assertRaisesRegex(completion.CompletionError, "receipt-root-insecure"):
            self.launch()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
        root.rmdir()
        root.write_text("canary")
        with self.assertRaises(completion.CompletionError):
            self.launch()
        root.unlink()
        (self.root / "link-parent").symlink_to(canary)
        with self.assertRaises(completion.CompletionError):
            self.launch(env=dict(self.env, MEM_SESSION_COMPLETION_RECEIPTS=str(self.root / "link-parent" / "new")))
        self.assertEqual(list(canary.iterdir()), [])

    def test_replaced_ancestor_keeps_original_open_directory(self) -> None:
        parent = self.root / "parent"
        parent.mkdir(mode=0o700)
        target = parent / "receipts"
        canary = self.root / "canary"
        canary.mkdir(mode=0o700)
        opened = completion.os.open
        replaced = False
        def replace_after_open(path, flags, *args, **kwargs):
            nonlocal replaced
            fd = opened(path, flags, *args, **kwargs)
            if path == "parent" and not replaced:
                parent.rename(self.root / "original-parent")
                parent.symlink_to(canary)
                replaced = True
            return fd
        with mock.patch.object(completion.os, "open", side_effect=replace_after_open):
            fd = completion._leaf(target)
        try:
            row = self.row()
            completion._publish(fd, row["key"] + ".json", row, expected_nonce=None)
        finally:
            os.close(fd)
        self.assertTrue((self.root / "original-parent" / "receipts" / (row["key"] + ".json")).exists())
        self.assertEqual(list(canary.iterdir()), [])

    def test_opened_receipt_is_checked_after_path_race(self) -> None:
        row = self.row()
        path = self.write_row(row)
        unsafe = self.root / "unsafe"
        unsafe.write_text(json.dumps(row))
        unsafe.chmod(0o644)
        opened = completion.os.open
        def raced_open(name, flags, *args, **kwargs):
            if name == path.name:
                os.replace(unsafe, path)
            return opened(name, flags, *args, **kwargs)
        with mock.patch.object(completion.os, "open", side_effect=raced_open), self.assertRaises(completion.CompletionError):
            completion.read_receipt("codex", "session", self.env)

    def test_receipt_and_lock_symlinks_hardlinks_and_modes_are_rejected(self) -> None:
        directory = self.root / "receipts"
        directory.mkdir(mode=0o700)
        canary = self.root / "canary"
        canary.write_text("untouched")
        canary.chmod(0o600)
        for suffix in ("json", "lock"):
            target = directory / (completion._key("codex", "session") + "." + suffix)
            for kind in ("symlink", "hardlink", "insecure", "fifo"):
                with self.subTest(suffix=suffix, kind=kind):
                    if kind == "symlink": target.symlink_to(canary)
                    elif kind == "hardlink": os.link(canary, target)
                    elif kind == "fifo": os.mkfifo(target, mode=0o600)
                    else:
                        target.write_text(json.dumps(self.row()))
                        target.chmod(0o644)
                    with self.assertRaises(completion.CompletionError):
                        self.launch()
                    self.assertEqual(canary.read_text(), "untouched")
                    target.unlink()
            if suffix == "json":
                (directory / (completion._key("codex", "session") + ".lock")).unlink()

    def test_foreign_uid_is_rejected_from_opened_metadata(self) -> None:
        row = self.row()
        self.write_row(row)
        actual = completion.os.fstat
        def foreign(fd):
            st = actual(fd)
            if stat.S_ISREG(st.st_mode):
                values = list(st)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return st
        with mock.patch.object(completion.os, "fstat", side_effect=foreign), self.assertRaises(completion.CompletionError):
            completion.read_receipt("codex", "session", self.env)

    def test_identity_reuse_death_and_ambiguous_inspection(self) -> None:
        identity = completion._identity(os.getpid())
        self.assertEqual(completion.identity_state(identity), "alive")
        reused = dict(identity, start_ticks=identity["start_ticks"] + 1)
        self.assertEqual(completion.identity_state(reused), "dead")
        with mock.patch.object(completion, "_identity", return_value=None), mock.patch.object(completion.os, "kill", side_effect=PermissionError):
            self.assertEqual(completion.identity_state(identity), "unknown")
        with mock.patch.object(completion, "_identity", return_value=None), mock.patch.object(completion.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(completion.identity_state(identity), "dead")
        self.assertEqual(completion.identity_state(None), "unknown")

    def test_stale_unknown_identity_is_not_reclaimed(self) -> None:
        path = self.write_row(self.row())
        before = path.read_bytes()
        with mock.patch.object(completion, "identity_state", return_value="unknown"), self.assertRaisesRegex(completion.CompletionError, "identity-unavailable"):
            self.launch()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(self.env["OUT"]).exists())

    def test_stale_dead_identity_competing_reclaim_starts_once(self) -> None:
        row = self.row()
        row["runner"] = dict(row["launcher"], start_ticks=row["launcher"]["start_ticks"] + 1)
        self.write_row(row)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.launch(env=dict(self.env, SLOW=".25")), range(5)))
        self.assertEqual(sum(item["state"] == "started" for item in results), 1)
        self.assertEqual(self.wait()["state"], "completed")
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 1)

    def test_live_command_identity_blocks_reclaim_even_with_dead_runner(self) -> None:
        row = self.row()
        row["runner"] = dict(row["launcher"], start_ticks=row["launcher"]["start_ticks"] + 1)
        row["command"] = row["launcher"]
        self.write_row(row)
        self.assertEqual(self.launch()["reason"], "active")

    def test_publication_errors_leave_no_temporary_files(self) -> None:
        row = self.row()
        fd = completion._leaf(self.root / "receipts")
        self.addCleanup(os.close, fd)
        for operation in ("open", "write", "fsync", "replace"):
            with self.subTest(operation=operation):
                real = getattr(completion.os, operation)
                def fail(*args, **kwargs):
                    if operation != "open" or str(args[0]).endswith(".tmp"):
                        raise OSError(errno.EIO, "private-payload")
                    return real(*args, **kwargs)
                with mock.patch.object(completion.os, operation, side_effect=fail), self.assertRaises(completion.CompletionError):
                    completion._publish(fd, row["key"] + ".json", row, expected_nonce=None)
                self.assertFalse(any(path.name.endswith(".tmp") for path in (self.root / "receipts").iterdir()))
        with mock.patch.object(completion.os, "write", return_value=0), self.assertRaises(completion.CompletionError):
            completion._publish(fd, row["key"] + ".json", row, expected_nonce=None)
        self.assertEqual(list((self.root / "receipts").iterdir()), [])

    def test_partial_writes_and_nonce_ownership(self) -> None:
        row = self.row()
        fd = completion._leaf(self.root / "receipts")
        self.addCleanup(os.close, fd)
        real_write = completion.os.write
        with mock.patch.object(completion.os, "write", side_effect=lambda fd, data: real_write(fd, data[:3])):
            completion._publish(fd, row["key"] + ".json", row, expected_nonce=None)
        self.assertEqual(completion.read_receipt("codex", "session", self.env), row)
        with self.assertRaisesRegex(completion.CompletionError, "receipt-ownership-changed"):
            completion._publish(fd, row["key"] + ".json", row, expected_nonce="b" * 32)
        self.assertEqual(completion.read_receipt("codex", "session", self.env), row)

    def test_nonce_replaced_during_write_is_not_overwritten(self) -> None:
        row = self.row()
        self.write_row(row)
        fd = completion._leaf(self.root / "receipts")
        self.addCleanup(os.close, fd)
        changed = dict(row, nonce="b" * 32)
        actual = completion.os.fsync
        def replace_during_sync(out):
            self.write_row(changed)
            return actual(out)
        with mock.patch.object(completion.os, "fsync", side_effect=replace_during_sync), self.assertRaisesRegex(completion.CompletionError, "receipt-ownership-changed"):
            completion._publish(fd, row["key"] + ".json", row, expected_nonce=row["nonce"])
        self.assertEqual(completion.read_receipt("codex", "session", self.env), changed)
        self.assertFalse(any(path.name.endswith(".tmp") for path in (self.root / "receipts").iterdir()))

    def test_command_inherits_lease_even_if_runner_dies(self) -> None:
        result = self.launch(env=dict(self.env, SLOW="2"))
        deadline = time.monotonic() + 2
        row = None
        while time.monotonic() < deadline:
            row = completion.read_receipt("codex", "session", self.env)
            if row and row["command"]:
                break
            time.sleep(.01)
        self.assertIsNotNone(row["command"])
        os.kill(result["runner"]["pid"], signal.SIGKILL)
        # Even loss of the runner cannot release the inherited kernel lease
        # while its command could still mutate memory.
        self.assertEqual(self.launch()["reason"], "active")
        lock = os.open(self.root / "receipts" / (row["key"] + ".lock"), os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                completion.fcntl.flock(lock, completion.fcntl.LOCK_EX | completion.fcntl.LOCK_NB)
        finally:
            os.close(lock)

    def test_initial_publish_failure_does_not_spawn_or_claim_success(self) -> None:
        with mock.patch.object(completion, "_publish", side_effect=completion.CompletionError("receipt-write-failed")), mock.patch.object(completion.subprocess, "Popen") as spawn:
            self.assertEqual(self.launch()["state"], "failed")
            spawn.assert_not_called()
        self.assertFalse(Path(self.env["OUT"]).exists())

    def test_initial_postrename_fsync_failure_is_terminal_without_spawn(self) -> None:
        actual = completion.os.fsync
        failed = False
        def fail_once(fd):
            nonlocal failed
            if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
                failed = True
                raise OSError(errno.EIO, "synthetic-fsync-failure")
            return actual(fd)
        with mock.patch.object(completion.os, "fsync", side_effect=fail_once), mock.patch.object(completion.subprocess, "Popen") as spawn:
            self.assertEqual(self.launch()["state"], "failed")
            spawn.assert_not_called()
        self.assertEqual(self.wait()["reason"], "receipt-publish-failed")
        self.assertEqual(self.launch()["reason"], "terminal")

    def test_publication_interruption_also_removes_temporary_file(self) -> None:
        row = self.row()
        fd = completion._leaf(self.root / "receipts")
        self.addCleanup(os.close, fd)
        with mock.patch.object(completion.os, "write", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            completion._publish(fd, row["key"] + ".json", row, expected_nonce=None)
        self.assertEqual(list((self.root / "receipts").iterdir()), [])

    def runner_fixture(self):
        row = self.row()
        fd = completion._leaf(self.root / "receipts")
        name = row["key"] + ".json"
        completion._publish(fd, name, row, expected_nonce=None)
        lock = os.open(row["key"] + ".lock", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=fd)
        completion.fcntl.flock(lock, completion.fcntl.LOCK_EX)
        return row, fd, name, lock

    def test_runner_refuses_changed_nonce_without_overwrite(self) -> None:
        row, fd, name, lock = self.runner_fixture()
        with mock.patch.object(completion.subprocess, "Popen") as spawn:
            self.assertEqual(completion._runner(fd, lock, name, "b" * 32, str(self.exe), [], str(self.root), 1.0), 1)
            spawn.assert_not_called()
        self.assertEqual(completion.read_receipt("codex", "session", self.env), row)

    def test_runner_refuses_replaced_lock_inode(self) -> None:
        row, fd, name, lock = self.runner_fixture()
        lockpath = self.root / "receipts" / (row["key"] + ".lock")
        lockpath.rename(self.root / "old-lock")
        lockpath.touch(mode=0o600)
        with mock.patch.object(completion.subprocess, "Popen") as spawn:
            self.assertEqual(completion._runner(fd, lock, name, row["nonce"], str(self.exe), [], str(self.root), 1.0), 1)
            spawn.assert_not_called()
        self.assertEqual(completion.read_receipt("codex", "session", self.env), row)

    def test_terminal_postrename_fsync_failure_cannot_report_success(self) -> None:
        row, fd, name, lock = self.runner_fixture()
        actual = completion.os.fsync
        def fail_completed(directory):
            if stat.S_ISDIR(os.fstat(directory).st_mode):
                current = completion._read_at(directory, name)
                if current["state"] == "completed":
                    raise OSError(errno.EIO, "synthetic-fsync-failure")
            return actual(directory)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(completion.os, "fsync", side_effect=fail_completed):
            self.assertEqual(completion._runner(fd, lock, name, row["nonce"], str(self.exe), [], str(self.root), 1.0), 1)
        terminal = self.wait()
        self.assertEqual((terminal["state"], terminal["reason"]), ("failed", "receipt-publish-failed"))
        self.assertFalse(any(path.name.endswith(".tmp") for path in (self.root / "receipts").iterdir()))

    def test_unsupported_platform_fails_before_state_creation(self) -> None:
        with mock.patch.object(completion.sys, "platform", "unsupported"), self.assertRaisesRegex(completion.CompletionError, "unsupported-platform"):
            self.launch()
        self.assertFalse((self.root / "receipts").exists())

    def wait_for_lease_release(self) -> None:
        lock = os.open(self.root / "receipts" / (completion._key("codex", "session") + ".lock"), os.O_RDWR)
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    completion.fcntl.flock(lock, completion.fcntl.LOCK_EX | completion.fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    time.sleep(.01)
            self.fail("synthetic completion lease was not released")
        finally:
            os.close(lock)

    def test_generation_validation_precedes_state_creation(self) -> None:
        for generation in (None, "", "x" * 64, "A" * 64, "a" * 63, "a" * 65, b"a" * 64, 1):
            with self.subTest(generation=generation), self.assertRaisesRegex(completion.CompletionError, "invalid-input-generation"):
                self.launch(input_generation=generation)
        self.assertFalse((self.root / "receipts").exists())

    def test_terminal_generation_duplicate_and_resumed_generation_runs(self) -> None:
        first, resumed = "a" * 64, "b" * 64
        argv = ["session-end", "session"]
        env = dict(self.env, ARGV_OUT=str(self.root / "command-argv"))
        self.assertEqual(self.launch(input_generation=first, argv=argv, env=env)["state"], "started")
        old = self.wait()
        self.assertEqual(old["input_generation"], first)
        self.wait_for_lease_release()
        self.assertEqual(self.launch(input_generation=first)["reason"], "terminal")
        self.assertEqual(self.launch(input_generation=resumed, argv=argv, env=env)["state"], "started")
        current = self.wait()
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["input_generation"], resumed)
        self.assertEqual(current["key"], old["key"])
        self.assertEqual(current["session_id_hash"], old["session_id_hash"])
        self.assertNotEqual(current["nonce"], old["nonce"])
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 2)
        self.assertEqual(len(list((self.root / "receipts").iterdir())), 2)
        self.assertEqual(json.loads((self.root / "command-argv").read_text()), argv)
        self.assertNotIn(str(self.root), json.dumps(current))

    def test_failed_generation_deduplicates_but_new_generation_runs(self) -> None:
        first, resumed = "a" * 64, "b" * 64
        self.launch(input_generation=first, env=dict(self.env, EXIT="7"))
        self.assertEqual(self.wait()["state"], "failed")
        self.wait_for_lease_release()
        self.assertEqual(self.launch(input_generation=first)["reason"], "terminal")
        self.assertEqual(self.launch(input_generation=resumed)["state"], "started")
        self.assertEqual(self.wait()["state"], "completed")
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 2)

    def test_active_incoming_generation_is_deferred_without_burning_retry(self) -> None:
        first, resumed = "a" * 64, "b" * 64
        self.launch(input_generation=first, env=dict(self.env, SLOW=".4"))
        self.assertEqual(self.launch(input_generation=first)["state"], "duplicate")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.launch(input_generation=resumed), range(4)))
        self.assertTrue(all(row == {"state": "deferred", "reason": "active-input-generation"} for row in results))
        self.assertEqual(completion.read_receipt("codex", "session", self.env)["input_generation"], first)
        self.assertEqual(self.wait()["input_generation"], first)
        self.wait_for_lease_release()
        self.assertEqual(self.launch(input_generation=resumed)["state"], "started")
        self.assertEqual(self.wait()["input_generation"], resumed)
        self.assertEqual(Path(self.env["OUT"]).read_text().count("invoked"), 2)

    def test_runner_publication_failure_cleans_command_in_finally(self) -> None:
        row = self.row()
        fd = completion._leaf(self.root / "receipts")
        name = row["key"] + ".json"
        completion._publish(fd, name, row, expected_nonce=None)
        lock = os.open(row["key"] + ".lock", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=fd)
        completion.fcntl.flock(lock, completion.fcntl.LOCK_EX)
        real_publish = completion._publish
        raised = False
        def failure(fd, name, value, **kwargs):
            nonlocal raised
            if value["command"] and value["state"] == "started" and not raised:
                raised = True
                raise completion.CompletionError("receipt-write-failed")
            return real_publish(fd, name, value, **kwargs)
        with mock.patch.dict(os.environ, dict(self.env, SLOW="5"), clear=True), mock.patch.object(completion, "_publish", side_effect=failure):
            self.assertEqual(completion._runner(fd, lock, name, row["nonce"], str(self.exe), [], str(self.root), 1.0), 1)
        terminal = self.wait()
        self.assertEqual(terminal["reason"], "receipt-write-failed")
        self.assertFalse(completion.identity_alive(terminal["command"]))


if __name__ == "__main__":
    unittest.main()
