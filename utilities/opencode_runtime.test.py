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
        self.assertNotIn("--standalone", v2)
        self.assertNotIn("--dir", v2)
        self.assertNotIn("--variant", v2)
        self.assertEqual(v2[v2.index("--model") + 1], "opencode-go/model#high")
        self.assertEqual(v2[v2.index("--session") + 1], "ses_exact")
        self.assertEqual(v2[v2.index("--title") + 1], "hearting-worker-att-x")
        private_v2 = runtime.run_argv(binary="opencode", model="fixture/model",
                                      standalone=True, version=(2, 0, 32))
        self.assertIn("--standalone", private_v2)
        self.assertEqual(private_v2[private_v2.index("--model") + 1], "fixture/model")
        self.assertNotIn("--standalone", runtime.run_argv(
            binary="opencode", model="fixture/model", standalone=True, version=(1, 19, 0)))

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


class OpenCodePermissionCodecTest(unittest.TestCase):
    @staticmethod
    def reference_effect(rows, action, resource):
        """Independent V2 last-match oracle for the simple documented wildcard grammar."""
        import fnmatch
        effect = None
        for row in rows:
            if fnmatch.fnmatchcase(action, row["action"]) and fnmatch.fnmatchcase(
                    resource, row["resource"]):
                effect = row["effect"]
        return effect

    def check_projection(self, rows, actions, resources):
        config = {"permissions": rows}
        projected = runtime.opencode_v2_to_legacy_config(config)
        for action in actions:
            for resource in resources:
                expected = self.reference_effect(rows, action, resource)
                actual = None
                for pattern, effect in runtime.opencode_permission_rules_for_action(
                        projected["permission"], action) or ():
                    import fnmatch
                    if fnmatch.fnmatchcase(resource, pattern):
                        actual = effect
                self.assertEqual(actual, expected, (action, resource, rows, projected))

    def test_scalar_object_and_action_aliases_share_the_legacy_comparison_view(self):
        self.assertEqual(runtime.opencode_permission_ordered_view("allow"),
                         runtime.opencode_permission_ordered_view({"*": "allow"}))
        self.assertEqual(runtime.opencode_permission_ordered_view({"bash": "allow"}),
                         runtime.opencode_permission_ordered_view(
                             {"bash": {"*": "allow"}}))
        rows = runtime.opencode_legacy_to_v2_config({"permission": {
            "write": {"/approved/**": "allow"},
            "patch": {"/approved/**": "deny"},
            "edit": {"*": "deny"},
            "bash": "allow", "task": {"agent/*": "deny"},
        }})["permissions"]
        self.assertEqual([row["action"] for row in rows],
                         ["edit", "edit", "edit", "shell", "subagent"])
        self.assertEqual(self.reference_effect(rows, "edit", "/approved/file"), "deny")
        self.assertEqual(self.reference_effect(rows, "shell", "git status"), "allow")
        self.assertEqual(self.reference_effect(rows, "subagent", "agent/build"), "deny")

    def test_legacy_bash_command_shorthand_overlap_stays_order_sensitive(self):
        first = runtime.opencode_permission_ordered_view({
            "bash *": {"*": "deny"}, "bash": {"*": "allow"},
        })
        second = runtime.opencode_permission_ordered_view({
            "bash": {"*": "allow"}, "bash *": {"*": "deny"},
        })
        self.assertNotEqual(first, second)

    def test_redundant_equal_effect_and_shadowed_rules_compare_equal(self):
        deny = runtime.opencode_permission_ordered_view({"edit": "deny"})
        for rules in (
                {"*": "deny", "/root/**": "deny"},
                {"/root/**": "deny", "*": "deny"},
                {"/root/**": "allow", "*": "deny"}):
            with self.subTest(rules=rules):
                self.assertEqual(runtime.opencode_permission_ordered_view({"edit": rules}),
                                 deny)
        self.assertEqual(runtime.opencode_permission_ordered_view({
            "*": "deny", "edit": {"/root/**": "deny"},
        }), runtime.opencode_permission_ordered_view({"*": "deny"}))
        self.assertEqual(runtime.opencode_permission_ordered_view({
            "edit": {"/root/**": "allow"}, "*": "deny",
        }), runtime.opencode_permission_ordered_view({"*": "deny"}))
        overlapping = {"/root/*": "deny", "*/file": "deny"}
        self.assertEqual(runtime.opencode_permission_ordered_view({"edit": overlapping}),
                         runtime.opencode_permission_ordered_view({
                             "edit": dict(reversed(list(overlapping.items())))}))

    def test_conflicting_overlaps_without_containment_stay_order_sensitive(self):
        rules = {"/root/*": "deny", "*/file": "allow"}
        first, second = {"edit": rules}, {"edit": dict(reversed(list(rules.items())))}
        self.assertNotEqual(runtime.opencode_permission_ordered_view(first),
                            runtime.opencode_permission_ordered_view(second))
        self.assertEqual(runtime.opencode_permission_effect(first, "edit", "/root/file"), "allow")
        self.assertEqual(runtime.opencode_permission_effect(second, "edit", "/root/file"), "deny")

    def test_pruning_keeps_a_deny_after_an_intervening_allow(self):
        restricted = {"edit": {"*": "deny", "/root/**": "allow",
                               "/root/private/**": "deny"}}
        widened = {"edit": {"*": "deny", "/root/**": "allow"}}
        self.assertNotEqual(runtime.opencode_permission_ordered_view(restricted),
                            runtime.opencode_permission_ordered_view(widened))
        self.assertEqual(runtime.opencode_permission_effect(
            restricted, "edit", "/root/private/file"), "deny")
        self.assertEqual(runtime.opencode_permission_effect(
            widened, "edit", "/root/private/file"), "allow")
        same_allow = {"edit": {"*": "deny", "/root/**": "allow",
                               "/root/private/**": "allow"}}
        self.assertEqual(runtime.opencode_permission_ordered_view(same_allow),
                         runtime.opencode_permission_ordered_view(widened))

    def test_comparison_pruning_never_merges_different_last_match_results(self):
        import itertools
        rules = (("*", "deny"), ("/root/*", "allow"),
                 ("/root/private/*", "deny"), ("/elsewhere", "deny"))
        observations = {}
        for ordered in itertools.permutations(rules):
            rows = [{"action": "edit", "resource": resource, "effect": effect}
                    for resource, effect in ordered]
            expected = tuple(self.reference_effect(rows, "edit", resource) for resource in
                             ("", "/root/file", "/root/private/file", "/elsewhere", "/other"))
            view = runtime.opencode_permission_ordered_view({"edit": dict(ordered)})
            key = json.dumps(view, sort_keys=True)
            if key in observations:
                self.assertEqual(observations[key], expected, ordered)
            observations[key] = expected

    def test_interleaved_action_wildcards_and_repeated_resources_keep_last_match(self):
        rows = [
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "*", "resource": "/approved/**", "effect": "allow"},
            {"action": "e*", "resource": "/approved/**", "effect": "deny"},
            {"action": "edit", "resource": "*", "effect": "allow"},
        ]
        self.check_projection(rows, ("edit", "read", "execute"),
                              ("/approved/file", "/other/file", "*"))
        repeated = [
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "edit", "resource": "/approved/**", "effect": "allow"},
            {"action": "edit", "resource": "*", "effect": "deny"},
        ]
        projected = runtime.opencode_v2_to_legacy_config({"permissions": repeated})
        self.assertEqual(runtime.opencode_permission_rules_for_action(
            projected["permission"], "edit"),
            [("/approved/**", "allow"), ("*", "deny")])
        self.check_projection(repeated, ("edit",), ("/approved/file", "/other/file"))

    def test_resource_overlaps_selected_agent_and_native_v2_shape(self):
        source = {
            "theme": "system",
            "permissions": [
                {"action": "edit", "resource": "*", "effect": "deny"},
                {"action": "edit", "resource": "/approved/**", "effect": "allow"},
                {"action": "edit", "resource": "/approved/private/**", "effect": "deny"},
            ],
            "agents": {"build": {"description": "kept", "permissions": [
                {"action": "edit", "resource": "*", "effect": "allow"},
                {"action": "edit", "resource": "/approved/private/**", "effect": "deny"},
            ]}, "research": {"mode": "subagent", "permissions": []}},
        }
        original = json.loads(json.dumps(source))
        legacy = runtime.opencode_v2_to_legacy_config(source)
        self.assertEqual(source, original)
        self.assertEqual(legacy["agent"]["build"]["description"], "kept")
        self.assertEqual(legacy["agent"]["research"]["mode"], "subagent")
        self.assertEqual(runtime.opencode_permission_ordered_view(
            legacy["permission"]), runtime.opencode_permission_ordered_view(
                runtime.opencode_permission_from_config(source)))
        encoded = runtime.opencode_legacy_to_v2_config(legacy, original_v2=source)
        self.assertIn("permissions", encoded)
        self.assertIn("agents", encoded)
        self.assertNotIn("permission", encoded)
        self.assertNotIn("agent", encoded)
        self.assertEqual(encoded["theme"], "system")
        self.assertEqual(runtime.opencode_permission_ordered_view(
            runtime.opencode_permission_from_config(encoded)),
            runtime.opencode_permission_ordered_view(legacy["permission"]))
        self.check_projection(source["permissions"], ("edit",),
                              ("/approved/file", "/approved/private/file", "/other"))

    def test_malformed_or_empty_v2_permissions_do_not_become_allow_rows(self):
        for malformed in (None, {}, [{"action": "edit", "resource": "*", "effect": True}],
                          [{"action": "edit", "resource": "*", "effect": "allow",
                            "extra": "discarded"}]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                runtime.opencode_v2_to_legacy_config({"permissions": malformed})
        for missing in (None, {}):
            self.assertEqual(runtime.opencode_permission_ordered_view(missing), [])
        with self.assertRaises(ValueError):
            runtime.opencode_permission_ordered_view([])

    def test_v2_action_pattern_that_collides_with_a_legacy_alias_fails_clearly(self):
        config = {"permissions": [
            {"action": "b*", "resource": "*", "effect": "allow"},
        ]}
        with self.assertRaisesRegex(ValueError, "conflicts with the V1 'bash' alias"):
            runtime.opencode_v2_to_legacy_config(config)


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

    def test_v2_shell_cursor_pages_all_row_types_and_refreshes_old_pending_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            session_id = "ses_shellpagefixture"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                            "time_created INTEGER, time_updated INTEGER)")
                con.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                            "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
                con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                            (session_id, tmp, 1000, 1000))
                for seq in range(1, 71):
                    if seq == 1:
                        kind = "shell"
                        payload = {"metadata": {"background": True},
                                   "time": {"created": 1792456543124},
                                   "shellID": "sh_pending_old_window", "command": "sleep 20",
                                   "status": "running"}
                    else:
                        kind = "user" if seq % 2 else "assistant"
                        payload = {"text": "ordinary non-candidate row"}
                    con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                                ("msg-%d" % seq, session_id, kind, seq, seq, seq,
                                 json.dumps(payload)))

            first = runtime.read_session_observation(
                session_id, db_path=db, generation="v2", shell_after_seq=0)
            self.assertEqual(first["shell_cursor"]["next_seq"], runtime.MAX_SHELL_TASK_PAGE)
            self.assertFalse(first["shell_cursor"]["caught_up"])
            self.assertEqual(first["shell_cursor"]["mutable_seqs"], [1])
            self.assertEqual(first["shell_tasks"][0]["native_record_kind"],
                             "session-message-shell")

            completed = {"metadata": {"background": True},
                         "time": {"created": 1792456543124, "completed": 1792456547139},
                         "shellID": "sh_pending_old_window", "command": "sleep 20",
                         "status": "exited", "exit": 3,
                         "output": {"output": "private output omitted", "cursor": 17,
                                    "size": 17, "truncated": False}}
            with sqlite3.connect(db) as con:
                con.execute("UPDATE session_message SET data=?, time_updated=? "
                            "WHERE session_id=? AND seq=1",
                            (json.dumps(completed), 1792456547139, session_id))

            second = runtime.read_session_observation(
                session_id, db_path=db, generation="v2",
                shell_after_seq=first["shell_cursor"]["next_seq"],
                shell_refresh_seqs=first["shell_cursor"]["mutable_seqs"])
            cursor = second["shell_cursor"]
            self.assertEqual(cursor["next_seq"], 70)
            self.assertTrue(cursor["caught_up"])
            self.assertEqual(cursor["refreshed_seqs"], [1])
            task = second["shell_tasks"][0]
            self.assertEqual(task["native_status"], "exited")
            self.assertEqual(task["exit_code"], 3)
            self.assertEqual(task["completed_at_ms"], 1792456547139)
            self.assertEqual(task["native_output_cursor"], 17)
            self.assertEqual(task["native_output_size"], 17)
            self.assertFalse(task["native_output_truncated"])
            self.assertNotIn("private output omitted", repr(task))

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
