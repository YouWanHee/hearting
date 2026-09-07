#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import execution_access_diagnose as diagnose_module


class ExecutionAccessDiagnoseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "home" / ".codex" / ".harness"
        self.private.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def classification(self, evidence: dict[str, object]) -> str:
        return str(
            diagnose_module.diagnose(
                evidence, launcher_state_roots=(self.private,)
            )["classification"]
        )

    def test_seven_classifications(self) -> None:
        fixtures = {
            "launcher-runtime-state-unwritable": {
                "errno": "EROFS",
                "phase": "write",
                "path": str(self.private / "codex-launcher.lock"),
            },
            "mount-read-only": {
                "errno": "EROFS",
                "phase": "write",
                "path": str(self.root / "mount" / "file"),
            },
            "unix-permission-denied": {
                "errno": "EACCES",
                "phase": "write",
                "mount_writable": True,
            },
            "remote-auth-rejected": {
                "http_status": 403,
                "phase": "connect",
            },
            "dns-resolution-failed": {"errno": "EAI_NONAME", "phase": "dns"},
            "tcp-connect-blocked": {"errno": "ETIMEDOUT", "phase": "connect"},
            "sandbox-network-disabled": {
                "network_all_hosts_failed": True,
                "sandbox_network_enabled": False,
                "phase": "network",
            },
        }
        for expected, evidence in fixtures.items():
            with self.subTest(expected=expected):
                result = diagnose_module.diagnose(
                    evidence, launcher_state_roots=(self.private,)
                )
                self.assertEqual(expected, result["classification"])
                self.assertTrue(result["one_change"])
                self.assertNotIn("stderr", json.dumps(result).lower())

    def test_launcher_state_wins_over_erofs(self) -> None:
        self.assertEqual(
            "launcher-runtime-state-unwritable",
            self.classification(
                {
                    "errno": "EROFS",
                    "phase": "write",
                    "path": str(self.private / "launcher.lock"),
                }
            ),
        )

    def test_launcher_state_path_without_write_failure_is_insufficient(self) -> None:
        with self.assertRaises(diagnose_module.DiagnosisError):
            diagnose_module.diagnose(
                {
                    "phase": "read",
                    "path": str(self.private / "launcher.lock"),
                    "exit_code": 0,
                },
                launcher_state_roots=(self.private,),
            )

    def test_request_absence_alone_does_not_mean_network_off(self) -> None:
        with self.assertRaises(diagnose_module.DiagnosisError):
            diagnose_module.diagnose(
                {
                    "network_all_hosts_failed": False,
                    "sandbox_network_enabled": None,
                    "execution_access_network": "not-requested",
                }
            )

    def test_default_cli_creates_no_file_or_socket_and_redacts_raw_error(self) -> None:
        evidence = self.root / "evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "http_status": 401,
                    "phase": "connect",
                    "stderr_excerpt": "Authorization: Bearer secret-token password=hunter2",
                }
            ),
            encoding="utf-8",
        )
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        stdout = io.StringIO()
        with mock.patch("os.open", side_effect=AssertionError("write probe called")), mock.patch(
            "socket.create_connection", side_effect=AssertionError("network probe called")
        ), mock.patch("sys.stdout", stdout):
            rc = diagnose_module.main(["--evidence-file", str(evidence)])
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(0, rc)
        self.assertEqual(before, after)
        rendered = stdout.getvalue()
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertEqual("remote-auth-rejected", json.loads(rendered)["classification"])

    def request_file(self, writable: Path, host: str = "example.com:443") -> Path:
        request = self.root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "writable_roots": [str(writable)],
                    "read_roots": [],
                    "network": {"required": True, "reason": "probe", "hosts": [host]},
                    "enforcement_required": "any",
                    "justification": {},
                }
            ),
            encoding="utf-8",
        )
        return request

    def context_args(self) -> list[str]:
        worktree = self.root / "worktree"
        artifact = self.root / "artifact"
        state = self.root / "state" / "dispatch"
        agent_home = self.root / "install" / "hearting"
        for path in (worktree, artifact, state, agent_home):
            path.mkdir(parents=True, exist_ok=True)
        return [
            "--worktree",
            str(worktree),
            "--artifact-root",
            str(artifact),
            "--dispatch-state-root",
            str(state),
            "--agent-home",
            str(agent_home),
        ]

    def test_probe_requires_opt_in_and_exact_declared_target(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        request = self.request_file(writable)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = diagnose_module.main(
                ["--request-file", str(request), "--probe-write", str(writable), *self.context_args()]
            )
        self.assertEqual(64, rc)
        self.assertIn("probe-opt-in-required", stderr.getvalue())

        outside = self.root / "outside"
        outside.mkdir()
        with mock.patch("sys.stderr", io.StringIO()):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(outside),
                    *self.context_args(),
                ]
            )
        self.assertEqual(64, rc)
        self.assertEqual([], list(outside.iterdir()))

    def test_write_probe_cleans_only_its_owned_file(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        keep = writable / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        request = self.request_file(writable)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(writable),
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)
        self.assertEqual("keep", keep.read_text(encoding="utf-8"))
        self.assertEqual([keep], list(writable.iterdir()))
        self.assertEqual("probe-succeeded", json.loads(stdout.getvalue())["classification"])

        descendant = writable / "child"
        descendant.mkdir()
        with mock.patch("sys.stdout", io.StringIO()):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(descendant),
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)
        self.assertEqual([], list(descendant.iterdir()))

    def test_connect_probe_is_bounded_and_exact(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        request = self.request_file(writable)
        fake_socket = mock.MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        with mock.patch("socket.create_connection", return_value=fake_socket) as connect, mock.patch(
            "sys.stdout", io.StringIO()
        ):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-connect",
                    "example.com:443",
                    "--probe-timeout",
                    "2",
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)
        connect.assert_called_once_with(("example.com", 443), timeout=2.0)


if __name__ == "__main__":
    unittest.main()
