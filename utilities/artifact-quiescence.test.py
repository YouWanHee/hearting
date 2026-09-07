#!/usr/bin/env python3
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import fcntl
from unittest.mock import patch

P = Path(__file__).with_name("artifact-quiescence.py")
S = importlib.util.spec_from_file_location("artifact_quiescence_tested", P)
Q = importlib.util.module_from_spec(S)
S.loader.exec_module(Q)


class QuiescenceTest(unittest.TestCase):
    def indexed(self, config, *paths):
        Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1,
            "registries": {str(i): {"path": str(p)} for i, p in enumerate(paths)}}))

    def test_missing_registry_is_sealed_skip_and_preserves_live_neighbor(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            missing = base / "gone" / "registry.json"
            self.indexed(config, missing)
            evidence = base / "missing.json"
            value = Q.publish(str(evidence), config)
            self.assertTrue(value["proven"], value)
            self.assertIn(str(missing), [r["path"] for r in value["sources"]["jobs"]["files"]
                                        if r["kind"] == "missing"])
            self.assertEqual(value["sources"]["jobs"]["diagnostics"][0]["kind"], "missing-registry")
            self.assertTrue(Q.validate(str(evidence), allow_fixture=True)["proven"])
            missing.parent.mkdir(); missing.write_text('{"schema_version":1,"runs":{}}')
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])
            identity = Q.RESOURCES.proc_identity(os.getpid())
            missing.write_text(json.dumps({"schema_version": 1, "runs": {
                "active": {**identity, "status": "running"}}}))
            self.indexed(config, missing, base / "another-missing.json")
            live = Q.publish(str(base / "live.json"), config)
            self.assertTrue(live["observation_valid"], live)
            self.assertEqual(live["open_jobs"], 1)
            self.assertFalse(live["proven"])

    def test_dangling_registry_is_not_missing_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            link = base / "link"; link.symlink_to(base / "absent")
            for path in (link, link / "child.json"):
                self.indexed(config, path)
                self.assertFalse(Q.publish(str(base / "bad.json"), config)["observation_valid"])

    def test_registry_seen_only_by_scan_cannot_disappear_between_bookends(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            source = base / "transient.json"; self.indexed(config, source)
            original = Q.RESOURCES.scan
            def transient(*args, **kwargs):
                source.write_text('{"schema_version":1,"runs":{}}')
                try:
                    return original(*args, **kwargs)
                finally:
                    source.unlink()
            with patch.object(Q.RESOURCES, "scan", transient):
                value = Q.publish(str(base / "transient-evidence.json"), config)
            self.assertFalse(value["observation_valid"], value)
            self.assertEqual(value["reason"], "source-changed-during-observation")

    def test_registry_permission_corruption_and_fifo_fail_closed_with_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            source = base / "source.json"; self.indexed(config, source)
            for text in ('{', '[]', '{"schema_version":99,"runs":{}}'):
                source.write_text(text)
                value = Q.publish(str(base / "bad.json"), config)
                self.assertFalse(value["observation_valid"], value)
                self.assertEqual(value["source_diagnostics"][0]["path"], str(source))
            original = os.open
            def denied(path, *args, **kwargs):
                if Path(path) == source:
                    raise PermissionError("fixture denied")
                return original(path, *args, **kwargs)
            with patch.object(os, "open", denied):
                value = Q.publish(str(base / "denied.json"), config)
            self.assertFalse(value["proven"])
            self.assertEqual(value["source_diagnostics"][0]["path"], str(source))
            source.unlink(); os.mkfifo(source)
            value = Q.publish(str(base / "fifo.json"), config)
            self.assertFalse(value["proven"])

    def test_valid_registry_symlink_is_sealed_and_retargeting_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            a = base / "a.json"; b = base / "b.json"
            for p in (a, b):
                p.write_text('{"schema_version":1,"runs":{}}')
            link = base / "link.json"; link.symlink_to(a); self.indexed(config, link)
            evidence = base / "evidence.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            link.unlink(); link.symlink_to(b)
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def authority_fixture(self, base):
        config = self.fixture(base)
        peer = base / "peer"; peer.mkdir()
        (peer / "jobs.log").write_text("")
        (peer / "resource-runs.index.json").write_text('{"schema_version":1,"registries":{}}')
        config["authority_roots"] = [str(base), str(peer)]
        return config, peer

    def test_both_harnesses_reject_distinct_authorities_even_when_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config, peer = self.authority_fixture(base)
            answers = []
            for selected in (base, peer):
                local = {**config, "dispatch_jobs": str(selected / "jobs.log"),
                         "resource_index": str(selected / "resource-runs.index.json")}
                value = Q.publish(str(base / (selected.name + "-evidence.json")), local)
                self.assertFalse(value["proven"], value)
                self.assertEqual(value["reason"], "observation-authority-mismatch")
                self.assertTrue(value["sources"]["authority"]["roots_read"])
                answers.append(value["source_diagnostics"])
            self.assertEqual(answers[0], answers[1])
            # A nonempty peer must not become invisible to the empty observer.
            (peer / "jobs.log").write_text("malformed but not empty")
            self.assertFalse(Q.publish(str(base / "nonempty.json"), config)["proven"])

    def test_authority_alias_is_one_source_but_new_peer_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            alias = base / "alias"; alias.symlink_to(base, target_is_directory=True)
            absent = base / "later"
            config["authority_roots"] = [str(base), str(alias), str(absent)]
            evidence = base / "proof.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            self.assertTrue(Q.validate(str(evidence), allow_fixture=True)["proven"])
            absent.mkdir(); (absent / "jobs.log").write_text("")
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def test_authority_equal_bytes_replacement_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            evidence = base / "proof.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            replacement = base / "replacement.log"; replacement.write_text("")
            replacement.replace(config["dispatch_jobs"])
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def test_live_authority_candidates_include_both_harnesses_and_keep_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            env = {"HOME": str(base / "user"), "CODEX_HOME": str(base / "private-codex"),
                   "XDG_STATE_HOME": str(base / "state"),
                   "AGENT_DISPATCH_JOBS": config["dispatch_jobs"],
                   "AGENT_RESOURCE_RUN_INDEX": config["resource_index"]}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(Q.subprocess, "check_output", return_value=config["artifact_root"]), \
                    patch.object(Q.RESOURCES, "agent_home", return_value=base / "agent"):
                live = Q.live_config()
                self.assertEqual(live["dispatch_jobs"], config["dispatch_jobs"])
                roots = set(live["authority_roots"])
                self.assertIn(str(base / "state" / "hearting" / "dispatch"), roots)
                self.assertIn(str(base / "user" / ".codex" / ".harness" / "dispatch"), roots)
                self.assertIn(str(base / "private-codex" / ".harness" / "dispatch"), roots)
                self.assertEqual(os.environ["AGENT_DISPATCH_JOBS"], config["dispatch_jobs"])

    def test_authority_unreadable_peer_and_malformed_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config, peer = self.authority_fixture(base)
            original = os.open
            def denied(path, *args, **kwargs):
                if Path(path) == peer / "jobs.log":
                    raise PermissionError("fixture peer denied")
                return original(path, *args, **kwargs)
            with patch.object(os, "open", denied):
                result = Q.publish(str(base / "denied.json"), config)
            self.assertEqual(result["reason"], "observation-authority-unverifiable")
            self.assertEqual(result["source_diagnostics"][0]["path"], str(peer / "jobs.log"))
            for roots in ([], ["relative"], [str(base / "..")], "not-a-list"):
                bad = {**config, "authority_roots": roots}
                self.assertFalse(Q.publish(str(base / "bad.json"), bad)["proven"])

    def test_authority_change_during_observation_and_scope_forgery_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            original = Q._dispatch_rows
            def replace_after_read(jobs):
                rows = original(jobs)
                replacement = base / "replace.log"; replacement.write_text("")
                replacement.replace(jobs)
                return rows
            with patch.object(Q, "_dispatch_rows", replace_after_read):
                value = Q.publish(str(base / "changed.json"), config)
            self.assertEqual(value["reason"], "source-changed-during-observation")
            evidence = base / "proof.json"; value = Q.publish(str(evidence), config)
            value["scope"] = "live"; evidence.write_text(json.dumps(value))
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def fixture(self, base: Path):
        artifact_root = base / "artifacts"
        (artifact_root / ".runtime" / "routes").mkdir(parents=True)
        index = base / "resource-runs.index.json"
        index.write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")
        jobs = base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        return Q.fixture_config(str(artifact_root), str(index), str(jobs))

    def test_lock_is_ownership_not_path_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            lock = Path(config["lock_path"])
            lock.touch()
            empty = Q.publish(str(base / "empty.json"), config)
            self.assertFalse(empty["lock_present"])
            self.assertTrue(empty["proven"])
            lock.write_text("stale-owner\n", encoding="utf-8")
            stale = Q.publish(str(base / "stale.json"), config)
            self.assertFalse(stale["lock_present"])
            self.assertTrue(stale["proven"])
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = Q.publish(str(base / "held.json"), config)
                self.assertTrue(held["lock_present"])
                self.assertFalse(held["proven"])
                self.assertEqual(held["pending"], 1)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_lock_malformed_or_changing_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            lock = Path(config["lock_path"])
            lock.write_bytes(b"bad\x00owner")
            result = Q.publish(str(base / "bad.json"), config)
            self.assertFalse(result["observation_valid"])
            self.assertFalse(result["proven"])

            original = Q._source_snapshots
            calls = [0]
            def changing(current):
                calls[0] += 1
                value = original(current)
                if calls[0] == 2:
                    Path(current["lock_path"]).touch()
                return value
            Q._source_snapshots = changing
            try:
                changed = Q.publish(str(base / "changed.json"), config)
            finally:
                Q._source_snapshots = original
            self.assertFalse(changed["observation_valid"])
            self.assertFalse(changed["proven"])

    def test_zero_pair_is_independent_atomic_and_brackets_fold(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            before = base / "before.json"
            after = base / "after.json"
            first = Q.publish(str(before), config, now - timedelta(seconds=2))
            second = Q.publish(str(after), config, now)
            self.assertTrue(first["proven"] and second["proven"])
            self.assertEqual(first["pending"], sum(first[key] for key in Q.COUNT_KEYS))
            self.assertNotEqual(first["observation_id"], second["observation_id"])
            self.assertFalse(list(base.glob("*.tmp")))
            proof = Q.pair(
                str(before), str(after),
                (now - timedelta(seconds=1.5)).isoformat(),
                (now - timedelta(seconds=.5)).isoformat(),
                now=now, allow_fixture=True,
            )
            self.assertTrue(proof["proven"], proof)
            self.assertFalse(Q.pair(str(before), str(before), now.isoformat(), now.isoformat(),
                                    now=now, allow_fixture=True)["proven"])

    def test_each_open_dimension_prevents_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            route = Path(config["artifact_root"]) / ".runtime" / "routes" / "rt-open.json"
            route.write_text(json.dumps({"route_id": "rt-open", "nodes": []}), encoding="utf-8")
            route_payload = Q.publish(str(base / "route.json"), config, now)
            self.assertEqual(route_payload["open_routes"], 1)
            self.assertFalse(Q.validate(str(base / "route.json"), now=now, allow_fixture=True)["proven"])
            route.unlink()

            identity = Q.RESOURCES.proc_identity(os.getpid())
            registry = base / "resource.json"
            registry.write_text(json.dumps({"schema_version": 1, "runs": {
                "unrelated": {**identity, "status": "running", "started_at": now.timestamp()}
            }}), encoding="utf-8")
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {
                "fixture": {"path": str(registry), "registered_at": now.timestamp(), "updated_at": now.timestamp()}
            }}), encoding="utf-8")
            job_payload = Q.publish(str(base / "job.json"), config, now)
            self.assertEqual(job_payload["open_jobs"], 1)
            self.assertFalse(job_payload["proven"])
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")

            Path(config["dispatch_jobs"]).write_text(
                "2026-08-21T00:00:00Z\topen\t/repo\t/worktree\tfixture\t"
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,route_id=rt-fixture,route_node=test,"
                "attempt_id=att-fixture-open\n", encoding="utf-8")
            dispatch_payload = Q.publish(str(base / "dispatch.json"), config, now)
            self.assertEqual(dispatch_payload["open_dispatch_attempts"], 1, dispatch_payload)
            self.assertFalse(dispatch_payload["proven"])

    def test_missing_malformed_stale_future_offset_counts_and_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            evidence = base / "evidence.json"
            original = Q.publish(str(evidence), config, now)
            self.assertTrue(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])

            cases = []
            malformed = dict(original); malformed["schema_version"] = 999; cases.append(malformed)
            stale = dict(original); stale["observed_at"] = (now - timedelta(hours=1)).isoformat(); cases.append(stale)
            future = dict(original); future["observed_at"] = (now + timedelta(minutes=2)).isoformat(); cases.append(future)
            offsetless = dict(original); offsetless["observed_at"] = now.replace(tzinfo=None).isoformat(); cases.append(offsetless)
            negative = dict(original); negative["open_jobs"] = -1; cases.append(negative)
            wrong_sum = dict(original); wrong_sum["pending"] = 1; cases.append(wrong_sum)
            for payload in cases:
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"], payload)

            evidence.write_text(json.dumps(original), encoding="utf-8")
            Path(config["dispatch_jobs"]).write_text("malformed\n", encoding="utf-8")
            self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])
            self.assertFalse(Q.validate(str(base / "missing.json"), now=now, allow_fixture=True)["proven"])

    def test_missing_authoritative_source_publishes_false(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            Path(config["resource_index"]).unlink()
            result = Q.publish(str(base / "evidence.json"), config)
            self.assertFalse(result["observation_valid"])
            self.assertFalse(result["proven"])
            self.assertEqual(result["pending"], 0)

    def test_unrelated_root_json_is_evidence_but_malformed_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            artifact_root = Path(config["artifact_root"])
            (artifact_root / "inventory.json").write_text('{"kind":"not-a-route"}', encoding="utf-8")
            valid = Q.publish(str(base / "valid.json"), config)
            self.assertTrue(valid["observation_valid"], valid)
            self.assertTrue(valid["proven"], valid)
            # SD-OPEN-54 (#15): the gate ledger beside a route record is a typed
            # sidecar, and a stray non-route basename is non-blocking evidence.
            (artifact_root / ".runtime" / "routes" / "rt-0123456789abcdef.gate-release.json").write_text(
                '{"schema_version":1,"route_id":"rt-0123456789abcdef","gate_releases":[]}', encoding="utf-8")
            (artifact_root / ".runtime" / "routes" / "notes.json").write_text('{"kind":"note"}', encoding="utf-8")
            still_valid = Q.publish(str(base / "still-valid.json"), config)
            self.assertTrue(still_valid["observation_valid"], still_valid)
            self.assertTrue(still_valid["proven"], still_valid)
            (artifact_root / ".runtime" / "routes" / "rt-fedcba9876543210.json").write_text('{', encoding="utf-8")
            invalid = Q.publish(str(base / "invalid.json"), config)
            self.assertFalse(invalid["observation_valid"], invalid)
            self.assertFalse(invalid["proven"], invalid)


if __name__ == "__main__":
    unittest.main()
