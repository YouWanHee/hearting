#!/usr/bin/env python3
"""The background exchange: one worker, a write trigger, a read trigger.

Every check drives the public CLI in throwaway stores.  The remote is always a
local bare repository created here, or off; no real store, settings file, or
remote is read.  A detached ``_exchange-worker`` may outlive the command that
started it, so each test waits for its workers to finish (bounded) and fails
if one is still alive at the end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import MEMORY_DIR, git, init_bare, load_module


MEM = MEMORY_DIR / "mem.py"
REF = "refs/heads/memory-v2"
WAIT_SECONDS = 90.0


class ExchangeScheduleTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init")
        git(self.project, "remote", "add", "origin",
            "https://example.invalid/team/project.git")
        self.remote = init_bare(self.root / "remote.git")
        self.stores = {name: self.root / f"store-{name}" for name in ("a", "b")}
        self.exchanges = {name: self.root / f"exchange-{name}.git" for name in ("a", "b")}
        self.projects_source = self.root / "runtime-projects"
        self.projects_source.mkdir()
        (self.root / "home").mkdir()
        config = self.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')
        (self.root / "gitconfig").write_text(
            "[user]\n\tname = exchange tests\n\temail = exchange@example.invalid\n")
        for name in self.stores:
            result = self._mem(name, "index", "--rebuild", auto="0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self._activate_fresh(name)

    def tearDown(self):
        try:
            for name in self.stores:
                self._wait_idle(name)
            leftovers = self._workers()
            for pid in leftovers:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.assertEqual(leftovers, [], "an exchange worker outlived its test")
        finally:
            self.tempdir.cleanup()

    # ---- fixtures -------------------------------------------------------

    def _environment(self, name, *, remote=False, auto="on", extra=None):
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_"))
        }
        env.update({
            "AGENT_HOME": str(self.root / "agent-home"),
            "CODEX_SESSIONS": str(self.root / "codex-sessions"),
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "HOME": str(self.root / "home"),
            "MEM_EXCHANGE_AUTO": auto,
            "MEM_EXCHANGE_WINDOW_SECONDS": "1",
            "MEM_PROJECTS": str(self.projects_source),
            "MEM_RECALL_EVENTS": str(self.root / f"events-{name}.jsonl"),
            "MEM_RECALL_RECEIPTS": str(self.root / f"receipts-{name}"),
            "MEM_STORE": str(self.stores[name]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / f"state-{name}"),
        })
        if remote:
            env.update({
                "MEM_SYNC_DIR": str(self.exchanges[name]),
                "MEM_SYNC_REF": REF,
                "MEM_SYNC_REMOTE": "1",
                "MEM_SYNC_REMOTE_URL": str(self.remote),
            })
        env.update(extra or {})
        return env

    def _mem(self, name, *args, remote=False, auto="on", extra=None):
        return subprocess.run(
            [sys.executable, str(MEM), *args],
            cwd=self.project,
            env=self._environment(name, remote=remote, auto=auto, extra=extra),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def _activate_fresh(self, name):
        sync = load_module("sync_v2")
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            connection.execute("BEGIN IMMEDIATE")
            sync.initialize_fresh_v2_epoch(
                connection, "test-fresh-epoch", proof="empty-store-proof")
            sync.activate_v2_only_fence(
                connection, "test-fresh-epoch",
                fence_proof="test-suite-v2-only-writer-proof",
                operator_authorized=True)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _written_id(result):
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("[write]"):
                return line.rsplit(maxsplit=1)[-1]
        raise AssertionError(f"no record id in {result.stdout!r}")

    def _state(self, name):
        try:
            return json.loads((self.stores[name] / ".exchange-state.json").read_text())
        except (OSError, ValueError):
            return {}

    def _set_state(self, name, **values):
        state = self._state(name)
        state.update(values)
        (self.stores[name] / ".exchange-state.json").write_text(json.dumps(state))

    def _workers(self, name=None):
        """PIDs of live ``_exchange-worker`` processes started under this test."""
        needle = (f"MEM_STORE={self.stores[name]}" if name
                  else f"MEM_STORE={self.root}").encode()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                if b"_exchange-worker" not in cmdline:
                    continue
                if needle in (entry / "environ").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
        return found

    def _wait_idle(self, name, timeout=WAIT_SECONDS):
        """Until no worker runs for this store and none is still starting."""
        import fcntl
        deadline = time.monotonic() + timeout
        lock = self.stores[name] / ".exchange-run.lock"
        while time.monotonic() < deadline:
            free = True
            if lock.exists():
                fd = os.open(lock, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    free = False
                finally:
                    os.close(fd)
            starting = self._state(name).get("worker_spawned_at", 0)
            if free and not starting and not self._workers(name):
                return
            time.sleep(0.1)
        self.fail(f"exchange worker for store {name} did not finish in {timeout}s")

    def _remote_ops(self):
        try:
            names = git(self.remote, "ls-tree", "-r", "--name-only", REF).splitlines()
        except subprocess.CalledProcessError:
            return []
        return [item for item in names if item.startswith("protocol/v2/ops/")]

    def _outbox_states(self, name):
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            return [row[0] for row in connection.execute(
                "SELECT state FROM sync_outbox ORDER BY queued_at")]
        finally:
            connection.close()

    def _sql(self, name, statement, params=()):
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            connection.execute(statement, params)
            connection.commit()
        finally:
            connection.close()

    # ---- immediate reads and expiry ---------------------------------------

    def test_add_is_found_at_once_without_a_rebuild(self):
        added = self._mem(
            "a", "add", "durable", "lesson",
            "자동 교환은 쓰기 직후 색인을 다시 만들지 않아도 찾힌다",
            "--headline", "immediate zebra headline",
            "--entity", "zebra-entity", auto="0")
        self.assertEqual(added.returncode, 0, added.stderr)
        rid = self._written_id(added)
        candidate = self._mem("a", "candidates", "zebra", auto="0")
        self.assertIn(rid, candidate.stdout)
        english = self._mem("a", "recall", "zebra", auto="0")
        self.assertIn(rid, english.stdout)
        korean = self._mem("a", "recall", "색인을 다시", auto="0")
        self.assertIn(rid, korean.stdout, "CJK bigram shadow must be incremental")
        self.assertEqual(self._mem("a", "index", auto="0").returncode, 0)

    def test_expired_working_is_hidden_pending_stays_and_a_write_run_applies_it(self):
        gone = self._written_id(self._mem(
            "a", "add", "working", "thread", "expired marker quokka body",
            "--headline", "expired quokka headline", auto="0"))
        held = self._written_id(self._mem(
            "a", "add", "working", "thread", "pending marker quokka body",
            "--headline", "pending quokka headline", "--requires-consume", auto="0"))
        alive = self._written_id(self._mem(
            "a", "add", "working", "thread", "fresh marker quokka body",
            "--headline", "fresh quokka headline", auto="0"))
        for rid in (gone, held):
            self._sql("a", "UPDATE records SET expires='2000-01-01' WHERE id=?", (rid,))
        # candidates and recall print ids; inject prints the record bodies.
        for command, marks in ((("candidates", "quokka"), (gone, held, alive)),
                               (("recall", "quokka"), (gone, held, alive)),
                               (("inject",), ("expired marker", "pending marker",
                                              "fresh marker"))):
            out = self._mem("a", *command, auto="0").stdout
            self.assertNotIn(marks[0], out, command)
            self.assertIn(marks[1], out, command)
            self.assertIn(marks[2], out, command)

        # A write-triggered run makes the hidden expiry real; pending survives.
        trigger = self._mem("a", "add", "working", "thread",
                            "a trigger body long enough to be stored as its own record",
                            "--headline", "trigger headline")
        self.assertEqual(trigger.returncode, 0, trigger.stderr)
        self._wait_idle("a")
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            remaining = {row[0] for row in connection.execute("SELECT id FROM records")}
        finally:
            connection.close()
        self.assertNotIn(gone, remaining)
        self.assertIn(held, remaining)
        self.assertIn(alive, remaining)

    # ---- write trigger ----------------------------------------------------

    def test_write_reaches_the_remote_after_the_window_and_writes_batch(self):
        started = time.monotonic()
        first = self._mem("a", "add", "durable", "lesson", "batched write one",
                          "--headline", "batch one", remote=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertLess(time.monotonic() - started, 30, "the write must not wait")
        for index in (2, 3):
            more = self._mem("a", "add", "durable", "lesson", f"batched write {index}",
                             "--headline", f"batch {index}", remote=True)
            self.assertEqual(more.returncode, 0, more.stderr)
        self._wait_idle("a")
        self.assertEqual(len(self._remote_ops()), 3)
        self.assertEqual(set(self._outbox_states("a")), {"confirmed"})
        runs = self._state("a")["run_count"]
        self.assertLessEqual(runs, 2, "three quick writes share one run plus one follow-up")
        self.assertGreaterEqual(runs, 1)

    def test_inline_mode_runs_the_same_pass_without_a_detached_process(self):
        result = self._mem("a", "add", "durable", "lesson", "inline exchange",
                           "--headline", "inline", remote=True, auto="inline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._workers(), [])
        self.assertEqual(len(self._remote_ops()), 1)
        state = self._state("a")
        self.assertEqual(state["done_gen"], state["dirty_gen"])

    def test_remote_off_only_commits_the_dump_and_never_touches_a_remote(self):
        subprocess.run(["git", "init", "-q", str(self.stores["a"])], check=True)
        result = self._mem("a", "add", "durable", "lesson", "local only body",
                           "--headline", "local only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self._wait_idle("a")
        log = git(self.stores["a"], "log", "--format=%s")
        self.assertIn("dump", log)
        self.assertIn("local only body", (self.stores["a"] / "dump.jsonl").read_text())
        self.assertEqual(git(self.remote, "for-each-ref"), "")
        self.assertFalse(self.exchanges["a"].exists())
        self.assertFalse((self.root / "state-a" / "hearting" / "memory-sync"
                          / "exchange").exists())
        # Reads never start a worker when the remote is off.
        self._set_state("a", last_success_receive=time.time() - 3600,
                        last_receive_attempt=0)
        runs = self._state("a")["run_count"]
        self._mem("a", "candidates", "local")
        self._mem("a", "recall", "local")
        self._mem("a", "inject")
        time.sleep(1.0)
        self.assertEqual(self._workers(), [])
        self.assertEqual(self._state("a")["run_count"], runs)

    def test_the_off_switch_starts_no_worker(self):
        self._mem("a", "add", "durable", "lesson", "no worker please",
                  "--headline", "quiet", remote=True, auto="0")
        self._mem("a", "candidates", "quiet", remote=True, auto="0")
        time.sleep(1.0)
        self.assertEqual(self._workers(), [])
        self.assertFalse((self.stores["a"] / ".exchange-state.json").exists())

    def test_worker_marked_sessions_write_trigger_but_do_not_read_trigger(self):
        marked = [{"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                  {"AGENT_DISPATCH_DEPTH": "2"}, {"OPENCODE_DISPATCH_SLUG": "job"},
                  {"FLEET_TITLE_REFRESH": "1"}]
        for extra in marked:
            self._set_state("a", last_success_receive=time.time() - 3600,
                            last_receive_attempt=0)
            before = dict(self._state("a"))
            self._mem("a", "candidates", "anything", remote=True, extra=extra)
            self._mem("a", "recall", "anything", remote=True, extra=extra)
            self._mem("a", "inject", remote=True, extra=extra)
            time.sleep(0.5)
            self.assertEqual(self._workers(), [], extra)
            self.assertEqual(self._state("a").get("last_receive_attempt", 0), 0, extra)
            self.assertEqual(self._state("a").get("run_count"), before.get("run_count"), extra)
        self._mem("a", "add", "durable", "lesson", "worker authored write",
                  "--headline", "worker write", remote=True,
                  extra={"AGENT_SESSION_ROLE": "worker"})
        self._wait_idle("a")
        self.assertEqual(len(self._remote_ops()), 1)

    # ---- read trigger -----------------------------------------------------

    def test_a_stale_read_receives_once_and_shows_the_record_on_the_next_read(self):
        rid = self._written_id(self._mem(
            "a", "add", "durable", "lesson", "the first server wrote this",
            "--headline", "exchange narwhal headline", remote=True))
        self._wait_idle("a")
        self.assertEqual(len(self._remote_ops()), 1)

        self._set_state("b", last_success_receive=time.time() - 11 * 60)
        first = self._mem("b", "candidates", "narwhal", remote=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn(rid, first.stdout, "the current read uses the current local state")
        self._wait_idle("b")
        state = self._state("b")
        self.assertEqual(state["run_count"], 1)
        self.assertLess(time.time() - state["last_success_receive"], 120)
        second = self._mem("b", "candidates", "narwhal", remote=True)
        self.assertIn(rid, second.stdout, "the received record shows from the next read")
        time.sleep(0.5)
        self.assertEqual(self._workers("b"), [], "a fresh receive starts no new worker")
        self.assertEqual(self._state("b")["run_count"], 1)

    def test_simultaneous_stale_reads_start_one_worker(self):
        self._set_state("b", last_success_receive=time.time() - 3600)
        env = self._environment("b", remote=True)
        readers = [subprocess.Popen(
            [sys.executable, str(MEM), "candidates", "anything"], cwd=self.project,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(5)]
        for reader in readers:
            reader.communicate(timeout=60)
            self.assertEqual(reader.returncode, 0)
        self._wait_idle("b")
        self.assertEqual(self._state("b")["run_count"], 1)

    # ---- failure ----------------------------------------------------------

    def test_a_failed_exchange_warns_once_keeps_exit_codes_and_retries_later(self):
        good_remote = self.remote
        self.remote = self.root / "missing-remote.git"
        try:
            added = self._mem("a", "add", "durable", "lesson", "written while offline",
                              "--headline", "offline write", remote=True)
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertNotIn("[sync]", added.stderr)
            self._wait_idle("a")
            self.assertIn("failure_notice", self._state("a"))
            runs = self._state("a")["run_count"]

            # A hook-driven read neither warns nor uses the notice up.
            hook_read = self._mem("a", "candidates", "offline", remote=True)
            self.assertEqual(hook_read.returncode, 0)
            self.assertNotIn("[sync]", hook_read.stderr)
            self.assertIn("failure_notice", self._state("a"))

            recalled = self._mem("a", "recall", "offline", remote=True)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(recalled.stderr.count("[sync]"), 1, recalled.stderr)
            self.assertEqual(len(recalled.stderr.strip().splitlines()), 1)
            again = self._mem("a", "recall", "offline", remote=True)
            self.assertEqual(again.returncode, 0)
            self.assertNotIn("[sync]", again.stderr)
            time.sleep(0.5)
            self.assertEqual(self._workers(), [])
            self.assertEqual(self._state("a")["run_count"], runs,
                             "a dead remote is not retried on every read")
            self.assertEqual(self._outbox_states("a"), ["rendered"])
        finally:
            self.remote = good_remote

        # The remote comes back: the next write also sends the backlog.
        self._mem("a", "add", "durable", "lesson", "written after recovery",
                  "--headline", "recovered write", remote=True)
        self._wait_idle("a")
        self.assertEqual(len(self._remote_ops()), 2)
        self.assertEqual(set(self._outbox_states("a")), {"confirmed"})
        self.assertNotIn("failure_notice", self._state("a"))

    def test_the_worker_survives_its_parents_process_group_being_killed(self):
        env = self._environment("a", remote=True, extra={"MEM_EXCHANGE_WINDOW_SECONDS": "3"})
        parent = subprocess.Popen(
            ["sh", "-c",
             f'"{sys.executable}" "{MEM}" add durable lesson "group kill body" '
             '--headline "group kill" >/dev/null; sleep 60'],
            cwd=self.project, env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not self._workers("a"):
                time.sleep(0.05)
            self.assertTrue(self._workers("a"), "the write did not start a worker")
            os.killpg(parent.pid, signal.SIGKILL)
        finally:
            parent.wait(timeout=30)
        self._wait_idle("a")
        self.assertEqual(len(self._remote_ops()), 1)

    # ---- executor shape ---------------------------------------------------

    def test_routine_pass_skips_migrate_index_and_the_read_run_skips_expiry(self):
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(MEMORY_DIR)!r})\n"
            "import mem\n"
            "out = {}\n"
            "for apply_expiry in (False, True):\n"
            "    mem._sync_locked(json_output=True, routine=True, apply_expiry=apply_expiry)\n"
            "    out[str(apply_expiry)] = mem._LAST_SYNC_STATUS['phases']\n"
            "print(json.dumps(out))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=self.project,
            env=self._environment("a", auto="0"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        phases = json.loads(result.stdout.strip().splitlines()[-1])
        for flag in ("False", "True"):
            self.assertEqual(phases[flag]["migrate"], "skipped")
            self.assertEqual(phases[flag]["index"], "skipped")
            self.assertEqual(phases[flag]["compatibility-export"], "ok")
        self.assertEqual(phases["False"]["lifecycle"], "skipped")
        self.assertEqual(phases["True"]["lifecycle"], "ok")

    def test_explicit_sync_keeps_its_phases_and_starts_no_worker(self):
        result = self._mem("a", "sync", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        phases = json.loads(result.stdout)["phases"]
        for phase in ("migrate", "lifecycle", "index", "compatibility-export"):
            self.assertEqual(phases[phase], "ok", phase)
        time.sleep(0.5)
        self.assertEqual(self._workers(), [])
        self.assertFalse((self.stores["a"] / ".exchange-state.json").exists())


if __name__ == "__main__":
    unittest.main()
