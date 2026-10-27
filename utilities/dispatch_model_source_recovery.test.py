#!/usr/bin/env python3
"""Settle only the historical rotated-frame duplicate through normal writers."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities")]
import dispatch_contract as D  # noqa: E402

_fixture_spec = importlib.util.spec_from_file_location(
    "model_source_registry_fixture", ROOT / "utilities/dispatch_registry.test.py")
FIXTURE = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(FIXTURE)


class HistoricalModelSourceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "jobs.log"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("AGENT_", "HEARTING_", "HARNESS_"))
        }
        self.env.update(HARNESS_STATE_ROOT=str(self.base / "state"),
                        AGENT_DISPATCH_JOBS=str(self.jobs))
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def metadata(self, **changes):
        result = {
            "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "frame", "route_node": "frame-alternative",
            "attempt_id": "att-historical-frame", "route_id": "rt-model-source",
            "route_hash": "sha256:" + "a" * 64,
            "model_diversity": "rotated", "model_source": "profile+diversity",
            "model_profile": "balanced", "model": "gpt-fixture",
        }
        result.update(changes)
        return result

    def pipe(self, metadata=None, *, sources=None):
        metadata = self.metadata() if metadata is None else metadata
        parts = [f"{key}={value}" for key, value in metadata.items()]
        if sources is None:
            parts.append("model_source=profile+diversity")
        else:
            parts = [part for part in parts if not part.startswith("model_source=")]
            parts.extend(f"model_source={value}" for value in sources)
        return ",".join(parts)

    def row(self, pipe, *, status="open", slug="historical-frame"):
        return f"2026-10-27T00:00:00Z\t{status}\t{self.base}\t{self.base}\t{slug}\t{pipe}"

    def invoke(self, attempt, *, apply=False):
        command = [sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
                   "reconcile", "--jobs", str(self.jobs), "--agent-home", str(self.base),
                   "--attempt", attempt, "--only-exact-dead"]
        if apply:
            command.append("--apply")
        with FIXTURE.fixture_cli_scope(self.jobs):
            result = subprocess.run(command, capture_output=True, text=True,
                                    env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_duplicate_refused(self, pipe, *, terminal=True):
        with self.assertRaises(D.DispatchContractError) as caught:
            D._updated_attempt_metadata(pipe, {"note": "dead-exact-pid"}, terminal=terminal)
        self.assertEqual(caught.exception.reason, "attempt-immutable-metadata-duplicate")

    def test_terminal_normalization_preserves_first_field_and_immutable_identity(self):
        pipe = self.pipe()
        expected = pipe.rsplit(",model_source=profile+diversity", 1)[0]
        normalized = D._updated_attempt_metadata(pipe, {}, terminal=True)
        self.assertEqual(normalized, expected)
        self.assertEqual(D.parse_registry_metadata(pipe), D.parse_registry_metadata(normalized))
        self.assertEqual(D._immutable_attempt_identity(self.row(pipe).split("\t")),
                         D._immutable_attempt_identity(self.row(normalized).split("\t")))
        self.assertEqual(D._updated_attempt_metadata(normalized, {}, terminal=True), normalized)

    def test_ordinary_mutation_still_refuses_the_historical_duplicate(self):
        pipe = self.pipe()
        self.assert_duplicate_refused(pipe, terminal=False)
        self.jobs.write_text(self.row(pipe) + "\n")
        before = self.jobs.read_bytes()
        with self.assertRaises(D.DispatchContractError) as caught:
            D.annotate_attempt_row(self.jobs, "att-historical-frame", {"pid": "99"})
        self.assertEqual(caught.exception.reason, "attempt-immutable-metadata-duplicate")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_conflicting_values_extra_copies_and_other_duplicates_stay_refused(self):
        for sources in (("profile", "profile+diversity"),
                        ("profile+diversity", "pin"),
                        ("profile", "profile"),
                        ("profile+diversity",) * 3):
            with self.subTest(sources=sources):
                self.assert_duplicate_refused(self.pipe(sources=sources))
        for extra in ("route_id=rt-model-source", "attempt_id=att-other",
                      "model=gpt-fixture"):
            with self.subTest(extra=extra):
                self.assert_duplicate_refused(self.pipe() + "," + extra)

    def test_wrong_or_missing_provenance_stays_refused(self):
        changes = {
            "attempt_schema_version": "1", "dispatch_depth": "2",
            "transport": "interactive", "execution_surface": "codex-native-subagent",
            "registered_worker": "0", "worker_type": "stage", "route_node": "frame",
            "model_diversity": "distinct", "attempt_id": "", "route_id": "", "route_hash": "",
        }
        for key, value in changes.items():
            with self.subTest(key=key, value=value):
                self.assert_duplicate_refused(self.pipe(self.metadata(**{key: value})))
            with self.subTest(missing=key):
                metadata = self.metadata()
                del metadata[key]
                self.assert_duplicate_refused(self.pipe(metadata))

    def test_normalization_does_not_authorize_immutable_changes(self):
        with self.assertRaises(D.DispatchContractError) as caught:
            D._updated_attempt_metadata(self.pipe(), {"model": "gpt-foreign"}, terminal=True)
        self.assertEqual(caught.exception.reason, "attempt-immutable-metadata-mutation")

    def test_exact_close_honors_revalidation_and_is_idempotent(self):
        pipe = self.pipe()
        self.jobs.write_text(self.row(pipe) + "\n")
        before = self.jobs.read_bytes()
        self.assertFalse(D.close_attempt_row_if(
            self.jobs, "att-historical-frame", "dead-exact-pid", lambda _fields: False))
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertTrue(D.close_attempt_row_if(
            self.jobs, "att-historical-frame", "dead-exact-pid", lambda _fields: True))
        settled = self.jobs.read_bytes()
        fields = settled.decode().strip().split("\t")
        metadata = D.parse_registry_metadata(fields[5])
        self.assertEqual(fields[1], "done")
        self.assertEqual(metadata["note"], "dead-exact-pid")
        self.assertEqual(fields[5].count("model_source="), 1)
        self.assertNotIn("completed-marker", fields[5])
        self.assertEqual(D._immutable_attempt_identity(self.row(pipe).split("\t")),
                         D._immutable_attempt_identity(fields))
        self.assertFalse(D.close_attempt_row_if(
            self.jobs, "att-historical-frame", "dead-exact-pid", lambda _fields: True))
        self.assertEqual(self.jobs.read_bytes(), settled)

    def test_conflicting_row_cannot_close(self):
        self.jobs.write_text(self.row(self.pipe(sources=("pin", "profile+diversity"))) + "\n")
        before = self.jobs.read_bytes()
        self.assertFalse(D.close_attempt_row_if(
            self.jobs, "att-historical-frame", "dead-exact-pid", lambda _fields: True))
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_reconcile_only_settles_selected_dead_row_and_preserves_live_and_conflict(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, env=self.env)
        try:
            live = self.metadata(attempt_id="att-live-frame", **D.process_launch_identity(process.pid))
            dead = self.metadata(pid="99999999", pid_start=FIXTURE.FIXTURE_BIRTH, pgid="99999999")
            conflict = self.metadata(attempt_id="att-conflict-frame", pid="99999998",
                                     pid_start=FIXTURE.FIXTURE_BIRTH, pgid="99999998")
            dead_row = self.row(self.pipe(dead))
            live_row = self.row(self.pipe(live), slug="live-frame")
            conflict_row = self.row(self.pipe(conflict, sources=("pin", "profile+diversity")),
                                    slug="conflict-frame")
            self.jobs.write_text("\n".join((dead_row, live_row, conflict_row)) + "\n")
            before = self.jobs.read_bytes()
            self.assertEqual(self.invoke("att-historical-frame")["closed"], 0)
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertEqual(self.invoke("att-live-frame", apply=True)["closed"], 0)
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertEqual(self.invoke("att-conflict-frame", apply=True)["closed"], 0)
            self.assertEqual(self.jobs.read_bytes(), before)
            applied = self.invoke("att-historical-frame", apply=True)
            self.assertEqual(applied["closed"], 1, applied)
            self.assertEqual(applied["decisions"][0]["category"], "exact-dead")
            lines = self.jobs.read_text().splitlines()
            self.assertEqual(lines[1:], [live_row, conflict_row])
            fields = lines[0].split("\t")
            self.assertEqual(fields[1], "done")
            self.assertEqual(fields[5].count("model_source="), 1)
            self.assertIn("note=dead-exact-pid", fields[5])
            self.assertNotIn("completed-marker", fields[5])
            settled = self.jobs.read_bytes()
            self.assertEqual(self.invoke("att-historical-frame", apply=True)["closed"], 0)
            self.assertEqual(self.jobs.read_bytes(), settled)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
