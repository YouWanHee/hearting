#!/usr/bin/env python3
"""Operator exchange skips local lifecycle; transport guards stay authoritative."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

MEM = Path(__file__).resolve().parents[1] / "mem.py"
ROOT = MEM.parents[2]
LOCAL_PHASES = ("migrate", "lifecycle", "index", "compatibility-export")
REF = "refs/heads/isolated-exchange-only"


class ExchangeOnlyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mem-exchange-only-", dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.remote = self.root / "remote.git"
        self.env = self.environment("writer")
        self.call(["git", "init", "--bare", str(self.remote)])
        self.mem("index", "--rebuild")
        self.store = Path(self.env["MEM_STORE"])

    def environment(self, name):
        base = self.root / name
        env = {"PATH": os.defpath, "HOME": str(base / "home"),
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "AGENT_HOME": str(ROOT), "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0",
               "MEM_DUMP_COMMIT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
               "MEM_SYNC_REMOTE_URL": str(self.remote), "MEM_SYNC_REF": REF}
        for key, suffix in {"XDG_CONFIG_HOME":"config", "XDG_STATE_HOME":"state",
                            "XDG_DATA_HOME":"data", "XDG_CACHE_HOME":"cache",
                            "MEM_STORE":"store", "MEM_PROJECTS":"projects",
                            "CODEX_SESSIONS":"sessions", "MEM_PROFILE":"profile",
                            "MEM_SYNC_DIR":"exchange", "MEM_RECALL_RECEIPTS":"recall"}.items():
            env[key] = str(base / suffix)
        Path(env["HOME"]).mkdir(parents=True)
        env["MEM_WRITE_EVENTS"] = str(base / "state/write-events.jsonl")
        env["MEM_RECALL_EVENTS"] = str(base / "state/recall-events.jsonl")
        return env

    def call(self, argv, *, env=None, rc=0):
        result = subprocess.run([str(item) for item in argv], cwd=self.project,
                                env=self.env if env is None else env,
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, rc, (argv, result.stdout, result.stderr))
        return result

    def mem(self, *args, env=None, rc=0):
        return self.call([sys.executable, MEM, *args], env=env, rc=rc)

    def rows(self, table="records", env=None, columns="*"):
        store = Path((self.env if env is None else env)["MEM_STORE"])
        with contextlib.closing(sqlite3.connect(f"file:{store / 'memory.db'}?mode=ro", uri=True)) as con:
            return con.execute("SELECT " + columns + " FROM " + table + " ORDER BY 1").fetchall()

    def join(self, env=None):
        result = self.mem("migration", "join", "--apply", "--json", env=env)
        self.assertIn(json.loads(result.stdout)["migration_state"], ("fresh-join", "joined"))

    def local_inputs(self):
        # Age only this synthetic writer through its supported transactional
        # writer, rather than editing SQL rows or immutable operation evidence.
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import mem; "
                  "mem.WORKING_TTL_DAYS = -1; "
                  "print(mem.write_record('working', 'project', 'thread', "
                  "'EXCHANGEONLYEXPIRED fixture', quiet=True))")
        rid = self.call([sys.executable, "-c", script, MEM.parent]).stdout.strip()
        namespace = re.sub(r"[/._]", "-", str(self.project))
        source = Path(self.env["MEM_PROJECTS"]) / namespace / "memory/stray.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\ntype: project\n---\nEXCHANGEONLYNATIVE fixture import.\n")
        dump = self.store / "dump.jsonl"
        dump.write_bytes(b"operator-preserved compatibility bytes\n")
        return rid, source, dump

    def assert_skipped(self, result):
        self.assertTrue(result["exchange_only"])
        self.assertEqual(result["migration_count"], 0)
        for phase in LOCAL_PHASES:
            self.assertEqual(result["phases"][phase], "skipped")

    def test_local_only_skips_expiry_and_native_import_but_default_sync_keeps_maintenance(self):
        rid, source, dump = self.local_inputs()
        rows, objects = self.rows(), self.rows("sync_objects")
        original_source, original_dump = source.read_bytes(), dump.read_bytes()
        result = json.loads(self.mem("sync", "--exchange-only", "--json").stdout)
        self.assert_skipped(result)
        self.assertEqual(result["status"], "local-only")
        self.assertTrue(all(value == "disabled" for phase, value in result["phases"].items()
                            if phase.startswith("remote-")))
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.rows("sync_objects"), objects)
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(dump.read_bytes(), original_dump)
        self.assertFalse(Path(self.env["MEM_SYNC_DIR"]).exists())
        ordinary = json.loads(self.mem("sync", "--json").stdout)
        self.assertFalse(ordinary["exchange_only"])
        self.assertEqual(ordinary["migration_count"], 1)
        self.assertTrue(all(ordinary["phases"][p] == "ok" for p in LOCAL_PHASES))
        self.assertFalse(any(row[0] == rid for row in self.rows()))
        self.assertTrue(any("EXCHANGEONLYNATIVE" in row[0] for row in self.rows(columns="body")))
        self.assertGreater(len(self.rows("sync_objects")), len(objects))
        self.assertNotEqual(dump.read_bytes(), original_dump)
        self.assertEqual(source.read_bytes(), original_source)

    def test_remote_exchange_confirms_and_fresh_reader_folds_without_local_maintenance(self):
        self.join()
        rid, source, dump = self.local_inputs()
        rows = self.rows()
        source_bytes, dump_bytes = source.read_bytes(), dump.read_bytes()
        env = {**self.env, "MEM_SYNC_REMOTE":"1"}
        result = json.loads(self.mem("sync", "--exchange-only", "--json", env=env).stdout)
        self.assert_skipped(result)
        self.assertEqual(result["status"], "remote-confirmed")
        self.assertEqual(result["phases"]["remote-confirm"], "ok")
        self.assertEqual(self.rows(), rows)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(dump.read_bytes(), dump_bytes)
        with contextlib.closing(sqlite3.connect(f"file:{self.store / 'memory.db'}?mode=ro", uri=True)) as con:
            self.assertEqual(con.execute("SELECT DISTINCT state FROM sync_outbox").fetchall(), [("confirmed",)])
        paths = self.call(["git", "--git-dir", self.remote, "ls-tree", "-r", "--name-only", REF]).stdout.splitlines()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith("protocol/v2/ops/"))
        reader = self.environment("reader")
        self.join(reader)
        reader["MEM_SYNC_REMOTE"] = "1"
        fetched = json.loads(self.mem("sync", "--exchange-only", "--json", env=reader).stdout)
        self.assert_skipped(fetched)
        self.assertEqual(fetched["status"], "remote-confirmed")
        self.assertEqual(fetched["phases"]["remote-fold"], "ok")
        self.assertEqual(self.rows(env=reader), rows)
        self.assertFalse((Path(reader["MEM_STORE"]) / "dump.jsonl").exists())
        shown = self.mem("show", rid, env=reader).stdout
        self.assertIn("EXCHANGEONLYEXPIRED", shown)

    def test_exchange_only_cannot_bypass_unseeded_writer_fence(self):
        self.mem("add", "durable", "decision", "EXCHANGEONLYFENCE fixture")
        rows, objects = self.rows(), self.rows("sync_objects")
        result = json.loads(self.mem("sync", "--exchange-only", "--json",
                                    env={**self.env, "MEM_SYNC_REMOTE":"1"}, rc=2).stdout)
        self.assert_skipped(result)
        self.assertEqual(result["status"], "hard-failure")
        self.assertTrue(all(value == "blocked" for phase, value in result["phases"].items()
                            if phase.startswith("remote-")))
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.rows("sync_objects"), objects)
        self.assertFalse(Path(self.env["MEM_SYNC_DIR"]).exists())

    def test_exchange_only_retains_path_guard(self):
        self.join()
        self.mem("add", "durable", "decision", "EXCHANGEONLYPATH fixture")
        rows, objects = self.rows(), self.rows("sync_objects")
        forbidden = self.project / "exchange"
        result = json.loads(self.mem("sync", "--exchange-only", "--json",
                                    env={**self.env, "MEM_SYNC_REMOTE":"1", "MEM_SYNC_DIR":str(forbidden)}, rc=2).stdout)
        self.assert_skipped(result)
        self.assertEqual(result["status"], "hard-failure")
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.rows("sync_objects"), objects)
        self.assertFalse(forbidden.exists())


if __name__ == "__main__":
    unittest.main()
