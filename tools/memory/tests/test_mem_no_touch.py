#!/usr/bin/env python3
"""Scout show/recall reads leave the serving store and runtime state untouched."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

MEM = Path(__file__).resolve().parents[1] / "mem.py"


class NoTouchCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mem-no-touch-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = self.root / "store"
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        self.events = self.root / "events.jsonl"
        self.env = self._environment()
        added = self.run_mem("add", "durable", "lesson", "no-touch fixture body", "--scope", "global")
        self.assertEqual(added.returncode, 0, added.stderr)
        with sqlite3.connect(self.store / "memory.db") as con:
            self.rid = con.execute("SELECT id FROM records").fetchone()[0]
            con.execute("UPDATE records SET last_accessed='2001-01-01' WHERE id=?", (self.rid,))
        self.paths = [Path(str(self.store / "memory.db") + suffix)
                      for suffix in ("", "-wal", "-shm")]

    def _environment(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.update({"HOME": str(self.home), "XDG_STATE_HOME": str(self.state),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "MEM_STORE": str(self.store), "MEM_RECALL_EVENTS": str(self.events),
                    "MEM_EXCHANGE_AUTO": "0", "TMPDIR": "/tmp"})
        env.update(extra)
        return env

    def run_mem(self, *args, env=None):
        return subprocess.run([sys.executable, str(MEM), *args], cwd=self.project,
                              env=self._environment(**(env or {})), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    @staticmethod
    def snapshot(paths):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns,
                       path.stat().st_mode & 0o777) for path in paths if path.exists()}

    def test_no_touch_show_and_full_recall_preserve_store_events_and_import_cache(self):
        # Use a new state root and remove bytecode overrides so import-time
        # writes cannot be hidden by a test-only environment setting.
        env = {"XDG_STATE_HOME": str(self.root / "fresh-state"), "MEM_EXCHANGE_AUTO": "1"}
        exchange_state = self.store / ".exchange-state.json"
        exchange_state.write_text(json.dumps({"failure_notice": {"reason": "fixture"}}))
        before = self.snapshot(self.paths + [self.events, exchange_state])
        show = self.run_mem("show", self.rid, "--no-touch", env=env)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("last_accessed: 2001-01-01", show.stdout)
        recall = self.run_mem("recall", "no-touch fixture", "--full", "--json",
                              "--no-touch", env=env)
        self.assertEqual(recall.returncode, 0, recall.stderr)
        self.assertIn(self.rid, recall.stdout)
        self.assertEqual(self.snapshot(self.paths + [self.events, exchange_state]), before)
        self.assertFalse((self.root / "fresh-state" / "hearting" / "pycache").exists())
        self.assertFalse((self.store / ".exchange-schedule.lock").exists())
        self.assertFalse((self.store / ".exchange-run.lock").exists())
        with sqlite3.connect(self.store / "memory.db") as con:
            self.assertEqual(con.execute("SELECT last_accessed FROM records WHERE id=?",
                                         (self.rid,)).fetchone()[0], "2001-01-01")

    def test_wal_only_update_is_visible_without_changing_serving_files(self):
        writer = sqlite3.connect(self.store / "memory.db")
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE records SET body='wal-only-newest fixture body' WHERE id=?",
                       (self.rid,))
        writer.commit()
        before = self.snapshot(self.paths)
        show = self.run_mem("show", self.rid, "--no-touch")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("wal-only-newest", show.stdout)
        recall = self.run_mem("recall", "wal-only-newest", "--full", "--no-touch")
        self.assertEqual(recall.returncode, 0, recall.stderr)
        self.assertIn("wal-only-newest", recall.stdout)
        self.assertEqual(self.snapshot(self.paths), before)

    def test_no_touch_respects_project_pending_superseded_and_injection_fences(self):
        created = {}
        for name, options in (
            ("visible", []),
            ("pending", ["--requires-consume"]),
            ("flagged", []),
            ("historical", []),
            ("cross-project", []),
        ):
            result = self.run_mem("add", "durable", "lesson",
                                  f"fence marker {name}", "--scope", "project", *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            created[name] = result.stdout.split()[-1]
        with sqlite3.connect(self.store / "memory.db") as con:
            con.execute("UPDATE records SET injection_flag=1 WHERE id=?",
                        (created["flagged"],))
            con.execute("UPDATE records SET status='superseded' WHERE id=?",
                        (created["historical"],))
            con.execute("UPDATE records SET cwd_origin='different-project' WHERE id=?",
                        (created["cross-project"],))
        before = self.snapshot(self.paths + [self.events])
        visible = self.run_mem("show", created["visible"], "--no-touch")
        pending = self.run_mem("show", created["pending"], "--no-touch")
        flagged = self.run_mem("show", created["flagged"], "--all", "--no-touch")
        historical = self.run_mem("show", created["historical"], "--no-touch")
        cross = self.run_mem("show", created["cross-project"], "--no-touch")
        self.assertEqual(visible.returncode, 0, visible.stderr)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertEqual(flagged.returncode, 1)
        self.assertEqual(historical.returncode, 1)
        self.assertEqual(cross.returncode, 1)
        result = self.run_mem("recall", "fence marker", "--json", "--no-touch")
        self.assertEqual(result.returncode, 0, result.stderr)
        ids = {row["id"] for row in json.loads(result.stdout)["results"]}
        self.assertIn(created["visible"], ids)
        self.assertIn(created["pending"], ids)
        self.assertNotIn(created["flagged"], ids)
        self.assertNotIn(created["historical"], ids)
        self.assertNotIn(created["cross-project"], ids)
        self.assertEqual(self.snapshot(self.paths + [self.events]), before)
        with sqlite3.connect(self.store / "memory.db") as con:
            self.assertEqual(con.execute("SELECT delivery_state FROM records WHERE id=?",
                                         (created["pending"],)).fetchone()[0], "pending")

    def test_no_touch_missing_store_does_not_initialize_it(self):
        missing = self.root / "missing-store"
        result = self.run_mem("show", "missing", "--no-touch",
                              env={"MEM_STORE": str(missing)})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(missing.exists())

    def test_incompatible_old_schema_returns_a_read_only_error(self):
        old_store = self.root / "old-store"
        old_store.mkdir()
        old_db = old_store / "memory.db"
        with sqlite3.connect(old_db) as con:
            con.execute("CREATE TABLE records (id TEXT PRIMARY KEY)")
        before = self.snapshot([old_db])
        result = self.run_mem("show", "legacy-id", "--no-touch",
                              env={"MEM_STORE": str(old_store), "MEM_EXCHANGE_AUTO": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("read-only lookup failed", result.stderr)
        self.assertEqual(self.snapshot([old_db]), before)

    def test_ordinary_main_show_and_recall_keep_access_touch_and_events(self):
        show = self.run_mem("show", self.rid)
        self.assertEqual(show.returncode, 0, show.stderr)
        recall = self.run_mem("recall", "no-touch fixture", "--json")
        self.assertEqual(recall.returncode, 0, recall.stderr)
        with sqlite3.connect(self.store / "memory.db") as con:
            self.assertNotEqual(con.execute("SELECT last_accessed FROM records WHERE id=?",
                                            (self.rid,)).fetchone()[0], "2001-01-01")
        events = [json.loads(line)["event"] for line in self.events.read_text().splitlines()]
        self.assertIn("show", events)
        self.assertIn("explicit-recall", events)


if __name__ == "__main__":
    unittest.main()
