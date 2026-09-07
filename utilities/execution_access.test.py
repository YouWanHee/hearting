#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from execution_access import (
    AccessContext,
    ExecutionAccessError,
    ParentGrant,
    adapter_default_roots,
    assert_within_parent,
    build_grant,
    load_request,
    receipt_fields,
    request_path,
)


class ExecutionAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.worktree = self.root / "worktree"
        self.artifact = self.root / "artifact"
        self.state = self.root / "state" / "dispatch"
        self.agent_home = self.root / "install" / "hearting"
        for path in (self.home, self.worktree, self.artifact, self.state, self.agent_home):
            path.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        }
        self.context = AccessContext.build(
            worktree=self.worktree,
            artifact_root=self.artifact,
            dispatch_state_root=self.state,
            agent_home=self.agent_home,
            environ=self.env,
        )
        self.request_file = self.root / "request.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, **changes: object):
        writable = self.root / "scoped" / "write"
        readable = self.root / "scoped" / "read"
        data = {
            "schema_version": 1,
            "writable_roots": [str(writable)],
            "read_roots": [str(readable)],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any",
            "justification": {str(writable): "build output"},
        }
        data.update(changes)
        self.request_file.write_text(json.dumps(data), encoding="utf-8")
        return load_request(self.request_file, context=self.context)

    def assert_reason(self, expected: str, **changes: object) -> None:
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(**changes)
        self.assertEqual(expected, raised.exception.reason)

    def test_valid_request_normalizes_and_hashes_stably(self) -> None:
        first = self.request(
            writable_roots=[str(self.root / "z"), str(self.root / "a"), str(self.root / "z")],
            read_roots=[],
            justification={},
        )
        second = self.request(
            writable_roots=[str(self.root / "a"), str(self.root / "z")],
            read_roots=[],
            justification={},
        )
        self.assertEqual((self.root / "a", self.root / "z"), first.writable_roots)
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(64, len(first.request_sha256))

    def test_schema_and_fields_are_strict(self) -> None:
        self.assert_reason("execution-access-schema-unsupported:v2", schema_version=2)
        self.assert_reason(
            "execution-access-field-invalid:surprise", surprise=True
        )
        self.assert_reason(
            "execution-access-field-invalid:network.required",
            network={"required": "yes", "reason": "", "hosts": []},
        )
        self.assert_reason(
            "execution-access-field-invalid:enforcement_required",
            enforcement_required="best-effort",
        )
        self.assert_reason(
            "execution-access-field-invalid:network.required",
            writable_roots=["relative/path"],
            read_roots=[],
            justification={},
            network={"required": "yes", "reason": "", "hosts": []},
        )

    def test_request_file_validation_precedes_json(self) -> None:
        missing = self.root / "missing.json"
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(missing, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)
        self.request_file.write_text("{", encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        self.request_file.unlink()
        self.request_file.symlink_to(target)
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)

    def test_request_surface_cli_wins_and_environment_is_fallback(self) -> None:
        cli = self.root / "cli.json"
        inherited = self.root / "inherited.json"
        self.assertEqual(
            cli,
            request_path(
                str(cli),
                {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)},
            ),
        )
        self.assertEqual(
            inherited,
            request_path(None, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)}),
        )
        self.assertIsNone(request_path(None, {}))
        self.assertEqual(
            Path(""),
            request_path("", {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)}),
        )

    def test_invalid_path_forms(self) -> None:
        bad_values = (
            "relative/path",
            "file:///tmp/value",
            "~/value",
            "/tmp/*",
            "/tmp/../etc",
            "/tmp/with,comma",
            "/tmp/with=equals",
            "/tmp/with\tcontrol",
            "/tmp/with\nnewline",
            "/" + "x" * 4097,
        )
        for value in bad_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(writable_roots=[value], read_roots=[], justification={})
                self.assertTrue(
                    raised.exception.reason.startswith("execution-access-path-invalid:")
                )
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(self.root / "roots" / str(index)) for index in range(17)],
                read_roots=[],
                justification={},
            )
        self.assertEqual("execution-access-path-invalid:root-count", raised.exception.reason)

    def test_symlink_escape_and_realpath_broad_root(self) -> None:
        declared = self.root / "declared"
        outside = self.root / "outside"
        declared.mkdir()
        outside.mkdir()
        (declared / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(declared), str(declared / "escape")],
                read_roots=[],
                justification={},
            )
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-symlink-escape:")
        )

        sensitive_link = declared / "sensitive"
        sensitive_link.symlink_to(self.agent_home, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(declared)],
                read_roots=[str(sensitive_link)],
                justification={},
            )
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-symlink-escape:")
        )

        broad_link = self.root / "broad-link"
        broad_link.symlink_to(self.agent_home, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(writable_roots=[str(broad_link)], read_roots=[], justification={})
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-root-too-broad:")
        )

    def test_broad_roots_rejected_and_defaults_absorbed(self) -> None:
        for path in (
            Path("/"),
            self.home,
            Path("/home"),
            Path("/usr"),
            Path("/etc"),
            self.state,
            self.root / "state",
            self.root,
        ):
            with self.subTest(path=path):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(writable_roots=[str(path)], read_roots=[], justification={})
                self.assertTrue(
                    raised.exception.reason.startswith("execution-access-root-too-broad:")
                )

        request = self.request(
            writable_roots=[str(self.worktree), str(self.artifact)],
            read_roots=[],
            justification={},
        )
        grant = build_grant(
            request,
            runtime="codex-exec",
            default_writable_roots=(self.worktree, self.artifact),
            network_available=True,
        )
        self.assertEqual((), grant.additional_writable_roots)
        self.assertEqual((self.artifact, self.worktree), grant.absorbed_writable_roots)
        args = type(
            "Args",
            (),
            {
                "worktree": str(self.worktree),
                "artifact_root": str(self.artifact),
                "report_bundle_root": None,
            },
        )()
        self.assertEqual(
            (self.artifact, self.worktree), adapter_default_roots(args)
        )

    def test_parent_monotonicity_is_fail_closed(self) -> None:
        request = self.request(read_roots=[], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(request, None, is_child=True)
        self.assertEqual(
            "execution-access-exceeds-parent:parent-grant-unknown",
            raised.exception.reason,
        )

        parent = ParentGrant(writable_roots=(self.root / "different",))
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(request, parent, is_child=True)
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-exceeds-parent:")
        )
        self.assertIn("top-level launch", raised.exception.detail)

        parent = ParentGrant(writable_roots=(self.root / "scoped",))
        assert_within_parent(request, parent, is_child=True)

    def test_runtime_grades_network_and_receipt(self) -> None:
        request = self.request(
            read_roots=[],
            network={"required": True, "reason": "fetch source", "hosts": ["EXAMPLE.com:443"]},
            justification={},
        )
        grant = build_grant(
            request,
            runtime="codex-exec",
            default_writable_roots=(),
            network_available=True,
        )
        self.assertEqual("granted-unenforced", grant.network)
        self.assertIn("network-hosts-unenforced", grant.unmet)
        fields = receipt_fields(grant)
        self.assertEqual(
            {
                "execution_access_request",
                "execution_access_roots",
                "execution_access_network",
                "execution_access_enforcement",
                "execution_access_unmet",
            },
            set(fields),
        )
        self.assertEqual("os-sandbox", fields["execution_access_enforcement"])

        claude = build_grant(
            request,
            runtime="claude-cli",
            default_writable_roots=(),
        )
        self.assertEqual("granted-unenforced", claude.network)
        self.assertEqual("tool-permission", claude.file_enforcement)
        self.assertEqual("none", claude.network_enforcement)

    def test_unavailable_enforcement_is_typed(self) -> None:
        network = self.request(
            read_roots=[],
            network={"required": True, "reason": "needed", "hosts": []},
            justification={},
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                network,
                runtime="codex-app-server",
                network_available=False,
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-network-role-gated",
            raised.exception.reason,
        )

        os_required = self.request(
            read_roots=[], enforcement_required="os-sandbox", justification={}
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(os_required, runtime="opencode")
        self.assertEqual(
            "execution-access-enforcement-unavailable:opencode",
            raised.exception.reason,
        )


if __name__ == "__main__":
    unittest.main()
