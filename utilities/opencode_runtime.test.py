#!/usr/bin/env python3
"""Read-only OpenCode runtime boundary fixtures; no native process or model calls."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_runtime as runtime


class OpenCodeRunCodecTest(unittest.TestCase):
    def test_v1_flags_and_v2_provider_variant_are_separate(self):
        v1 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", pure=True,
                              version=(1, 19, 0))
        self.assertIn("--pure", v1)
        self.assertIn("--dir", v1)
        self.assertIn("--variant", v1)
        v2 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", session="ses_exact",
                              title="hearting-worker-att-x", pure=True, version=(2, 0, 32))
        self.assertNotIn("--pure", v2)
        self.assertNotIn("--dir", v2)
        self.assertNotIn("--variant", v2)
        self.assertEqual(v2[v2.index("--model") + 1], "opencode-go/model#high")
        self.assertEqual(v2[v2.index("--session") + 1], "ses_exact")
        self.assertEqual(v2[v2.index("--title") + 1], "hearting-worker-att-x")

    def test_database_generation_never_selects_cli_generation(self):
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch.object(runtime, "runtime_version", return_value=None), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}):
            self.assertTrue(runtime.is_v2())

    def test_v2_default_variant_has_no_suffix(self):
        self.assertEqual(runtime.model_reference("openai/gpt-6.1-sol", "runtime-default"),
                         "openai/gpt-6.1-sol")
        self.assertEqual(runtime.model_reference("opencode/model#old", "high"),
                         "opencode/model#high")


class OpenCodeCredentialReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "opencode.db"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, "
                        "label TEXT, value TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")

    def _insert(self, con, identifier, integration, value, active, created):
        con.execute("INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identifier, integration, "label", json.dumps(value), active, created, created))

    def test_active_row_wins_and_reader_does_not_mutate_database(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "old", "opencode-go", {"type": "key", "key": "old-secret"}, 0, 1)
            self._insert(con, "active", "opencode-go", {"type": "key", "key": "selected-secret"}, 1, 2)
        before = self.db.read_bytes()
        row = runtime.read_credential("opencode-go", db_path=self.db)
        self.assertEqual(row["credential_id"], "active")
        self.assertEqual(row["value"]["key"], "selected-secret")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(runtime.credential_store_present(db_path=self.db))

    def test_null_migration_rows_select_newest_only_when_no_flagged_rows(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "legacy-old", "opencode", {"type": "oauth", "access": "old"}, None, 1)
            self._insert(con, "legacy-new", "opencode", {"type": "oauth", "access": "new"}, None, 2)
        self.assertEqual(runtime.read_credential("opencode", db_path=self.db)["credential_id"],
                         "legacy-new")
        with sqlite3.connect(self.db) as con:
            self._insert(con, "inactive", "opencode", {"type": "oauth", "access": "inactive"}, 0, 3)
        self.assertIsNone(runtime.read_credential("opencode", db_path=self.db))

    def test_malformed_values_and_missing_integrations_are_unknown(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("bad", "openai", "label", "not-json", 1, 1, 1))
        row = runtime.read_credential("openai", db_path=self.db)
        self.assertEqual(row["credential_id"], "bad")
        self.assertIsNone(row["value"])
        self.assertFalse(row["credential_valid"])
        self.assertIsNone(runtime.read_credential("missing", db_path=self.db))

    def test_observation_distinguishes_missing_store_missing_row_and_bad_schema(self):
        missing = runtime.read_credential_observation(
            "opencode", db_path=self.db.with_name("not-created.db"))
        self.assertEqual(missing.state, "absent")
        self.assertEqual(runtime.read_credential_observation(
            "opencode", db_path=self.db).state, "absent")
        broken_link = Path(self.temp.name) / "broken-link.db"
        broken_link.symlink_to(Path(self.temp.name) / "missing-target.db")
        self.assertEqual(runtime.read_credential_observation(
            "opencode", db_path=broken_link).state, "unavailable")

        malformed = Path(self.temp.name) / "malformed.db"
        with sqlite3.connect(malformed) as con:
            con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT)")
        observation = runtime.read_credential_observation("opencode", db_path=malformed)
        self.assertEqual(observation.state, "unavailable")
        self.assertEqual(observation.reason, "credential-store-schema-unavailable")
        # Keep the public boundary compatible while internal callers retain status.
        self.assertIsNone(runtime.read_credential("opencode", db_path=malformed))

    def test_selected_query_failure_and_ambiguous_active_rows_are_unavailable(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "default-a", "opencode",
                         {"type": "oauth", "access": "a"}, 1, 2)
            self._insert(con, "default-b", "opencode",
                         {"type": "oauth", "access": "b"}, 1, 3)
        ambiguous = runtime.read_credential_observation("opencode", db_path=self.db)
        self.assertEqual(ambiguous.state, "unavailable")
        self.assertEqual(ambiguous.reason, "credential-selection-ambiguous")

        real_connect = sqlite3.connect

        class FailSelectedQuery:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)

            def __getattr__(self, key):
                return getattr(self.connection, key)

            def __setattr__(self, key, value):
                setattr(self.connection, key, value)

            def execute(self, sql, *args):
                if "FROM credential WHERE" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, *args)

        with unittest.mock.patch.object(
                runtime.sqlite3, "connect",
                side_effect=lambda *args, **kwargs:
                FailSelectedQuery(real_connect(*args, **kwargs))):
            failed_query = runtime.read_credential_observation("opencode", db_path=self.db)
        self.assertEqual(failed_query.state, "unavailable")
        self.assertEqual(failed_query.reason, "credential-store-read-failed")

    def test_locked_database_is_unavailable_not_absent(self):
        with sqlite3.connect(self.db, timeout=0.1) as con:
            self._insert(con, "default", "opencode",
                         {"type": "oauth", "access": "selected"}, 1, 2)
        locker = sqlite3.connect(self.db, timeout=0.1)
        self.addCleanup(locker.close)
        locker.execute("BEGIN EXCLUSIVE")
        observation = runtime.read_credential_observation("opencode", db_path=self.db)
        locker.rollback()
        self.assertEqual(observation.state, "unavailable")
        self.assertEqual(observation.reason, "credential-store-read-failed")


class OpenCodeSessionObservationTest(unittest.TestCase):
    def test_v2_exact_session_and_sequence_reader_needs_no_session_input_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            session_id = "ses_v2fixture"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                            "parent_id TEXT, slug TEXT, title TEXT, agent TEXT, model TEXT, "
                            "time_created INTEGER, time_updated INTEGER, cost REAL, "
                            "tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER)")
                con.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                            "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
                con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (session_id, "/work/tree", None, "slug", "worker", "build",
                             json.dumps({"id": "beam-free", "providerID": "opencode"}),
                             1000, 2000, 0.1, 7, 3, 0))
                con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ("msg_user", session_id, "user", 1, 1000, 1000,
                             json.dumps({"text": "question", "time": {"created": 1000}})))
                con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ("msg_assistant", session_id, "assistant", 2, 1500, 2000,
                             json.dumps({"agent": "build", "model": {"id": "beam-free", "providerID": "opencode"},
                                         "content": [{"type": "text", "text": "OK"}],
                                         "tokens": {"input": 7, "output": 1},
                                         "time": {"created": 1500, "completed": 2000}})))
            before = db.read_bytes()
            observation = runtime.read_session_observation(session_id, db_path=db)
            self.assertEqual(observation["state"], "observed")
            self.assertEqual(observation["generation"], "v2")
            self.assertEqual(observation["session"]["directory"], "/work/tree")
            self.assertEqual(observation["messages"][-1]["text"], "OK")
            self.assertEqual(observation["cursor_kind"], "session-message-seq-v2")
            delta = runtime.read_session_observation(session_id, db_path=db,
                                                    generation="v2", after_seq=2)
            self.assertEqual(delta["messages"], [])
            self.assertEqual(delta["cursor"], 2)
            self.assertEqual(db.read_bytes(), before)

    def test_same_sid_in_both_generations_is_unknown_without_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER)")
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER)")
                con.execute("INSERT INTO session VALUES ('ses_dupe', '/old', 1)")
                con.execute("INSERT INTO session_v2 VALUES ('ses_dupe', '/new', 2)")
            self.assertEqual(runtime.read_session_observation("ses_dupe", db_path=db)["reason"],
                             "session-generation-ambiguous")
            self.assertEqual(runtime.read_session_observation("ses_dupe", db_path=db,
                                                              generation="v2")["session"]["directory"],
                             "/new")


class OpenCodeProviderEvidenceTest(unittest.TestCase):
    def test_unavailable_credential_scope_cannot_read_or_record_model_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT)")
            tools = str(ROOT / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet.collectors import usage_cache
            with unittest.mock.patch.dict(os.environ, {
                    "OPENCODE_RUNTIME_VERSION": "2.0.32", "OPENCODE_DB": str(db)}, clear=True), \
                 unittest.mock.patch.object(usage_cache, "record_provider") as record:
                self.assertEqual(runtime.model_availability("opencode-go/model")["state"],
                                 "unknown")
                self.assertFalse(runtime.record_model_outcome(
                    "opencode-go/model", error={"status": 401}))
            record.assert_not_called()

    def test_only_typed_expiry_and_entitlement_errors_are_hard(self):
        expired = runtime.classify_usage_rejection(
            401, {"error": {"code": "api_key_expired", "message": "expired"}})
        self.assertEqual(expired["reason"], "credential-expired")
        missing = runtime.classify_usage_rejection(
            403, {"error": {"type": "EntitlementError",
                            "message": "OpenCode Go subscription required."}})
        self.assertEqual(missing["reason"], "subscription-missing")
        self.assertIsNone(runtime.classify_usage_rejection(403, {"message": "cloudflare 1010"}))
        self.assertEqual(runtime.classify_usage_rejection(401, {})["reason"],
                         "authentication-rejected")

    def test_provider_errors_are_redacted_for_terminal_receipts(self):
        result = runtime.safe_provider_error({"type": "provider.auth", "status": 401,
                                              "code": "api_key_expired",
                                              "message": "Bearer secret-token"})
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertEqual(result["status"], 401)

    def test_configured_fallbacks_are_source_owned(self):
        fallbacks = runtime.automatic_model_fallbacks()
        self.assertIn(("openai/gpt-6.1-sol", "runtime-default"), fallbacks)
        self.assertNotIn(("opencode/beam-free", "runtime-default"), fallbacks)


if __name__ == "__main__":
    unittest.main()
