#!/usr/bin/env python3
"""Strict distiller-output regression tests; subprocess state is private."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

APPLIER = Path(__file__).resolve().parents[1] / "apply-distill-actions.py"
MISSING = object()

class StrictDistillOutputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apply-distill-strict-", dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.output = self.root / "model-output.jsonl"
        self.snapshot = self.root / "snapshot-ids.txt"
        self.calls_path = self.root / "mem-calls.jsonl"
        self.fake_mem = self.root / "fake-mem.py"
        self.fake_mem.write_text(textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import sys
            call = {"argv": sys.argv[1:], "actor": os.environ.get("MEM_ACTOR")}
            path = Path(os.environ["FAKE_MEM_CALLS"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(call, sort_keys=True) + "\\n")
            if sys.argv[1:2] == [os.environ.get("FAKE_MEM_FAIL_ACTION")]:
                raise SystemExit(int(os.environ.get("FAKE_MEM_FAIL_RC", "9")))
            """), encoding="utf-8")
        private = self.root / "private"
        self.env = {
            "PATH": os.defpath, "HOME": str(private / "home"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": str(private / "config"), "XDG_STATE_HOME": str(private / "state"),
            "XDG_DATA_HOME": str(private / "data"), "XDG_CACHE_HOME": str(private / "cache"),
            "MEM_STORE": str(private / "store"), "MEM_PROJECTS": str(private / "projects"),
            "MEM_WRITE_EVENTS": str(private / "state/write-events.jsonl"),
            "MEM_RECALL_EVENTS": str(private / "state/recall-events.jsonl"),
            "MEM_RECALL_RECEIPTS": str(private / "state/recall-receipts"),
            "FAKE_MEM_CALLS": str(self.calls_path),
        }
        Path(self.env["HOME"]).mkdir(parents=True)

    @staticmethod
    def jsonl(*records):
        return "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)

    @staticmethod
    def valid_add(body="VALID_ADD_BODY"):
        return {"action": "add", "tier": "durable", "type": "artifact-pointer", "body": body,
                "headline": "Read the memory contract", "aliases": ["strict output", "curator output"],
                "entities": ["D-40"], "topics": ["memory"], "artifact_refs": ["core/MEMORY.md"]}

    def run_cli(self, content=MISSING, *options, extra_env=None):
        self.calls_path.unlink(missing_ok=True)
        if content is MISSING:
            self.output.unlink(missing_ok=True)
        else:
            self.output.write_text(content, encoding="utf-8")
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, str(APPLIER), str(self.output), str(self.fake_mem), *options],
                              cwd=self.workspace, env=env, text=True, capture_output=True, timeout=10)

    def run_strict(self, content=MISSING, *options, extra_env=None):
        return self.run_cli(content, *options, "--strict-output", extra_env=extra_env)

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]

    def assert_strict_rejected(self, result, *, secret=None):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(result.stdout, "")
        self.assertIn("[distill-parse]", result.stderr)
        if secret is not None:
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_empty_and_whitespace_strict_outputs_are_noops(self):
        for content in ("", " \n\t\n"):
            with self.subTest(content=repr(content)):
                result = self.run_strict(content)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_exact_sole_noop_is_accepted(self):
        result = self.run_strict('\n  {"action": "noop"}  \n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_noop_must_be_exact_and_solo(self):
        valid = self.jsonl(self.valid_add("NOOP_MIX_SECRET"))
        cases = {"extra-field": '{"action":"noop","reason":"nothing"}\n',
                 "noop-first": '{"action":"noop"}\n' + valid,
                 "noop-last": valid + '{"action":"noop"}\n'}
        for name, content in cases.items():
            with self.subTest(name=name):
                self.assert_strict_rejected(self.run_strict(content), secret="NOOP_MIX_SECRET")

    def test_missing_output_file_is_strict_error(self):
        self.assert_strict_rejected(self.run_strict())

    def test_strict_rejects_invalid_records_without_running_mem(self):
        bad_shape = self.valid_add("STRICT_BAD_SHAPE_BODY")
        bad_shape["headline"] = ""
        unexpected = self.valid_add("STRICT_EXTRA_FIELD_BODY")
        unexpected["commentary"] = "model prose is not an action field"
        cases = {"malformed-prose": "MODEL_PROSE_IS_NOT_JSON\n", "non-object": '["not","an","object"]\n',
                 "unknown-action": '{"action":"teleport"}\n', "non-string-action": '{"action":[]}\n',
                 "delete": '{"action":"delete","id":"record-a"}\n',
                 "invalid-existing-shape": self.jsonl(bad_shape), "unexpected-field": self.jsonl(unexpected)}
        for name, content in cases.items():
            with self.subTest(name=name):
                result = self.run_strict(content)
                self.assert_strict_rejected(result)
                if "BODY" in content:
                    self.assertNotIn("STRICT_", result.stdout + result.stderr)

    def test_strict_canonical_parse_rejects_duplicate_keys_without_body_leak(self):
        content = ('{"action":"add","tier":"durable","type":"decision",'
                   '"body":"DUPLICATE_SECRET_ONE","body":"DUPLICATE_SECRET_TWO","headline":"Duplicate body"}\n')
        result = self.run_strict(content)
        self.assert_strict_rejected(result, secret="DUPLICATE_SECRET_ONE")
        self.assertNotIn("DUPLICATE_SECRET_TWO", result.stdout + result.stderr)

    def test_apply_actions_keyword_enables_strict_mode(self):
        self.calls_path.unlink(missing_ok=True)
        self.output.write_text('{"action":"unknown"}\n', encoding="utf-8")
        spec = importlib.util.spec_from_file_location("strict_applier", APPLIER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(stderr):
            result = module.apply_actions(str(self.output), str(self.fake_mem), strict_output=True)
        self.assertEqual(result, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("[distill-parse]", stderr.getvalue())

    def test_cli_strict_prevalidates_whole_batch_before_any_subprocess(self):
        content = self.jsonl(self.valid_add("PREVALIDATE_SECRET_BODY")) + "MALFORMED_AFTER_VALID_ACTION\n"
        self.assert_strict_rejected(self.run_strict(content), secret="PREVALIDATE_SECRET_BODY")

    def test_strict_increment_executes_valid_add_with_existing_shape(self):
        result = self.run_strict(self.jsonl(self.valid_add()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [{"actor": None, "argv": [
            "add", "durable", "artifact-pointer", "VALID_ADD_BODY", "--headline", "Read the memory contract",
            "--alias", "strict output", "--alias", "curator output", "--entity", "D-40", "--topic", "memory",
            "--artifact-ref", "core/MEMORY.md"]}])

    def test_strict_increment_remains_add_only(self):
        self.snapshot.write_text("record-a\n", encoding="utf-8")
        self.assert_strict_rejected(self.run_strict('{"action":"prune","id":"record-a"}\n',
                                                   "--snapshot-ids", str(self.snapshot)))

    def test_strict_curate_executes_existing_id_mutation_shapes(self):
        ids = [f"record-{letter}" for letter in "abcdefgh"]
        self.snapshot.write_text("\n".join(ids) + "\n", encoding="utf-8")
        records = ({"action": "reinforce", "id": ids[0]}, {"action": "prune", "id": ids[1]},
                   {"action": "graduate", "id": ids[2], "to": "durable"}, {"action": "reattribute", "id": ids[3]},
                   {"action": "merge", "ids": ids[4:6], "canonical": ids[4]},
                   {"action": "supersede", "id": ids[6], "by": ids[7]})
        result = self.run_strict(self.jsonl(*records), "--mode", "curate", "--snapshot-ids", str(self.snapshot))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["argv"] for call in self.calls()], [
            ["reinforce", ids[0]], ["prune", ids[1]], ["graduate", ids[2], "--to", "durable"],
            ["reattribute", ids[3]], ["merge", "--canonical", ids[4], ids[4], ids[5]],
            ["supersede", ids[6], "--by", ids[7]]])
        self.assertTrue(all(call["actor"] == "curator" for call in self.calls()))

    def test_strict_curate_requires_snapshot_ids_for_id_mutation(self):
        self.assert_strict_rejected(self.run_strict('{"action":"reinforce","id":"record-a"}\n', "--mode", "curate"))

    def test_pending_id_absent_from_destructive_allowlist_is_denied(self):
        self.snapshot.write_text("safe-record\n", encoding="utf-8")
        result = self.run_strict('{"action":"prune","id":"pending-record"}\n',
                                 "--mode", "curate", "--snapshot-ids", str(self.snapshot))
        self.assert_strict_rejected(result)
        self.assertNotIn("pending-record", result.stdout + result.stderr)

    def test_periodic_strict_curate_denies_reattribute(self):
        self.snapshot.write_text("orphan-record\n", encoding="utf-8")
        self.assert_strict_rejected(self.run_strict('{"action":"reattribute","id":"orphan-record"}\n',
            "--mode", "curate", "--snapshot-ids", str(self.snapshot), "--deny-reattribute"))

    def test_mem_failure_returns_one_and_stops_remaining_actions(self):
        ids = ["record-a", "record-b", "record-c"]
        self.snapshot.write_text("\n".join(ids) + "\n", encoding="utf-8")
        content = self.jsonl({"action": "reinforce", "id": ids[0]}, {"action": "prune", "id": ids[1]},
                             {"action": "graduate", "id": ids[2]})
        result = self.run_strict(content, "--mode", "curate", "--snapshot-ids", str(self.snapshot),
                                extra_env={"FAKE_MEM_FAIL_ACTION": "prune", "FAKE_MEM_FAIL_RC": "9"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([call["argv"][0] for call in self.calls()], ["reinforce", "prune"])
        self.assertIn("[distill-apply]", result.stderr)
        self.assertNotIn("record-", result.stdout + result.stderr)

    def test_strict_rejects_adversarial_field_types_without_running_mem(self):
        base = self.valid_add("ADVERSARIAL_FIELD_SECRET")
        cases = {
            "action-list": {**base, "action": []},
            "tier-list": {**base, "tier": []},
            "type-list": {**base, "type": []},
            "type-object": {**base, "type": {}},
            "body-list": {**base, "body": []},
            "headline-object": {**base, "headline": {}},
            "aliases-object": {**base, "aliases": {}},
            "aliases-bad-item": {**base, "aliases": [[]]},
            "entities-object": {**base, "entities": {}},
            "topics-object": {**base, "topics": {}},
            "artifact-refs-object": {**base, "artifact_refs": {}},
            "id-object": {"action": "reinforce", "id": {}},
            "graduate-to-list": {"action": "graduate", "id": "record-a", "to": []},
            "merge-ids-object": {"action": "merge", "ids": {}, "canonical": "record-a"},
            "merge-id-container": {"action": "merge", "ids": ["record-a", []], "canonical": "record-a"},
            "canonical-list": {"action": "merge", "ids": ["record-a", "record-b"], "canonical": []},
            "supersede-by-object": {"action": "supersede", "id": "record-a", "by": {}},
        }
        self.snapshot.write_text("record-a\nrecord-b\n", encoding="utf-8")
        for name, record in cases.items():
            with self.subTest(name=name):
                result = self.run_strict(self.jsonl(record), "--mode", "curate", "--snapshot-ids", str(self.snapshot))
                self.assert_strict_rejected(result, secret="ADVERSARIAL_FIELD_SECRET")

    def test_manual_default_keeps_mixed_valid_invalid_skip_behavior(self):
        invalid = self.valid_add("MANUAL_INVALID_BODY")
        invalid["type"] = "lesson"
        valid = self.valid_add("MANUAL_VALID_BODY")
        content = "MODEL_PROSE\n" + self.jsonl(invalid, valid, {"action": "unknown"})
        result = self.run_cli(content)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"][0:5], ["add", "durable", "artifact-pointer", "MANUAL_VALID_BODY", "--headline"])
        self.assertIn("skip malformed", result.stderr)
        self.assertIn("skip unsupported automatic type", result.stderr)
        self.assertIn("skip unknown action", result.stderr)

if __name__ == "__main__":
    unittest.main()
