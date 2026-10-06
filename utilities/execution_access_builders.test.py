#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import replace
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
    load_parent_effective_grant,
    publish_effective_grant,
    prepare_task_request,
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
            if delivery == "one-shot":
                # Session persistence changed independently of access grants.
                # Keep every permission/sandbox/path byte pinned to the base.
                self.assertIn("codex exec --cd", base)
                base = base.replace("codex exec --cd", "codex exec --ephemeral --cd", 1)
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

    def test_lab_inventory_and_explicit_data_reach_both_codex_execution_builders(self) -> None:
        run_root = self.root / "inventory-runs"
        inventory = self.root / "compute-hosts.yaml"
        inventory.write_text(
            f"schema_version: 1\nrun_root: {run_root}\nhosts:\n  fixture:\n    ssh_host: local\n")
        route = {"route_id": "rt-lab-builders", "route_hash": "sha256:" + "d" * 64,
                 "capability": "autopilot-lab", "cwd": str(self.worktree),
                 "artifact_root": str(self.artifact), "work_request": {"text": "Run lab"}}
        with mock.patch.dict(os.environ, {
            **self.env, "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file),
        }, clear=True):
            prepared = prepare_task_request(route, self.state / "jobs.log")
        for delivery, runtime, flag in (("one-shot", "codex-exec", "--add-dir"),
                                        ("app-server-supervised", "codex-app-server", "--writable-root")):
            grant = self.codex.bind_execution_access_request(
                str(prepared), environ=self.env, context=self.context,
                is_child=False, parent=None, runtime=runtime)
            command = self.codex.shell_command(self.codex_args(delivery, grant),
                                               self.root / "prompt.txt", self.root / "log.jsonl")
            self.assertIn(f"{flag} {run_root}", command)
            self.assertIn(f"{flag} {self.scoped}", command)
        self.assertFalse(run_root.exists())

    def test_all_adapter_execution_surfaces_consume_the_same_request(self) -> None:
        expected = str(self.request_file.resolve())
        rows = (
            (self.codex, "codex-exec"),
            (self.claude, "claude-cli"),
            (self.opencode, "opencode"),
        )
        for module, runtime in rows:
            grant = module.bind_execution_access_request(
                expected,
                environ=self.env,
                context=self.context,
                is_child=False,
                parent=None,
                runtime=runtime,
            )
            self.assertIsNotNone(grant)
            self.assertEqual(Path(expected), grant.source_path)
            self.assertEqual((self.scoped.resolve(),), grant.writable_roots)

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

    def test_codex_read_only_refuses_before_writable_argv_projection(self) -> None:
        args = self.codex_args("one-shot")
        args.sandbox = "read-only"
        self.assertEqual("read-only", self.codex.effective_runtime_sandbox(args))
        with self.assertRaises(ExecutionAccessError) as raised:
            bind_request(
                str(self.request_file),
                environ=self.env,
                context=self.context,
                is_child=False,
                parent=None,
                runtime="codex-exec",
                effective_sandbox=self.codex.effective_runtime_sandbox(args),
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-read-only",
            raised.exception.reason,
        )

    def checked_parent(self):
        grant = build_grant(self.request, runtime="codex-app-server")
        jobs = self.state / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        path, digest = publish_effective_grant(
            jobs=jobs, attempt_id="att-parent", route_id="rt-parent",
            route_hash="sha256:" + "b" * 64, runtime="codex-app-server",
            sandbox="workspace-write", grant=grant,
            default_writable_roots=(self.worktree, self.artifact), network_allowed=False,
        )
        jobs.write_text("now\topen\t12\tparent\tparent-slug\t"
                        "attempt_id=att-parent,route_id=rt-parent,route_hash=sha256:" + "b" * 64
                        + ",runtime_sandbox=workspace-write,execution_access_effective_file=" + str(path)
                        + ",execution_access_effective_sha256=" + digest + "\n")
        return load_parent_effective_grant(jobs=jobs, parent_attempt_id="att-parent",
                                           context=self.context)

    def nested_grant(self, args, parent):
        return bind_request(
            str(self.request_file), environ=self.env, context=self.context,
            is_child=True, parent=parent, runtime="codex-exec",
            default_writable_roots=(self.worktree, self.artifact),
            effective_sandbox=self.codex.effective_runtime_sandbox(args),
            inherit_parent_sandbox=self.codex.uses_enclosing_codex_sandbox(args),
        )

    def test_nested_request_uses_exact_outer_boundary_and_actual_inner_argv(self):
        # The installed failure was an inherited inventory run-root, not a GPU
        # selection. Strict OS requests must use the same real parent boundary.
        for required in ("any", "os-sandbox"):
            with self.subTest(required=required):
                self.write_request(enforcement_required=required)
                self.request = load_request(self.request_file, context=self.context)
                parent = self.checked_parent()
                for delivery in ("one-shot", "app-server-supervised"):
                    args = self.codex_args(delivery)
                    args.dispatch_depth = 2
                    args.launch_lifecycle = "foreground-scoped"
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
                        grant = self.nested_grant(args, parent)
                        args.execution_access_grant = grant
                        cmd = self.codex.shell_command(args, self.root / "prompt.txt", self.root / "log")
                    self.assertIn("--sandbox danger-full-access", cmd)
                    self.assertNotIn(str(self.scoped), cmd)  # already projected by parent
                    self.assertEqual("os-sandbox", grant.file_enforcement)
                    self.assertEqual("att-parent", grant.enclosing_parent_attempt_id)
                    self.assertEqual((), grant.additional_writable_roots)
                    self.assertIn(",execution_access_boundary=parent-os-sandbox", receipt_fragment(grant))
                    self.assertIn(",execution_access_parent_attempt=att-parent", receipt_fragment(grant))
                    child, _ = publish_effective_grant(
                        jobs=self.state / "jobs.log", attempt_id="att-" + required + delivery,
                        route_id="rt-parent", route_hash="sha256:" + "b" * 64,
                        runtime="codex-exec", sandbox="danger-full-access", grant=grant,
                        default_writable_roots=(self.worktree, self.artifact), network_allowed=False)
                    record = json.loads(child.read_text())
                    self.assertEqual("danger-full-access", record["sandbox"])
                    self.assertEqual("parent-os-sandbox", record["boundary"])
                    self.assertEqual("att-parent", record["enclosing_parent_attempt_id"])
                    self.assertTrue(record["os_filesystem_enforced"])
                    self.assertFalse(record["network_allowed"])
                    self.assertEqual([], record["read_roots"])
                    self.assertIn(str(self.scoped), record["writable_roots"])
                # Each scenario owns a fresh canonical record, never rewrites it.
                self.state.joinpath("jobs.log").unlink()
                import shutil
                shutil.rmtree(self.state / "execution-access")

    def test_explicit_full_host_detached_and_read_only_never_inherit_os_grade(self):
        parent = self.checked_parent()
        for mode, lifecycle, child_env in (
            ("danger-full-access", "foreground-scoped", "1"),  # explicit or FORCE result
            ("danger-full-access", "detached", ""),
            ("read-only", "foreground-scoped", "1"),
        ):
            with self.subTest(mode=mode, lifecycle=lifecycle):
                args = self.codex_args("one-shot")
                args.dispatch_depth, args.launch_lifecycle, args.sandbox = 2, lifecycle, mode
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": child_env}):
                    self.assertFalse(self.codex.uses_enclosing_codex_sandbox(args))
                    self.assertEqual(mode, self.codex.effective_runtime_sandbox(args))
                    with self.assertRaises(ExecutionAccessError) as raised:
                        self.nested_grant(args, parent)
                self.assertEqual("execution-access-enforcement-unavailable:" +
                                 ("codex-read-only" if mode == "read-only" else "codex-file-sandbox"),
                                 raised.exception.reason)
        for env, change in (("", {}), ("1", {"launch_lifecycle": "detached"}),
                            ("1", {"parent_transport": "interactive"}),
                            ("1", {"parent_sandbox": "danger-full-access"}),
                            ("1", {"parent_harness": "claude"}),
                            ("1", {"gpu_execution_scope": True})):
            args = self.codex_args("one-shot")
            args.dispatch_depth, args.launch_lifecycle = 2, "foreground-scoped"
            for key, value in change.items():
                setattr(args, key, value)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": env}):
                self.assertFalse(self.codex.uses_enclosing_codex_sandbox(args))
                self.assertEqual("workspace-write", self.codex.effective_runtime_sandbox(args))

    def test_nested_missing_enforcement_parent_expansion_and_unsupported_axes_refused(self):
        parent = self.checked_parent()
        args = self.codex_args("one-shot")
        args.dispatch_depth, args.launch_lifecycle = 2, "foreground-scoped"
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
            for bad in (replace(parent, file_enforcement="none"),
                        replace(parent, sandbox="danger-full-access"),
                        replace(parent, runtime="claude-cli"), replace(parent, attempt_id=""),
                        replace(parent, network_enforcement="unknown")):
                with self.subTest(parent=bad), self.assertRaises(ExecutionAccessError) as raised:
                    self.nested_grant(args, bad)
                self.assertEqual("execution-access-enforcement-unavailable:codex-parent-sandbox",
                                 raised.exception.reason)
            with self.assertRaises(ExecutionAccessError):
                self.nested_grant(args, None)
            for bad in (replace(parent, writable_roots=(self.worktree, self.artifact)),
                        replace(parent, writable_roots=(self.scoped,))):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.nested_grant(args, bad)
                self.assertTrue(raised.exception.reason.startswith("execution-access-exceeds-parent:"))
            inherited_network = self.nested_grant(args, replace(parent, network_allowed=True))
            record_path, _ = publish_effective_grant(
                jobs=self.state / "jobs.log", attempt_id="att-inherited-network",
                route_id="rt-parent", route_hash="sha256:" + "b" * 64,
                runtime="codex-exec", sandbox="danger-full-access", grant=inherited_network,
                default_writable_roots=(self.worktree, self.artifact), network_allowed=False)
            self.assertEqual("not-requested", inherited_network.network)
            self.assertTrue(json.loads(record_path.read_text())["network_allowed"])
            self.write_request(network={"required": True, "reason": "fetch", "hosts": []})
            with self.assertRaises(ExecutionAccessError) as raised:
                self.nested_grant(args, parent)
            self.assertEqual("execution-access-exceeds-parent:network", raised.exception.reason)
            self.write_request(network={"required": True, "reason": "fetch", "hosts": ["example.com"]},
                               enforcement_required="os-sandbox")
            with self.assertRaises(ExecutionAccessError) as raised:
                self.nested_grant(args, replace(parent, network_allowed=True))
            self.assertEqual("execution-access-enforcement-unavailable:codex-network-hosts",
                             raised.exception.reason)
            self.write_request(read_roots=[str(self.scoped)], enforcement_required="os-sandbox")
            with self.assertRaises(ExecutionAccessError) as raised:
                self.nested_grant(args, parent)
            self.assertEqual("execution-access-enforcement-unavailable:codex-exec", raised.exception.reason)

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
        install = self.root / "installed-contract"
        (install / "capabilities").mkdir(parents=True)
        alias = self.root / "agent-home"
        alias.symlink_to(install, target_is_directory=True)
        existing = {
            "theme": "system",
            "permission": {
                "bash": "deny", "read": {"*.env": "deny"},
                "edit": {"*": "ask", "/keep/**": "deny"},
                "external_directory": {"*": "deny", "/keep/**": "deny"},
            },
        }
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}):
            config = json.loads(self.opencode.scoped_external_directory_config(
                str(self.artifact), None, grant.additional_writable_roots,
                agent_home=alias, worktree=str(self.worktree),
            ))
        rules = config["permission"]["external_directory"]
        self.assertEqual("allow", rules[str(self.scoped)])
        self.assertEqual("allow", rules[f"{self.scoped}/**"])
        self.assertEqual("system", config["theme"])
        self.assertEqual("deny", config["permission"]["bash"])
        self.assertEqual({"*.env": "deny"}, config["permission"]["read"])
        self.assertEqual("deny", rules["*"])
        self.assertEqual("deny", rules["/keep/**"])
        for directory in (alias / "capabilities", install / "capabilities"):
            for pattern in (str(directory), f"{directory}/**"):
                self.assertEqual("allow", rules[pattern])
                self.assertEqual("deny", config["permission"]["edit"][pattern])
            relative = os.path.relpath(directory, self.worktree)
            self.assertEqual("deny", config["permission"]["edit"][f"{relative}/**"])
        self.assertEqual("ask", config["permission"]["edit"]["*"])
        self.assertEqual("deny", config["permission"]["edit"]["/keep/**"])
        for directory in (alias, install, install / "core", install / "roles", install / "skills"):
            self.assertNotIn(str(directory), rules)
            self.assertNotIn(f"{directory}/**", rules)
        # A string/global deny remains the default for every other edit.
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"permission":"deny"}'}):
            denied = json.loads(self.opencode.scoped_external_directory_config(
                str(self.artifact), agent_home=alias, worktree=str(self.worktree),
            ))
        self.assertEqual("deny", denied["permission"]["edit"]["*"])
        self.assertEqual("deny", denied["permission"]["*"])
        # The normal --agent selection merges its rules after global rules.
        # Both original agent overrides must keep the contract-only exception.
        for override in ({"external_directory": "deny"}, {"edit": "allow"}):
            selected = {"permission": {**override, "read": "deny", "bash": "ask"},
                        "model": "fixture/model", "prompt": "keep original prompt"}
            existing["agent"] = {"research": selected,
                                 "build": {"permission": {"edit": "allow"}}}
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}):
                projected = json.loads(self.opencode.scoped_external_directory_config(
                    str(self.artifact), agent_home=alias, worktree=str(self.worktree),
                    selected_agent="research",
                ))
            self.assertEqual(existing["agent"]["build"], projected["agent"]["build"])
            local = projected["agent"]["research"]
            self.assertEqual("fixture/model", local["model"])
            self.assertEqual("keep original prompt", local["prompt"])
            self.assertEqual("deny", local["permission"]["read"])
            self.assertEqual("ask", local["permission"]["bash"])
            for directory in (alias / "capabilities", install / "capabilities"):
                for pattern in (str(directory), f"{directory}/**"):
                    self.assertEqual("allow", local["permission"]["external_directory"][pattern])
                    self.assertEqual("deny", local["permission"]["edit"][pattern])
                relative = os.path.relpath(directory, self.worktree)
                self.assertEqual("deny", local["permission"]["edit"][f"{relative}/**"])
            for tool, default in override.items():
                self.assertEqual(default, local["permission"][tool]["*"])
            self.assertNotIn(f"{alias}/**", local["permission"]["external_directory"])
        # An absent selected-agent override inherits every global default.
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "{}"}):
            inherited = json.loads(self.opencode.scoped_external_directory_config(
                str(self.artifact), agent_home=alias, worktree=str(self.worktree),
                selected_agent="build",
            ))
        self.assertNotIn("*", inherited["agent"]["build"]["permission"]["edit"])
        # Fixed original-order counterexamples: a later outer wildcard wins
        # outside capabilities, while the narrow new exception always wins there.
        for original, expected_edit, expected_external in (
            ({"edit": "allow", "*": "allow"}, "allow", "allow"),
            ({"external_directory": "deny", "*": "deny"}, "deny", "deny"),
            ({"edit": "deny", "*": "allow"}, "allow", "allow"),
            ({"external_directory": "allow", "*": "deny"}, "deny", "deny"),
            ({"*": "allow", "edit": "deny"}, "deny", "allow"),
            ({"*": "deny", "external_directory": "allow"}, "deny", "allow"),
        ):
            before = {"permission": original,
                      "agent": {"build": {"permission": original}}}
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(before)}):
                after = json.loads(self.opencode.scoped_external_directory_config(
                    str(self.artifact), agent_home=alias, worktree=str(self.worktree),
                    selected_agent="build",
                ))
            for permission in (after["permission"], after["agent"]["build"]["permission"]):
                keys = list(permission)
                self.assertGreater(keys.index("edit"), keys.index("*"))
                self.assertGreater(keys.index("external_directory"), keys.index("*"))
                self.assertEqual(expected_edit, permission["edit"]["*"])
                # Absent global external keeps the original invocation deny;
                # an explicit selected-agent wildcard keeps its native override.
                external_default = ("deny" if permission is after["permission"]
                                    and "external_directory" not in original else expected_external)
                self.assertEqual(external_default, permission["external_directory"]["*"])
                self.assertEqual("deny", permission["edit"][f"{alias / 'capabilities'}/**"])
                self.assertEqual("allow", permission["external_directory"][f"{alias / 'capabilities'}/**"])
        # Partial wildcard objects leave non-overlapping earlier rules valid.
        # Outer wildcard tool names use the native '*'/'?' matching grammar.
        for original in (
            {"edit": {"private/**": "deny"}, "*": {"public/**": "allow"}},
            {"ed?t": {"private/**": "deny"}, "e*": {"public/**": "allow"}},
            {"*": {"public/**": "allow"}, "edit": {"private/**": "deny"}},
        ):
            before = {"permission": original,
                      "agent": {"build": {"permission": original}}}
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(before)}):
                after = json.loads(self.opencode.scoped_external_directory_config(
                    str(self.artifact), agent_home=alias, worktree=str(self.worktree),
                    selected_agent="build",
                ))
            for permission in (after["permission"], after["agent"]["build"]["permission"]):
                self.assertEqual("deny", permission["edit"]["private/**"])
                self.assertEqual("allow", permission["edit"]["public/**"])
                self.assertEqual("deny", permission["edit"][f"{alias / 'capabilities'}/**"])
                self.assertEqual("allow", permission["external_directory"][f"{alias / 'capabilities'}/**"])
        # A later scalar wildcard does supersede every earlier private rule;
        # duplicate patterns move to their last native position within objects.
        before = {"permission": {"edit": {"private/**": "deny"}, "*": "allow"},
                  "agent": {"build": {"permission": {
                      "edit": {"private/**": "deny", "private/safe/**": "deny"},
                      "*": {"private/**": "allow"},
                  }}}}
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(before)}):
            after = json.loads(self.opencode.scoped_external_directory_config(
                str(self.artifact), agent_home=alias, worktree=str(self.worktree), selected_agent="build",
            ))
        self.assertNotIn("private/**", after["permission"]["edit"])
        ordered = after["agent"]["build"]["permission"]["edit"]
        self.assertEqual("allow", ordered["private/**"])
        self.assertGreater(list(ordered).index("private/**"), list(ordered).index("private/safe/**"))
        for default in ("allow","ask"):
            for permission in (default,{"*":default,"read":"deny","bash":"ask"}):
                before = {"permission":permission}
                with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT":json.dumps(before)}):
                    after = json.loads(self.opencode.scoped_external_directory_config(
                        str(self.artifact),agent_home=alias,worktree=str(self.worktree),selected_agent="build",
                    ))
                self.assertEqual("deny",after["permission"]["external_directory"]["*"])
                self.assertEqual(default,after["permission"]["*"])
                if isinstance(permission,dict):
                    self.assertEqual("deny",after["permission"]["read"])
                    self.assertEqual("ask",after["permission"]["bash"])
                self.assertNotIn("*",after["agent"]["build"]["permission"]["external_directory"])
                self.assertEqual("allow",after["permission"]["external_directory"][f"{alias / 'capabilities'}/**"])
                self.assertNotIn(f"{alias}/**",after["permission"]["external_directory"])
        # Existing explicit global/selected external allow is not blanket-denied.
        for before in ({"permission":{"*":"deny","external_directory":"allow"}},
                       {"permission":"ask","agent":{"build":{"permission":{"external_directory":"allow"}}}}):
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT":json.dumps(before)}):
                after = json.loads(self.opencode.scoped_external_directory_config(
                    str(self.artifact),agent_home=alias,worktree=str(self.worktree),selected_agent="build",
                ))
            effective = after["agent"]["build"]["permission"] if "agent" in before else after["permission"]
            self.assertEqual("allow",effective["external_directory"]["*"])
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
