#!/usr/bin/env python3
"""Isolated active-merge to same-owner-resume permission projection coverage."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import commit_policy  # noqa: E402

ADAPTER_FLAGS = {
    "claude": ["--model", "claude-fixture", "--effort", "low"],
    "codex": ["--model", "gpt-fixture", "--reasoning", "low"],
    "opencode": ["--model", "provider/fixture", "--variant", "low"],
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OwnerRuntimePermissionProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hearting-owner-resume-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / ".agent_reports"
        self.output_dir = self.artifact_root / "campaigns" / "fixture" / "cycle" / "artifacts"
        (self.output_dir / "dev_logs").mkdir(parents=True)
        self.origin = self.root / "origin.git"
        self.route_file = self.root / "route.json"
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.claude_home = self.root / "claude"
        self.claude_home.mkdir(mode=0o700)
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir(mode=0o700)
        self.data_home = self.root / "data"
        self.data_home.mkdir(mode=0o700)
        self.jobs = {name: self.root / f"{name}.jobs.log" for name in ADAPTER_FLAGS}
        self.logs = {name: self.root / f"{name}-logs" for name in ADAPTER_FLAGS}
        for path in (*self.jobs.values(),):
            path.write_text("", encoding="utf-8")
        for path in self.logs.values():
            path.mkdir()
        self._git("init", "-q", "-b", "main", str(self.repo))
        self._git("config", "user.email", "fixture@example.test")
        self._git("config", "user.name", "Fixture")
        (self.repo / "engine.py").write_text("value = 'base'\n", encoding="utf-8")
        self._git("add", "engine.py")
        self._git("commit", "-q", "-m", "base")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self._git("remote", "add", "origin", str(self.origin))
        self._git("push", "-q", "origin", "main")
        self._git("remote", "set-head", "origin", "main")
        self._git("switch", "-q", "-c", "pr-ci-task")
        (self.repo / "engine.py").write_text("value = 'task'\n", encoding="utf-8")
        self._git("commit", "-q", "-am", "task change")
        self.route = self._compile_complete_owner_route()
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        self._git("switch", "-q", "main")
        self._git("switch", "-q", "-c", "merge-incoming")
        (self.repo / "engine.py").write_text("value = 'incoming'\n", encoding="utf-8")
        self._git("commit", "-q", "-am", "incoming change")
        self._git("switch", "-q", "pr-ci-task")
        conflict = subprocess.run(["git", "-C", str(self.repo), "merge", "--no-commit", "--no-ff",
                                   "merge-incoming"], capture_output=True, text=True)
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").is_file())
        self._write_fixture_clis()

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True)

    def _compile_complete_owner_route(self):
        route_module = load_module("fixture_capability_route", ROOT / "utilities/capability-route.py")
        from route_identity import route_hash, route_id_from_hash
        evidence = [{
            "parent_harness": harness,
            "parent_transport": "headless",
            "parent_sandbox": route_module.WRAPPER_PARENT_SANDBOXES[harness][0],
            "child_harness": harness,
            "launch_authority": "conductor",
            "status": "supported",
            "probe_source": f"{harness}-fixture",
            "probe_time": "2026-10-18T00:00:00Z",
            "failure_class": "",
            "checked_worktree": str(self.repo.resolve()),
            "failure_scope": "none",
            "codex_command": "ok" if harness == "codex" else "not-applicable",
            "retry_on_isolated_worktree": 0,
        } for harness in ADAPTER_FLAGS]
        gate = {"spec_read": {"satisfied": True, "source": "fixture"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "fixture"}}
        env = self.base_env()
        with mock.patch.dict(os.environ, env, clear=True):
            route = route_module.compile_route(
                "autopilot-code", "dev", "standard", self.repo, self.artifact_root,
                signals=[], transport="headless", tracking="tracked",
                tracked_gate_evidence=gate,
                dispatch_evidence={"tuples": evidence, "native_subagent": []},
            )
        route["entry_scope_contract_version"] = 1
        route["entry_execution_scope"] = "complete"
        route["work_request"] = {
            "text": "변경 사항을 정리하고 자동 검사 결과를 전달해 주세요.",
            "owner_harness": None,
        }
        route["route_hash"] = route_hash(route)
        route["route_id"] = route_id_from_hash(route["route_hash"])
        route_module.verify_route(route, expected_cwd=str(self.repo.resolve()))
        return route

    def _write_fixture_clis(self):
        claude = self.fakebin / "claude"
        claude.write_text("#!/usr/bin/env python3\n"
                          "print('--resume --session-id ' + 'x' * 20000 + 'update|upgrade')\n",
                          encoding="utf-8")
        claude.chmod(0o755)
        codex = self.fakebin / "codex"
        codex.write_text("#!/bin/sh\nif [ \"$1\" = app-server ] && [ \"$2\" = --help ]; then\n"
                         "  echo 'stdio:// app-server fixture'\n  exit 0\nfi\nexit 0\n",
                         encoding="utf-8")
        codex.chmod(0o755)

    def base_env(self, jobs=None):
        env = {
            "PATH": str(self.fakebin) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_ARTIFACT_OUTPUT_DIR": str(self.output_dir),
            "AGENT_DISPATCH_JOBS": str(jobs or self.root / "fixture.jobs.log"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "CODEX_HOME": str(self.codex_home),
            "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"bash": "allow", "edit": "allow"}}),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return env

    def route_binding_env(self, env):
        env.update({"AGENT_OWNER_ROUTE_FILE": str(self.route_file),
                    "AGENT_OWNER_ROUTE_ID": self.route["route_id"],
                    "AGENT_OWNER_ROUTE_HASH": self.route["route_hash"]})
        return env

    def merge_snapshot(self):
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip())
        names = ("index", "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "ORIG_HEAD", "AUTO_MERGE")
        metadata = {name: (git_dir / name).read_bytes() for name in names
                    if (git_dir / name).exists()}
        return (self.repo.joinpath("engine.py").read_bytes(), metadata)

    def _register_owner(self, harness):
        wrapper = load_module(f"{harness}_registered_owner_fixture",
                              ROOT / f"adapters/{harness}/bin/dispatch-headless.py")
        attempt = f"att-replacement-{harness}"
        argv = ["dispatch-headless.py", "--register", "--worktree", str(self.repo),
                "--slug", f"{harness}-replacement", "--capability", "autopilot-code",
                "--capability-mode", "dev", "--intensity", "standard", "--dispatch-depth", "1",
                "--worker-type", "owner", "--unit", "_kernel/owner",
                "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
                "--owner-harness", harness,
                "--jobs", str(self.jobs[harness]),
                "--log-dir", str(self.logs[harness]), "--attempt-id", attempt,
                "--model-profile", self.route["owner_model_profile"]]
        if harness == "claude":
            argv.extend(["--permission-mode", "allowlist"])
        env = self.route_binding_env(self.base_env(self.jobs[harness]))
        stream = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(stream):
            code = wrapper.main(argv)
        output = stream.getvalue()
        self.assertEqual(code, 0, output)
        self.assertIn("child_spawned=0", output)
        rows = self.jobs[harness].read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(attempt in row for row in rows), 1, rows)
        self.assertTrue(any(f"route_id={self.route['route_id']}" in row for row in rows), rows)
        return attempt

    def _runtime_context(self, harness, attempt):
        base = self.route_binding_env(self.base_env(self.jobs[harness]))
        context_args = argparse.Namespace(
            worker_type="owner", dispatch_depth=1, capability="autopilot-code",
            worktree=str(self.repo), route_file=str(self.route_file),
            route_id=self.route["route_id"], route_hash=self.route["route_hash"],
            artifact_root=str(self.artifact_root), artifact_output_dir=str(self.output_dir),
            attempt_id=attempt, owner_route_binding=None,
        )
        extra = {"adapter": harness}
        if harness == "claude":
            extra.update(permission_mode_requested="allowlist", agent_home=str(ROOT))
        elif harness == "codex":
            extra.update(base_sandbox="workspace-write", network_eligible=True)
        else:
            extra.update(
                agent_home=str(ROOT),
                opencode_config_base=base["OPENCODE_CONFIG_CONTENT"],
                report_bundle_root=None,
                execution_access_roots=[], execution_access_read_roots=[],
                contract_read_roots=[], selected_agent="build",
            )
        with mock.patch.dict(os.environ, base, clear=True):
            base.update(commit_policy.runtime_permission_context_env(context_args, **extra))
        return base

    def _claude_session(self, attempt):
        capture = self.root / "claude-calls.jsonl"
        fake = self.root / "claude_api.py"
        fake.write_text(textwrap.dedent("""\
            import json, os, sys
            with open(os.environ["CLAUDE_CAPTURE"], "a", encoding="utf-8") as out:
                out.write(json.dumps(sys.argv[1:]) + "\\n")
            print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"ok"}))
            """), encoding="utf-8")
        module = load_module("fixture_claude_session_supervisor",
                             ROOT / "utilities/claude-session-supervisor.py")
        args = argparse.Namespace(
            runtime_harness="claude", parent_attempt_id=attempt, jobs=str(self.jobs["claude"]),
            worktree=str(self.repo), turn_timeout=10, state_file=None,
            claude_command=shlex.join([sys.executable, str(fake)]),
            add_dir=[str(self.artifact_root)], model=None, effort=None,
            disallowed_tool=[], permission_mode="acceptEdits", allowed_tool=[],
            turn_transport="resume-process",
        )
        return module, args, capture

    def _opencode_session(self, attempt):
        capture = self.root / "opencode-calls.jsonl"
        fake = self.root / "opencode_api.py"
        fake.write_text(textwrap.dedent("""\
            import json, os, sys
            with open(os.environ["OPENCODE_CAPTURE"], "a", encoding="utf-8") as out:
                out.write(json.dumps({"argv":sys.argv[1:],
                    "config":json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])}) + "\\n")
            print(json.dumps({"type":"text","sessionID":"ses_fixture","part":{"text":"ok"}}))
            print(json.dumps({"type":"step_finish","sessionID":"ses_fixture","part":{"reason":"stop"}}))
            """), encoding="utf-8")
        module = load_module("fixture_opencode_session_supervisor",
                             ROOT / "utilities/claude-session-supervisor.py")
        args = argparse.Namespace(
            runtime_harness="opencode", parent_attempt_id=attempt,
            jobs=str(self.jobs["opencode"]), worktree=str(self.repo), turn_timeout=10,
            model=None, variant=None, opencode_agent="build",
            opencode_command=shlex.join([sys.executable, str(fake)]), owner_input=None,
        )
        return module, args, capture

    def _codex_session(self, env, attempt, *, named_profile=None, approval="inherit"):
        module = load_module("fixture_codex_app_server_supervisor",
                             ROOT / "utilities/codex-app-server-supervisor.py")
        fake = self.root / "codex_app_server_fixture.py"
        trace = self.root / "codex-requests.jsonl"
        fake.write_text(textwrap.dedent("""\
            import json, os, sys
            turns = 0
            def send(value):
                print(json.dumps(value), flush=True)
            def record(value):
                with open(os.environ["CODEX_TRACE"], "a", encoding="utf-8") as out:
                    out.write(json.dumps(value) + "\\n")
            for line in sys.stdin:
                value = json.loads(line)
                method, params = value.get("method"), value.get("params", {})
                if method == "initialize":
                    send({"jsonrpc":"2.0","id":value["id"],"result":{}})
                elif method == "initialized":
                    pass
                elif method == "thread/start":
                    record({"method":method,"params":params})
                    send({"jsonrpc":"2.0","id":value["id"],"result":{"thread":{"id":"thread-same-owner"}}})
                elif method == "turn/start":
                    turns += 1
                    record({"method":method,"params":params})
                    turn = f"turn-{turns}"
                    send({"jsonrpc":"2.0","id":value["id"],"result":{"turn":{"id":turn}}})
                    send({"jsonrpc":"2.0","method":"item/completed","params":{
                        "threadId":"thread-same-owner","turnId":turn,
                        "item":{"type":"agentMessage","id":f"message-{turns}","text":"ok"}}})
                    send({"jsonrpc":"2.0","method":"turn/completed","params":{
                        "threadId":"thread-same-owner","turn":{"id":turn,"status":"completed"}}})
            """), encoding="utf-8")
        env = {**env, "CODEX_TRACE": str(trace)}
        args = argparse.Namespace(
            parent_attempt_id=attempt, worktree=str(self.repo), writable_root=[],
            base_writable_root=[],
            sandbox="workspace-write" if named_profile is not None else "read-only",
            network_access=named_profile is not None,
            approval=approval, model=None, reasoning=None, owner_input=None,
            native_permission_profile=named_profile,
        )
        command = [sys.executable, str(fake)]
        if named_profile is not None:
            command.extend(module.config_arguments(named_profile))
        thread_params = {"cwd": str(self.repo), "ephemeral": True}
        if named_profile is None:
            thread_params["sandbox"] = "read-only"
        if approval != "inherit":
            thread_params["approvalPolicy"] = approval
        with mock.patch.dict(os.environ, env, clear=True):
            server = module.AppServer(command, str(self.repo), dict(os.environ))
            server.request("initialize", {"clientInfo":{"name":"fixture"},"capabilities":None})
            server.notification("initialized")
            thread = server.request("thread/start", thread_params)["thread"]["id"]
        self.addCleanup(server.close)
        return module, args, server, thread, trace, command, thread_params

    def _claude_turn(self, session, env, *, resume, prompt):
        module, args, _capture = session
        active_env = {**env, "CLAUDE_CAPTURE": str(_capture)}
        with mock.patch.dict(os.environ, active_env, clear=True), mock.patch.object(module, "emit"):
            module.run_turn(args, "ses_same-owner", prompt, resume=resume)
        return args.permission_mode, list(args.allowed_tool)

    def _opencode_turn(self, session, env, *, resume, prompt):
        module, args, capture = session
        active_env = {**env, "OPENCODE_CAPTURE": str(capture)}
        with mock.patch.dict(os.environ, active_env, clear=True), mock.patch.object(module, "emit"):
            module.run_turn(args, "ses_fixture", prompt, resume=resume)

    def _codex_turn(self, session, env, *, prompt):
        module, args, server, thread, _trace = session[:5]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(module, "emit"):
            module.run_turn(server, thread_id=thread, prompt=prompt, args=args)

    def test_all_adapters_resume_same_owner_after_conflict_is_settled(self):
        before = self.merge_snapshot()
        attempts = {harness: self._register_owner(harness) for harness in ADAPTER_FLAGS}
        for harness, attempt in attempts.items():
            self.assertTrue(any(f"attempt_id={attempt}" in row
                                for row in self.jobs[harness].read_text(encoding="utf-8").splitlines()))
        self.assertEqual(self.merge_snapshot(), before)

        guard = load_module("fixture_worker_route_guard", ROOT / "utilities/worker-route-guard.py")
        execute = next(node for node in self.route["nodes"] if node["id"] == "execute")
        with self.assertRaises(guard.WorkerRouteError) as blocked:
            guard.validate_route_contract(
                self.route_file, "execute", self.repo, self.artifact_root,
                "autopilot-code", "standard", ";".join(execute["write_scope"]),
                self.route["route_id"], self.route["route_hash"], self.route["registry_digest"],
            )
        self.assertEqual(blocked.exception.reason, "unsafe-git-operation")

        initial = commit_policy.runtime_permission_projection(argparse.Namespace(
            worker_type="owner", dispatch_depth=1, capability="autopilot-code",
            worktree=str(self.repo), route_file=str(self.route_file),
            route_id=self.route["route_id"], route_hash=self.route["route_hash"],
            artifact_root=str(self.artifact_root), artifact_output_dir=str(self.output_dir),
        ))
        self.assertTrue(initial["observation_only"])
        self.assertFalse(initial["may_commit"])
        self.assertIsNone(initial["task_publish_scope"])

        runtime_envs = {harness: self._runtime_context(harness, attempts[harness])
                        for harness in ADAPTER_FLAGS}
        claude_session = self._claude_session(attempts["claude"])
        opencode_session = self._opencode_session(attempts["opencode"])
        codex_session = self._codex_session(runtime_envs["codex"], attempts["codex"])
        first_mode, first_allowed = self._claude_turn(
            claude_session, runtime_envs["claude"], resume=False, prompt="fixture prompt")
        self._opencode_turn(opencode_session, runtime_envs["opencode"],
                            resume=False, prompt="fixture prompt")
        self._codex_turn(codex_session, runtime_envs["codex"], prompt="fixture prompt")
        self.assertEqual(self.merge_snapshot(), before)

        # Only this test process resolves and commits the fixture conflict.
        (self.repo / "engine.py").write_text("value = 'fixture resolution'\n", encoding="utf-8")
        self._git("add", "engine.py")
        self._git("commit", "-q", "-m", "settle test fixture merge")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

        settled = commit_policy.runtime_permission_projection(argparse.Namespace(
            worker_type="owner", dispatch_depth=1, capability="autopilot-code",
            worktree=str(self.repo), route_file=str(self.route_file),
            route_id=self.route["route_id"], route_hash=self.route["route_hash"],
            artifact_root=str(self.artifact_root), artifact_output_dir=str(self.output_dir),
        ))
        self.assertFalse(settled["observation_only"])
        self.assertTrue(settled["may_commit"])
        self.assertEqual(settled["task_publish_scope"]["branch"], "pr-ci-task")

        # The identical supervisor session/attempt resumes with fresh native permissions.
        second_mode, second_allowed = self._claude_turn(
            claude_session, runtime_envs["claude"], resume=True,
            prompt="fixture continuation")
        self._opencode_turn(opencode_session, runtime_envs["opencode"],
                            resume=True, prompt="fixture continuation")
        self._codex_turn(codex_session, runtime_envs["codex"], prompt="fixture continuation")
        calls = [json.loads(line) for line in claude_session[2].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(first_mode, "plan")
        self.assertEqual(second_mode, "acceptEdits")
        self.assertIn("--session-id", calls[0])
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], "ses_same-owner")
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], "ses_same-owner")
        self.assertNotIn("git push -u origin HEAD:refs/heads/pr-ci-task", " ".join(calls[0]))
        self.assertIn("git push -u origin HEAD:refs/heads/pr-ci-task", " ".join(calls[1]))
        self.assertNotIn("git commit", " ".join(calls[0]))
        self.assertNotIn("gh pr create", " ".join(calls[0]))
        self.assertIn("gh pr create", " ".join(calls[1]))
        self.assertFalse(any("git push" in rule or "gh pr create" in rule for rule in first_allowed))
        self.assertTrue(any("git push -u origin HEAD:refs/heads/pr-ci-task" in rule
                            for rule in second_allowed))

        codex = [json.loads(line) for line in codex_session[4].read_text(encoding="utf-8").splitlines()]
        turns = [row["params"] for row in codex if row["method"] == "turn/start"]
        self.assertEqual(len(turns), 2)
        self.assertEqual([turn["threadId"] for turn in turns], ["thread-same-owner"] * 2)
        self.assertEqual(turns[0]["sandboxPolicy"]["type"], "readOnly")
        self.assertFalse(turns[0]["sandboxPolicy"]["networkAccess"])
        self.assertEqual(turns[1]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertTrue(turns[1]["sandboxPolicy"]["networkAccess"])
        self.assertIn(str(self.repo), turns[1]["sandboxPolicy"]["writableRoots"])

        opencode = [json.loads(line) for line in opencode_session[2].read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("--session", opencode[0]["argv"])
        self.assertIn("--session", opencode[1]["argv"])
        self.assertEqual(opencode[1]["argv"][opencode[1]["argv"].index("--session") + 1],
                         "ses_fixture")
        active_config, settled_config = [row["config"] for row in opencode]
        self.assertEqual(active_config["permission"]["edit"]["*"], "deny")
        self.assertEqual(active_config["permission"]["bash"]["*"], "deny")
        self.assertNotEqual(settled_config["permission"].get("edit"), "deny")
        self.assertTrue(any("gh pr create" in command and value == "allow"
                            for command, value in settled_config["permission"]["bash"].items()))

        for harness, attempt in attempts.items():
            rows = self.jobs[harness].read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(f"attempt_id={attempt}" in row for row in rows), 1, rows)

    def test_codex_named_profile_reprojects_owner_scope_on_the_same_thread(self):
        # The session starts with a verified complete owner route while Git is
        # idle, so its native profile is genuinely active before the conflict.
        self._git("merge", "--abort")
        primary_repo = self.repo
        self.repo = self.root / "codex-owner-worktree"
        subprocess.run(["git", "-C", str(primary_repo), "worktree", "add", "-q", "-b",
                        "pr-ci-projection", str(self.repo), "pr-ci-task"], check=True,
                       capture_output=True, text=True)
        self.route = self._compile_complete_owner_route()
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        attempt = self._register_owner("codex")
        runtime_env = self._runtime_context("codex", attempt)
        module = load_module("fixture_codex_named_profile_supervisor",
                             ROOT / "utilities/codex-app-server-supervisor.py")
        owner_args = argparse.Namespace(
            parent_attempt_id=attempt, worktree=str(self.repo), writable_root=[],
            base_writable_root=[], sandbox="workspace-write", network_access=False,
        )
        with mock.patch.dict(os.environ, runtime_env, clear=True):
            module.apply_owner_runtime_permissions(owner_args)
        initial = owner_args.owner_runtime_permission_state
        self.assertFalse(initial["observation_only"])
        self.assertTrue(initial["may_commit"])
        self.assertEqual(initial["task_publish_scope"]["branch"], "pr-ci-projection")

        # Exercise the real profile builder while stubbing only its optional
        # installed-Codex feature probe; no native Codex process is launched.
        import codex_permission_profile
        with mock.patch.object(codex_permission_profile, "named_profiles_available",
                               return_value=True):
            profile = module.commit_profile_config(
                str(self.repo), owner_args.writable_root, owner_args.sandbox,
                owner_args.network_access,
            )
        self.assertIsNotNone(profile)
        self.assertEqual(profile["default_permissions"], codex_permission_profile.PROFILE_NAME)
        self.assertTrue(profile["permissions"][codex_permission_profile.PROFILE_NAME]
                        ["network"]["enabled"])
        profile_filesystem = profile["permissions"][codex_permission_profile.PROFILE_NAME]["filesystem"]
        self.assertEqual(profile_filesystem[str((primary_repo / ".git").resolve())], "read")
        self.assertTrue(all(profile_filesystem[path] == "write"
                            for path in owner_args.writable_root))

        session = self._codex_session(
            runtime_env, attempt, named_profile=profile, approval="on-request")
        self.assertEqual(session[1].native_permission_profile, profile)
        self.assertEqual(session[5][2:], module.config_arguments(profile))

        conflict = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-commit", "--no-ff",
             "merge-incoming"], capture_output=True, text=True)
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        self.assertIn("MERGE_HEAD", self.merge_snapshot()[1])
        active_merge = self.merge_snapshot()
        self._codex_turn(session, runtime_env, prompt="read-only during active merge")
        self.assertTrue(session[1].owner_runtime_permission_state["observation_only"])
        self.assertEqual(self.merge_snapshot(), active_merge)

        # Only this fixture process settles the conflict; the same app-server
        # thread then receives the current approved task/Git scope.
        (self.repo / "engine.py").write_text("value = 'fixture resolution'\n", encoding="utf-8")
        self._git("add", "engine.py")
        self._git("commit", "-q", "-m", "settle named-profile fixture merge")
        self.assertNotIn("MERGE_HEAD", self.merge_snapshot()[1])
        settled = commit_policy.runtime_permission_projection(argparse.Namespace(
            worker_type="owner", dispatch_depth=1, capability="autopilot-code",
            worktree=str(self.repo), route_file=str(self.route_file),
            route_id=self.route["route_id"], route_hash=self.route["route_hash"],
            artifact_root=str(self.artifact_root), artifact_output_dir=str(self.output_dir),
        ))
        self.assertFalse(settled["observation_only"])
        self.assertEqual(settled["task_publish_scope"]["branch"], "pr-ci-projection")
        self._codex_turn(session, runtime_env, prompt="continue with approved task scope")

        codex = [json.loads(line) for line in session[4].read_text(encoding="utf-8").splitlines()]
        thread_starts = [row["params"] for row in codex if row["method"] == "thread/start"]
        turns = [row["params"] for row in codex if row["method"] == "turn/start"]
        self.assertEqual(len(thread_starts), 1)
        self.assertEqual(thread_starts[0]["approvalPolicy"], "on-request")
        self.assertNotIn("sandbox", thread_starts[0])
        self.assertEqual(len(turns), 2)
        self.assertEqual([turn["threadId"] for turn in turns], [session[3], session[3]])
        for turn in turns:
            self.assertNotIn("approvalPolicy", turn)
        self.assertEqual(turns[0]["sandboxPolicy"], {
            "type": "readOnly", "networkAccess": False,
        })
        git_roots = [str(path) for path in commit_policy.commit_git_metadata_dirs(
            argparse.Namespace(worker_type="owner", dispatch_depth=1,
                               worktree=str(self.repo)))
        ]
        self.assertEqual(turns[1]["sandboxPolicy"], {
            "type": "workspaceWrite",
            "writableRoots": [str(self.repo), *git_roots],
            "networkAccess": True,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        })
        self.assertFalse(session[1].owner_runtime_permission_state["observation_only"])


if __name__ == "__main__":
    unittest.main()
