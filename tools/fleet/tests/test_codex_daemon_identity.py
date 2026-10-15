"""Daemon-loaded Codex TUI identity is tied to codex_tui's listening PID."""
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = TOOLS.parent / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

from fleet.collectors import codex, procscan  # noqa: E402

PID = 43101
ROOT = "01a0fa57-7f0e-7eb0-bffe-1d56df94e90c"
SECOND = "01a0fa57-7f0e-7eb0-bffe-1d56df94e91c"
CHILD = "01a0fa57-7f0e-7eb0-bffe-1d56df94e92c"


class FakeDaemonWebSocket:
    def __init__(self, threads):
        self.threads = threads
        self.calls = []
        self.closed = False

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        thread_id = params["threadId"]
        if method == "mcpServerStatus/list":
            port, _source = self.threads[thread_id]
            data = ([{"name": "codex_tui", "httpOrigin": f"http://127.0.0.1:{port}"}]
                    if port else [])
            return {"data": data}
        if method == "thread/read":
            _port, source = self.threads[thread_id]
            return {"thread": {"threadSource": source}}
        raise AssertionError(f"unexpected daemon method: {method}")

    def close(self):
        self.closed = True


class CodexDaemonIdentityTest(unittest.TestCase):
    def setUp(self):
        codex._DAEMON_ORIGINS.clear()
        self.threads = {ROOT: (41301, "user"), CHILD: (41301, "subagent")}
        self.client = FakeDaemonWebSocket(self.threads)
        self._patches = [
            mock.patch.object(codex, "_control_socket", return_value=Path("/tmp/fake-codex.sock")),
            mock.patch("codex_queue_delivery._connect", return_value=self.client),
            mock.patch.object(codex, "_tcp_listener_owners", return_value={PID}),
            mock.patch.object(codex, "_live_tui_start", return_value="778899"),
            mock.patch.object(procscan, "read_proc_start", return_value="778899"),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_loaded_root_thread_uses_origin_listener_and_excludes_subagents(self):
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        methods = [method for method, _params in self.client.calls]
        self.assertEqual(methods, [
            "thread/loaded/list", "mcpServerStatus/list", "thread/read",
            "mcpServerStatus/list", "thread/read",
        ])
        self.assertTrue(self.client.closed)

    def test_public_process_identity_uses_the_daemon_loaded_root(self):
        env = {"CODEX_HOME": "/fake"}
        with mock.patch.object(codex.os, "readlink", return_value="/fixture/work"), \
                mock.patch.object(procscan, "read_environ", return_value=env), \
                mock.patch.object(procscan, "codex_effective_cwd", return_value="/fixture/work"), \
                mock.patch.object(procscan, "_comm_of", return_value="codex"), \
                mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
                mock.patch.object(procscan, "invocation_mode", return_value="session"), \
                mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
                mock.patch.object(codex, "_proc_rollout", return_value=None):
            self.assertEqual(codex.session_id_of_process(PID), ROOT)

    def test_zero_or_multiple_root_threads_fail_closed(self):
        self.threads[ROOT] = (None, "user")
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))

        codex._DAEMON_ORIGINS.clear()
        self.threads[ROOT] = (41301, "user")
        self.threads[SECOND] = (41301, "user")
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))

    def test_thread_to_process_can_use_the_callers_exact_thread_during_transition(self):
        self.threads[SECOND] = (41301, "user")
        self.assertEqual(codex.process_for_thread(SECOND, {"CODEX_HOME": "/fake"}),
                         (PID, "778899"))

    def test_loaded_thread_origins_are_cached_until_the_daemon_unloads_them(self):
        first = codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        calls_before = len(self.client.calls)
        again = codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        self.assertEqual(first, again)
        self.assertEqual(len(self.client.calls) - calls_before, 1)


if __name__ == "__main__":
    unittest.main()
