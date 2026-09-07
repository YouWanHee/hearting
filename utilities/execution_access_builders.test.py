#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from execution_access import (
    AccessContext,
    ExecutionAccessError,
    ParentGrant,
    bind_request,
    build_grant,
    load_request,
    receipt_fragment,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = "83e61ec7"


def load_path(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_base(name: str, relative: str):
    source = subprocess.check_output(
        ["git", "show", f"{BASE}:{relative}"], cwd=ROOT, text=True
    )
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / relative)
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


class ExecutionAccessBuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.codex = load_path("fixture_codex_current", "adapters/codex/bin/dispatch-headless.py")
        cls.claude = load_path("fixture_claude_current", "adapters/claude/bin/dispatch-headless.py")
        cls.opencode = load_path("fixture_opencode_current", "adapters/opencode/bin/dispatch-headless.py")
        cls.codex_base = load_base("fixture_codex_base", "adapters/codex/bin/dispatch-headless.py")
        cls.claude_base = load_base("fixture_claude_base", "adapters/claude/bin/dispatch-headless.py")
        cls.opencode_base = load_base("fixture_opencode_base", "adapters/opencode/bin/dispatch-headless.py")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.worktree = self.root / "worktree"
        self.artifact = self.root / "artifact"
        self.state = self.root / "state" / "dispatch"
        self.agent_home = self.root / "install" / "hearting"
        self.scoped = self.root / "scoped" / "output"
        for path in (
            self.home,
            self.worktree,
            self.artifact,
            self.state,
            self.agent_home,
            self.scoped,
        ):
            path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        self.context = AccessContext.build(
            worktree=self.worktree,
            artifact_root=self.artifact,
            dispatch_state_root=self.state,
            agent_home=self.agent_home,
            environ=self.env,
        )
        self.request_file = self.root / "request.json"
        self.write_request()
        self.request = load_request(self.request_file, context=self.context)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_request(self, **changes: object) -> None:
        data = {
            "schema_version": 1,
            "writable_roots": [str(self.scoped)],
            "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any",
            "justification": {str(self.scoped): "bounded output"},
        }
        data.update(changes)
        self.request_file.write_text(json.dumps(data), encoding="utf-8")

    def codex_args(self, delivery: str, grant=None) -> argparse.Namespace:
        return argparse.Namespace(
            resolved_completion_delivery=delivery,
            worktree=str(self.worktree),
            jobs_path=self.state / "jobs.log",
            attempt_id="att-fixture",
            command_attempt_id=None,
            artifact_root=str(self.artifact),
            report_bundle_root=None,
            owner_route_binding=None,
            max_continuations=None,
            nested_headless_network=False,
            dispatch_depth=1,
            route_id=None,
            agent_home=str(self.agent_home),
            worker_type="stage",
            write_scope=None,
            sandbox="workspace-write",
            launch_lifecycle="detached",
            parent_harness="codex",
            parent_transport="headless",
            parent_sandbox="workspace-write",
            resolved_model_settings={"source": "inherit"},
            approval="never",
            execution_access_grant=grant,
        )

    def claude_args(self, delivery: str, grant=None) -> argparse.Namespace:
        return argparse.Namespace(
            resolved_completion_delivery=delivery,
            worktree=str(self.worktree),
            jobs_path=self.state / "jobs.log",
            attempt_id="att-fixture",
            artifact_root=str(self.artifact),
            report_bundle_root=None,
            owner_route_binding=None,
            max_continuations=None,
            resolved_model_settings={"source": "inherit"},
            resolved_permission_posture={
                "mode": "allowlist",
                "mode_flag": "acceptEdits",
                "reason": "fixture",
                "inherited_default_mode": "default",
                "allowed_tools": (),
            },
            execution_access_grant=grant,
        )

    def test_no_request_builders_are_byte_identical_to_base(self) -> None:
        prompt = self.root / "prompt.txt"
        log = self.root / "log.jsonl"
        for delivery in ("one-shot", "app-server-supervised"):
            current = self.codex.shell_command(self.codex_args(delivery), prompt, log)
            base = self.codex_base.shell_command(self.codex_args(delivery), prompt, log)
            self.assertEqual(base, current)
        for delivery in ("one-shot", "session-resume-supervised"):
            current = self.claude.shell_command(self.claude_args(delivery), prompt, log)
            base = self.claude_base.shell_command(self.claude_args(delivery), prompt, log)
            self.assertEqual(base, current)
        self.assertEqual(
            self.opencode_base.scoped_external_directory_config(str(self.artifact)),
            self.opencode.scoped_external_directory_config(str(self.artifact)),
        )

    def test_codex_exec_and_app_server_use_distinct_real_flags(self) -> None:
        grant = build_grant(self.request, runtime="codex-exec")
        prompt = self.root / "prompt.txt"
        log = self.root / "log.jsonl"
        command = self.codex.shell_command(self.codex_args("one-shot", grant), prompt, log)
        self.assertIn(f"--add-dir {self.scoped}", command)
        self.assertNotIn("--writable-root", command)

        grant = build_grant(self.request, runtime="codex-app-server")
        command = self.codex.shell_command(
            self.codex_args("app-server-supervised", grant), prompt, log
        )
        self.assertIn(f"--writable-root {self.scoped}", command)
        self.assertNotIn("--add-dir", command)

    def test_codex_network_receipt_matches_applied_boolean_flag(self) -> None:
        self.write_request(
            network={"required": True, "reason": "fetch", "hosts": []}
        )
        request = load_request(self.request_file, context=self.context)
        grant = build_grant(
            request, runtime="codex-exec", network_available=True
        )
        args = self.codex_args("one-shot", grant)
        args.nested_headless_network = True
        with mock.patch.dict(os.environ, self.env, clear=False):
            command = self.codex.shell_command(
                args, self.root / "prompt.txt", self.root / "log.jsonl"
            )
        self.assertIn("sandbox_workspace_write.network_access=true", command)
        self.assertIn(
            ",execution_access_network=enforced", receipt_fragment(grant)
        )

        app_grant = build_grant(
            request, runtime="codex-app-server", network_available=True
        )
        args = self.codex_args("app-server-supervised", app_grant)
        args.nested_headless_network = True
        with mock.patch.dict(os.environ, self.env, clear=False):
            command = self.codex.shell_command(
                args, self.root / "prompt.txt", self.root / "log.jsonl"
            )
        self.assertIn("--network-access", command)
        self.assertIn(
            ",execution_access_network=enforced", receipt_fragment(app_grant)
        )

    def test_claude_and_opencode_project_without_os_claim(self) -> None:
        prompt = self.root / "prompt.txt"
        log = self.root / "log.jsonl"
        for delivery, runtime in (
            ("one-shot", "claude-cli"),
            ("session-resume-supervised", "claude-supervisor"),
        ):
            grant = build_grant(self.request, runtime=runtime)
            command = self.claude.shell_command(
                self.claude_args(delivery, grant), prompt, log
            )
            self.assertIn(f"--add-dir {self.scoped}", command)
            self.assertEqual("tool-permission", grant.file_enforcement)
            self.assertEqual("none", grant.network_enforcement)
            self.assertNotIn("dangerously-bypass", command)

        grant = build_grant(self.request, runtime="opencode")
        config = json.loads(
            self.opencode.scoped_external_directory_config(
                str(self.artifact), None, grant.additional_writable_roots
            )
        )
        rules = config["permission"]["external_directory"]
        self.assertEqual("allow", rules[str(self.scoped)])
        self.assertEqual("allow", rules[f"{self.scoped}/**"])
        self.assertEqual("tool-permission", grant.file_enforcement)
        self.assertEqual("none", grant.network_enforcement)

    def test_receipt_matches_projection_and_is_absent_without_request(self) -> None:
        self.assertEqual("", receipt_fragment(None))
        grants = (
            build_grant(self.request, runtime="codex-exec"),
            build_grant(self.request, runtime="claude-cli"),
            build_grant(self.request, runtime="opencode"),
        )
        for grant in grants:
            fragment = receipt_fragment(grant)
            self.assertEqual(5, fragment.count(",execution_access_"))
            self.assertIn(",execution_access_roots=1", fragment)
            self.assertNotIn("\n", fragment)
            self.assertNotIn("=enforced", fragment if grant.file_enforcement != "os-sandbox" else "")

    def test_normal_grant_and_excess_refusal_are_atomic(self) -> None:
        grant = bind_request(
            str(self.request_file),
            environ=self.env,
            context=self.context,
            is_child=False,
            parent=None,
            runtime="codex-exec",
        )
        self.assertIsNotNone(grant)
        spawn_count = 0
        try:
            bind_request(
                str(self.request_file),
                environ=self.env,
                context=self.context,
                is_child=True,
                parent=ParentGrant(writable_roots=(self.root / "other",)),
                runtime="codex-exec",
            )
            spawn_count += 1
        except ExecutionAccessError as exc:
            self.assertTrue(exc.reason.startswith("execution-access-exceeds-parent:"))
            self.assertIn("top-level launch", exc.detail)
        self.assertEqual(0, spawn_count)

    def test_invalid_request_precedes_registry_and_model_spawn_in_all_adapters(self) -> None:
        for module in (self.codex, self.claude, self.opencode):
            source = Path(module.__file__).read_text(encoding="utf-8")
            bind_at = source.index("args.execution_access_grant = bind_execution_access_request")
            append_at = source.index("args.attempt_claimed = append_job", bind_at)
            spawn_at = source.index("spawn_claimed_attempt", append_at)
            self.assertLess(bind_at, append_at)
            self.assertLess(append_at, spawn_at)

        self.request_file.write_text("{", encoding="utf-8")
        spawn_count = 0
        try:
            bind_request(
                str(self.request_file),
                environ=self.env,
                context=self.context,
                is_child=False,
                parent=None,
                runtime="codex-exec",
            )
            spawn_count += 1
        except ExecutionAccessError as exc:
            self.assertEqual("execution-access-invalid-json", exc.reason)
        self.assertEqual(0, spawn_count)

    def test_all_adapter_parsers_accept_only_the_file_surface(self) -> None:
        required = [
            "--worktree",
            str(self.worktree),
            "--slug",
            "fixture",
            "--capability",
            "autopilot-code",
            "--execution-access-file",
            str(self.request_file),
        ]
        for module in (self.codex, self.claude, self.opencode):
            args = module.parser().parse_args(required)
            self.assertEqual(str(self.request_file), args.execution_access_file)


if __name__ == "__main__":
    unittest.main()
