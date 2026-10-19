import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import usage_cache
from fleet.collectors import zen_go_usage


PROBED_BODY = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-04T10:48:37.338Z"},
        "weekly": {"status": "ok", "percent": 2, "resetsAt": "2026-09-07T00:00:00.338Z"},
        "monthly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-28T04:51:28.338Z"},
    }
}


class OpenCodeV1Test(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        auth_path = str(Path(root.name) / "auth.json")
        version = mock.patch.dict(os.environ, {
            "OPENCODE_RUNTIME_VERSION": "1.19.0", "OPENCODE_AUTH": auth_path})
        version.start()
        self.addCleanup(version.stop)


class ZenGoTokenTest(OpenCodeV1Test):
    def test_token_reads_opencode_go_key_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "api", "key": "sk-go-test"}}, fh)
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertEqual(zen_go_usage._token(), "sk-go-test")

    def test_token_missing_entry_or_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "oauth"}}, fh)
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertIsNone(zen_go_usage._token())
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path",
                                   return_value=os.path.join(tmp, "absent.json")):
                self.assertIsNone(zen_go_usage._token())


class ZenGoFetchTest(OpenCodeV1Test):
    def setUp(self):
        super().setUp()
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None,
                      "scope": zen_go_usage._usage_account_scope()}
        cache_patcher = mock.patch.object(zen_go_usage, "_cache", self.cache)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)
        store_patcher = mock.patch.object(zen_go_usage._runtime(), "credential_store_present",
                                          return_value=False)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    def _fetch_with(self, body):
        opener = mock.MagicMock()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(body).encode()
        opener.return_value = resp
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen", opener):
            return zen_go_usage._fetch(), opener

    def _fetch_body(self, body):
        out, opener = self._fetch_with(body)
        return out

    def test_fetch_sends_explicit_user_agent(self):
        # Cloudflare bans the default Python-urllib UA with 403/1010 (measured
        # 2026-09-04) — the explicit User-Agent is load-bearing, not cosmetic.
        _out, opener = self._fetch_with(PROBED_BODY)
        headers = opener.call_args[0][0].headers
        ua = {k.lower(): v for k, v in headers.items()}.get("user-agent")
        self.assertTrue(ua and ua != "Python-urllib/3.12",
                        "fetch must send an explicit User-Agent")

    def test_fetch_maps_probed_shape(self):
        from datetime import datetime, timezone
        out = self._fetch_body(PROBED_BODY)
        self.assertEqual(out["rl_5h"], 0)
        self.assertEqual(out["rs_5h"],
                         datetime(2026, 9, 4, 10, 48, 37, 338000,
                                  tzinfo=timezone.utc).timestamp())
        labels = [w[0] for w in out["rl_windows"]]
        self.assertEqual(labels, ["5h", "wk", "mo"])
        pcts = {w[0]: w[1] for w in out["rl_windows"]}
        self.assertEqual(pcts, {"5h": 0, "wk": 2, "mo": 1})
        self.assertTrue(all(w[2] is not None for w in out["rl_windows"]))
        # weekly is a Mon 00:00 window — it must NOT claim the rolling 7d slot
        self.assertIsNone(out["rl_7d"])
        self.assertIsNone(out["rs_7d"])
        self.assertEqual(out["rl_ms"], [])

    def test_fetch_empty_or_bad_body_is_none(self):
        self.assertIsNone(self._fetch_body({}))
        self.assertIsNone(self._fetch_body({"usage": {}}))
        self.assertIsNone(self._fetch_body("not-a-dict"))

    def test_fetch_without_key_is_none_and_never_hits_network(self):
        opener = mock.MagicMock()
        with mock.patch.object(zen_go_usage, "_token", return_value=None), \
             mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(zen_go_usage._fetch())
        opener.assert_not_called()

    def test_fetch_transport_failure_is_none(self):
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(zen_go_usage._fetch())

    def test_fetch_auth_rejection_is_error_marker_not_none(self):
        # Only a typed rejection is definitive. A generic 403 remains unknown.
        import urllib.error
        for code in (401, 403):
            with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
                 mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.HTTPError(
                                "url", code, "Forbidden", None, None)):  # noqa: SIM115
                out = zen_go_usage._fetch()
                if code == 401:
                    self.assertEqual(("auth", "authentication-rejected"),
                                     (out["error"], out["reason"]))
                else:
                    self.assertIsNone(out)
        entitlement = {"error": {"type": "EntitlementError",
                                 "message": "OpenCode Go subscription required."}}
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "url", 403, "Forbidden", None,
                            __import__("io").BytesIO(json.dumps(entitlement).encode()))):
            out = zen_go_usage._fetch()
            self.assertEqual(("auth", "subscription-missing"),
                             (out["error"], out["reason"]))
        # other HTTP failures stay transient
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "url", 500, "Server Error", None, None)):  # noqa: SIM115
            self.assertIsNone(zen_go_usage._fetch())

    def test_confirmed_auth_error_surfaces_immediately(self):
        now0 = time.time()
        good = {"rl_5h": 3, "rl_7d": None, "rl_ms": [], "rs_5h": 1.0, "rs_7d": None,
                "rl_windows": [["5h", 3, 1.0]]}
        auth = {"error": "auth", "detail": 401, "reason": "credential-expired"}
        with mock.patch.object(zen_go_usage, "_fetch", return_value=good):
            self.assertEqual(zen_go_usage.account_usage(), good)
        with mock.patch.object(zen_go_usage, "_fetch", return_value=auth):
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            result = zen_go_usage.account_usage()
            self.assertEqual(result["error"], "auth")
            self.assertEqual(result["reason"], "credential-expired")
            self.assertEqual(result["rl_windows"], good["rl_windows"])


class ZenGoV2CredentialPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "opencode.db"
        self.data = root / "opencode-data"
        self.data.mkdir()
        self.legacy_auth = self.data / "auth.json"
        self.usage_state = root / "usage-state"
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, "
                "value TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")
        environment = mock.patch.dict(os.environ, {
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
            "OPENCODE_DB": str(self.db),
            "OPENCODE_DATA_DIR": str(self.data),
            "OPENCODE_AUTH": str(self.legacy_auth),
            "FLEET_USAGE_STATE_DIR": str(self.usage_state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        cache_patcher = mock.patch.object(
            zen_go_usage, "_cache", {"ts": 0.0, "ok_ts": 0.0, "data": None, "scope": None})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    def add_credential(self, identifier, integration, value, active, created):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identifier, integration, identifier, json.dumps(value), active, created, created))

    def test_active_default_oauth_beats_inactive_personal_and_expired_api_key(self):
        runtime = zen_go_usage._runtime()
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "inactive-personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "active-default-token"}, 1, 2)
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        api_key = zen_go_usage._native_credential("opencode-go")
        usage_cache.record_provider("opencode", "opencode-go", "*", api_key["scope"],
                                    "unavailable", reason="credential-expired")
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            result = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("active-default-token")])
        self.assertEqual(result["rl_5h"], 0)
        self.assertEqual(result["provider_availability"]["opencode-go"]["state"], "usable")
        selected_scope = runtime.provider_account_scope("opencode-go")
        self.assertNotEqual(selected_scope, api_key["scope"])
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "usable")

    def test_active_oauth_rejection_does_not_fall_back_to_api_key_or_legacy(self):
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "inactive-personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "expired-active-token"}, 1, 2)
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        rejection = (401, json.dumps({"error": {"code": "api_key_expired"}}).encode())
        with mock.patch.object(zen_go_usage, "_request_usage", return_value=(None, rejection)) as request:
            result = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("expired-active-token")])
        self.assertEqual((result["error"], result["reason"]), ("auth", "credential-expired"))

    def test_present_malformed_oauth_is_unknown_and_does_not_fall_back(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("broken-oauth", "opencode", "Default", "not-json", 1, 2, 2))
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        with mock.patch.object(zen_go_usage, "_request_usage") as request:
            result = zen_go_usage._fetch_v2()
        request.assert_not_called()
        self.assertEqual((result["error"], result["reason"]), ("unknown", "credential-invalid"))

    def test_absent_oauth_uses_native_key_then_legacy_file(self):
        self.add_credential("go-key", "opencode-go", {
            "type": "key", "key": "native-go-key"}, 1, 1)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            self.assertIn("rl_5h", zen_go_usage._fetch_v2())
        self.assertEqual(request.call_args_list, [mock.call("native-go-key")])
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM credential")
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            self.assertIn("rl_5h", zen_go_usage._fetch_v2())
        self.assertEqual(request.call_args_list, [mock.call("legacy-go-key")])

    def test_active_account_change_does_not_inherit_old_entitlement_denial(self):
        runtime = zen_go_usage._runtime()
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "default-token"}, 1, 2)
        old_scope = runtime.provider_account_scope("opencode-go")
        usage_cache.record_provider("opencode", "opencode-go", "*", old_scope,
                                    "unavailable", reason="subscription-missing")
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "unavailable")
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE credential SET active=1 WHERE id='personal'")
            connection.execute("UPDATE credential SET active=0 WHERE id='default'")
        new_scope = runtime.provider_account_scope("opencode-go")
        self.assertNotEqual(old_scope, new_scope)
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "unknown")
        zen_go_usage._cache.update(ts=time.time(), ok_ts=time.time(), data={
            "rl_5h": 1, "provider_availability": {"opencode-go": {
                "state": "unavailable", "reason": "subscription-missing",
                "account_scope": old_scope}}}, scope=old_scope)
        recovered = {"rl_5h": 0, "provider_availability": {"opencode-go": {
            "state": "usable", "reason": None, "account_scope": new_scope}}}
        with mock.patch.object(zen_go_usage, "_fetch_v2", return_value=recovered) as fetch:
            self.assertEqual(zen_go_usage.account_usage(), recovered)
        fetch.assert_called_once()


class ZenGoAccountUsageTest(OpenCodeV1Test):
    def setUp(self):
        super().setUp()
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None,
                      "scope": zen_go_usage._usage_account_scope()}
        self.saved = dict(zen_go_usage._cache)
        zen_go_usage._cache.clear()
        zen_go_usage._cache.update(self.cache)
        self.addCleanup(lambda: (zen_go_usage._cache.clear(),
                                 zen_go_usage._cache.update(self.saved)))

    def test_failure_keeps_stale_then_drops_after_stale_max(self):
        now0 = time.time()
        good = {"rl_5h": 3, "rl_7d": None, "rl_ms": [], "rs_5h": 1.0, "rs_7d": None,
                "rl_windows": [["5h", 3, 1.0]]}
        with mock.patch.object(zen_go_usage, "_fetch", return_value=good):
            self.assertEqual(zen_go_usage.account_usage(), good)
        with mock.patch.object(zen_go_usage, "_fetch", return_value=None) as fetch:
            still = zen_go_usage.account_usage()
            self.assertEqual(still, good)
            fetch.assert_not_called()          # inside the TTL the endpoint is not re-hit
            # beyond the TTL a failure keeps the last-good payload...
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            self.assertEqual(zen_go_usage.account_usage(), good)
            # ...until the staleness ceiling (keyed on the last-good anchor) passes.
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            zen_go_usage._cache["ok_ts"] = now0 - zen_go_usage._STALE_MAX - 1
            self.assertIsNone(zen_go_usage.account_usage())


class ZenGoCacheIntegrationTest(unittest.TestCase):
    """usage_cache wires "opencode" like claude/codex: fetcher registered, refresh works,
    fresh window 180s."""

    def setUp(self):
        import tempfile as _tf
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLEET_USAGE_STATE_DIR"] = self.tmp.name
        self.old_fetchers = dict(usage_cache.FETCHERS)
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        usage_cache.FETCHERS.clear()
        usage_cache.FETCHERS.update(self.old_fetchers)
        self.tmp.cleanup()

    def test_request_refresh_opencode_writes_cache(self):
        calls = []
        usage_cache.FETCHERS["opencode"] = lambda: (
            calls.append(1) or {"rl_5h": 0, "rl_windows": [["5h", 0, 1.0]]})
        self.assertTrue(usage_cache.request_refresh("opencode"))
        thread = usage_cache._THREADS.get("opencode")
        self.assertIsNotNone(thread)
        thread.join(timeout=5)
        self.assertEqual(usage_cache.read("opencode")["payload"]["rl_5h"], 0)

    def test_read_freshness_window_opencode_is_180s(self):
        usage_cache._write("opencode", {"rl_5h": 1}, 1000.0, 1000.0)
        self.assertEqual(usage_cache.read("opencode", now=1000 + 180)["freshness"], "fresh")
        self.assertEqual(usage_cache.read("opencode", now=1000 + 181)["freshness"], "stale")


class ZenGoRenderHeaderTest(unittest.TestCase):
    """The auth-reject state must be visible in the usage header: 'key rejected'
    instead of a silently blank gauge (hearting-46 v2.110.0 field report)."""

    @staticmethod
    def _rows_text(rows):
        return "\n".join("".join(t for t, _k in row) for row in rows)

    def test_provider_statuses_render_expiry_entitlement_and_unknown_separately(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        snapshots = {"opencode": {
            "payload": {"error": "auth", "detail": 401, "reason": "credential-expired"},
            "providers": [
                {"provider": "opencode-go", "model": "*", "state": "unavailable",
                 "reason": "credential-expired", "observed_at": 1000},
                {"provider": "opencode-go-entitlement", "model": "*", "state": "unavailable",
                 "reason": "subscription-missing", "observed_at": 1000},
                {"provider": "opencode", "model": "beam-free", "state": "usable",
                 "reason": None, "observed_at": 1000},
            ], "freshness": "fresh", "observed_at": 1000}}
        rows = render._usage_header_rows([oc], layout="wide", usage_snapshots=snapshots)
        text = self._rows_text(rows)
        self.assertIn("Go credential expired", text)
        self.assertIn("Go subscription missing", text)
        self.assertIn("Zen valid", text)
        self.assertIn("OpenAI unknown", text)

    def test_session_error_marker_alone_renders_rejected_message(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        oc._usage_error = "auth"
        rows = render._usage_header_rows([oc], layout="wide")
        self.assertIn("Go key rejected", self._rows_text(rows))

    def test_healthy_snapshot_still_renders_gauges(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        snapshots = {"opencode": {
            "payload": {"rl_5h": 58, "rl_windows": [["5h", 58, 1.0], ["wk", 25, 2.0]]},
            "freshness": "fresh", "observed_at": 1000}}
        rows = render._usage_header_rows([oc], layout="wide", usage_snapshots=snapshots)
        text = self._rows_text(rows)
        self.assertNotIn("usage api —", text)
        self.assertIn("58%", text)
        self.assertIn("25%", text)


if __name__ == "__main__":
    unittest.main()
