#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from execution_access import (
    AccessContext,
    ExecutionAccessError,
    ParentGrant,
    adapter_default_roots,
    assert_within_parent,
    build_grant,
    load_request,
    load_parent_effective_grant,
    receipt_fields,
    request_path,
    publish_effective_grant,
    prepare_task_request,
    read_roots_data,
    resolve_task_targets,
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

    def lab_route(self, **changes: object):
        route = {
            "route_id": "rt-lab-access",
            "route_hash": "sha256:" + "a" * 64,
            "capability": "autopilot-lab",
            "cwd": str(self.worktree),
            "artifact_root": str(self.artifact),
            "work_request": {"text": "Use /data/unapproved-text for the experiment"},
        }
        route.update(changes)
        return route

    def inventory(self, run_root: Path) -> Path:
        path = self.root / "compute-hosts.yaml"
        path.write_text(
            f"schema_version: 1\nrun_root: {run_root}\nhosts:\n"
            "  fixture:\n    ssh_host: local\n",
            encoding="utf-8",
        )
        return path

    def test_lab_owner_uses_exact_inventory_root_without_creating_run_storage(self) -> None:
        run_root = self.root / "custom-inventory" / "runs"
        inventory = self.inventory(run_root)
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            prepared = prepare_task_request(self.lab_route(), self.state / "jobs.log")
            again = prepare_task_request(self.lab_route(), self.state / "jobs.log")
        self.assertEqual(prepared, again)
        request = load_request(prepared, context=self.context)
        self.assertEqual((run_root,), request.writable_roots)
        self.assertFalse(run_root.exists())
        self.assertNotIn(Path("/data/unapproved-text"), request.writable_roots)

    def test_inventory_default_is_not_given_to_code_owners_or_frames(self) -> None:
        inventory = self.inventory(self.root / "runs")
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            self.assertIsNone(prepare_task_request(
                self.lab_route(capability="autopilot-code"), self.state / "jobs.log"))
            self.assertIsNone(prepare_task_request(
                self.lab_route(), self.state / "jobs.log", node="frame"))
        self.assertFalse((self.state / "execution-access").exists())

    def test_missing_or_template_inventory_does_not_guess_a_lab_root(self) -> None:
        inventory = self.root / "compute-hosts.yaml"
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            self.assertIsNone(prepare_task_request(self.lab_route(), self.state / "jobs.log"))
            inventory.write_text("schema_version: 1\nhosts:\n# uninitialized\n", encoding="utf-8")
            self.assertIsNone(prepare_task_request(self.lab_route(), self.state / "jobs.log"))
        self.assertFalse((self.state / "execution-access").exists())

    def test_invalid_inventory_and_broad_run_root_refuse_before_publishing(self) -> None:
        inventory = self.inventory(self.home)
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
            self.assertTrue(raised.exception.reason.startswith("execution-access-root-too-broad:"))
            inventory.write_text("schema_version: 2\n", encoding="utf-8")
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
            self.assertEqual("execution-access-compute-inventory-invalid", raised.exception.reason)
        self.assertFalse((self.state / "execution-access").exists())

    def test_lab_explicit_data_request_is_validated_preserved_and_delivered(self) -> None:
        run_root = self.root / "runs"
        inventory = self.inventory(run_root)
        supplied = self.request(network={"required": True, "reason": "approved transfer", "hosts": ["fixture.invalid:22"]})
        original = self.request_file.read_bytes()
        # Explicit data wins over a malformed old preview-table reference.
        route = self.lab_route(work_request={"text": "## 입력\n- 루트 목록과 경로: unsupported ROOTS input\n"})
        with mock.patch.dict(os.environ, {
            **self.env, "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file),
        }, clear=True):
            prepared = prepare_task_request(route, self.state / "jobs.log")
        merged = load_request(prepared, context=self.context)
        self.assertEqual(set((*supplied.writable_roots, run_root)), set(merged.writable_roots))
        self.assertEqual(supplied.read_roots, merged.read_roots)
        self.assertEqual(supplied.network_hosts, merged.network_hosts)
        self.assertEqual(supplied.network_reason, merged.network_reason)
        self.assertEqual(supplied.enforcement_required, merged.enforcement_required)
        self.assertEqual(original, self.request_file.read_bytes())

    def test_invalid_explicit_data_does_not_publish_partial_lab_grant(self) -> None:
        inventory = self.inventory(self.root / "runs")
        self.request_file.write_text("{", encoding="utf-8")
        with mock.patch.dict(os.environ, {
            **self.env, "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file),
        }, clear=True):
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)
        self.assertFalse((self.state / "execution-access").exists())

    def test_lab_resource_request_cannot_expand_the_live_parent_grant(self) -> None:
        inventory = self.inventory(self.root / "runs")
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            prepared = prepare_task_request(self.lab_route(), self.state / "jobs.log")
        request = load_request(prepared, context=self.context)
        parent = ParentGrant(writable_roots=request.writable_roots)
        assert_within_parent(request, parent, is_child=True)
        outside = self.request(read_roots=[], writable_roots=[str(self.root / "outside")], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(outside, parent, is_child=True)
        self.assertTrue(raised.exception.reason.startswith("execution-access-exceeds-parent:"))

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

    def test_host_ports_are_typed_and_hash_canonical(self) -> None:
        first = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "bounded connect",
                "hosts": ["CNN.example:00022", "[2001:0DB8::1]:00443"],
            },
            justification={},
        )
        second = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "bounded connect",
                "hosts": ["cnn.example:22", "[2001:db8::1]:443"],
            },
            justification={},
        )
        self.assertEqual(
            ("[2001:db8::1]:443", "cnn.example:22"), first.network_hosts
        )
        self.assertEqual(first.request_sha256, second.request_sha256)

        for host in (
            "cnn.example:ssh",
            "cnn.example:",
            "cnn.example:+22",
            "cnn.example:-22",
            "cnn.example:０２２",
            "cnn.example:0",
            "cnn.example:65536",
            ":22",
            "[2001:db8::1]:",
            "[2001:db8::1]:ssh",
        ):
            with self.subTest(host=host):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(
                        read_roots=[],
                        network={
                            "required": True,
                            "reason": "bounded connect",
                            "hosts": [host],
                        },
                        justification={},
                    )
                self.assertEqual(
                    "execution-access-field-invalid:network.hosts",
                    raised.exception.reason,
                )

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

    def test_special_request_file_is_rejected_before_nonblocking_open(self) -> None:
        fifo = self.root / "request.fifo"
        os.mkfifo(fifo)
        with mock.patch("execution_access.os.open") as opened:
            with self.assertRaises(ExecutionAccessError) as raised:
                load_request(fifo, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)
        opened.assert_not_called()

        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(Path(str(self.root / "request") + "\0.json"), context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)

        self.request(read_roots=[], justification={})
        real_open = os.open
        with mock.patch("execution_access.os.open", wraps=real_open) as opened:
            load_request(self.request_file, context=self.context)
        request_open = next(
            call for call in opened.call_args_list if Path(call.args[0]) == self.request_file
        )
        self.assertTrue(request_open.args[1] & getattr(os, "O_NONBLOCK", 0))

    def test_resolve_failures_and_deep_json_are_typed(self) -> None:
        loop_a = self.root / "loop-a"
        loop_b = self.root / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(writable_roots=[str(loop_a)], read_roots=[], justification={})
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-invalid:")
        )

        valid = {
            "schema_version": 1,
            "writable_roots": [str(self.root / "scoped")],
            "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any",
            "justification": {},
        }
        self.request_file.write_text(json.dumps(valid), encoding="utf-8")
        with mock.patch.object(Path, "resolve", side_effect=OSError("resolver failed")):
            with self.assertRaises(ExecutionAccessError) as raised:
                load_request(self.request_file, context=self.context)
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-invalid:")
        )

        self.request_file.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)

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

    def test_roots_reader_selects_only_named_rows_from_data_block(self) -> None:
        manifest = self.root / "run_all.sh"
        selected = tuple(f"project-{index}" for index in range(12))
        rows = [f"{name}|{self.root}/external/{index}/.agent_reports"
                for index, name in enumerate(selected)]
        rows.append(f"unrequested-13|{self.root}/external/13/.agent_reports")
        manifest.write_text(
            "#!/bin/sh\ndone <<'ROOTS'\n" + "\n".join(rows) + "\nROOTS\n",
            encoding="utf-8",
        )
        targets = read_roots_data(manifest, selected)
        self.assertEqual(selected, targets.selected_names)
        self.assertEqual(12, len(targets.writable_roots))
        self.assertNotIn(self.root / "external/13/.agent_reports", targets.writable_roots)
        self.assertEqual(64, len(targets.manifest_sha256))

    def linked_roots_fixture(self):
        primary = self.root / "primary"
        linked = self.root / "linked"
        primary.mkdir()
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "fixture-linked", str(linked)], check=True)
        canonical = primary / ".agent_reports"
        canonical.mkdir()
        return primary, linked, canonical

    @staticmethod
    def target_route(cwd: Path, artifact_root: Path, *, preview=".agent_reports/_scratch/flow"):
        return {
            "route_id": "rt-test",
            "route_hash": "sha256:" + "a" * 64,
            "cwd": str(cwd),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                f"- 미리보기(사용자가 본 것): {preview}/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
            )},
        }

    def test_task_target_resolution_uses_canonical_artifact_root_for_linked_worktree(self) -> None:
        primary, linked, canonical = self.linked_roots_fixture()
        manifest = canonical / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("done <<'ROOTS'\nalpha|/tmp/canonical/.agent_reports\nROOTS\n", encoding="utf-8")
        # A same-named linked-worktree shadow carries different authority and must be ignored.
        shadow = linked / ".agent_reports/_scratch/flow/previews/run_all.sh"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("done <<'ROOTS'\nalpha|/tmp/shadow/.agent_reports\nROOTS\n", encoding="utf-8")
        route = self.target_route(linked, canonical)
        targets = resolve_task_targets(route)
        self.assertEqual(canonical.resolve(), targets.manifest_path.parent.parent.parent.parent)
        self.assertEqual((Path("/tmp/canonical/.agent_reports"),), targets.writable_roots)

    def test_missing_canonical_roots_never_falls_back_to_linked_shadow(self) -> None:
        _, linked, canonical = self.linked_roots_fixture()
        shadow = linked / ".agent_reports/_scratch/flow/previews/run_all.sh"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("done <<'ROOTS'\nalpha|/tmp/shadow/.agent_reports\nROOTS\n", encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(self.target_route(linked, canonical))
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        self.assertIn("No such file", raised.exception.detail)

    def test_canonical_roots_symlink_is_not_followed(self) -> None:
        _, linked, canonical = self.linked_roots_fixture()
        real = canonical / "_scratch/flow/previews/real-roots.sh"
        real.parent.mkdir(parents=True)
        real.write_text("done <<'ROOTS'\nalpha|/tmp/outside/.agent_reports\nROOTS\n", encoding="utf-8")
        manifest = canonical / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.symlink_to(real)
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(self.target_route(linked, canonical))
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        self.assertIn("non-symlink regular file", raised.exception.detail)

    def test_source_relative_target_keeps_route_cwd_and_noninput_prose_has_no_authority(self) -> None:
        primary, linked, canonical = self.linked_roots_fixture()
        manifest = linked / "source/_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("done <<'ROOTS'\nalpha|/tmp/source/.agent_reports\nROOTS\n", encoding="utf-8")
        route = self.target_route(linked, canonical, preview="source/_scratch/flow")
        targets = resolve_task_targets(route)
        self.assertEqual(manifest.resolve(), targets.manifest_path)
        self.assertEqual((Path("/tmp/source/.agent_reports"),), targets.writable_roots)
        route["work_request"] = {"text": "Quoted old task: .agent_reports/_scratch/flow previews/run_all.sh ROOTS(alpha)"}
        self.assertIsNone(resolve_task_targets(route))
        route["work_request"] = {"text": (
            "## 입력\n"
            "- 미리보기(사용자가 본 것): .agent_reports-copy/_scratch/flow/previews/<루트>.md\n"
            "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
        )}
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(route)
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        route["work_request"] = {"text": (
            "## 입력\n"
            "- 미리보기(사용자가 본 것): source/_scratch/flow/previews/<루트>.md\n"
            "- 루트 목록과 경로: other/run.sh 의 ROOTS 표(alpha)\n"
        )}
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(route)
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)

    def test_prepared_request_is_route_bound_and_refuses_changed_manifest(self) -> None:
        _, cwd, artifact_root = self.linked_roots_fixture()
        manifest = artifact_root / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "done <<'ROOTS'\nalpha|/tmp/alpha/.agent_reports\nROOTS\n",
            encoding="utf-8",
        )
        route = {
            "route_id": "rt-test",
            "route_hash": "sha256:" + "a" * 64,
            "cwd": str(cwd),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                "- 미리보기(사용자가 본 것): .agent_reports/_scratch/flow/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
            )},
        }
        jobs = self.state / "jobs.log"
        request_file = prepare_task_request(route, jobs)
        self.assertEqual(request_file, prepare_task_request(route, jobs))
        request = json.loads(request_file.read_text(encoding="utf-8"))
        self.assertEqual(["/tmp/alpha/.agent_reports"], request["writable_roots"])
        binding = json.loads(request_file.with_name("binding.json").read_text(encoding="utf-8"))
        self.assertEqual(str(artifact_root.resolve()), binding["artifact_root"])
        self.assertEqual([], request["read_roots"])
        manifest.write_text(
            "done <<'ROOTS'\nalpha|/tmp/alpha/.agent_reports\nbeta|/tmp/beta/.agent_reports\nROOTS\n",
            encoding="utf-8",
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            prepare_task_request(route, jobs)
        self.assertEqual("execution-access-cache-conflict", raised.exception.reason)

    def test_parent_effective_record_is_full_digest_bound_to_live_attempt(self) -> None:
        request = self.request(read_roots=[], justification={})
        grant = build_grant(
            request, runtime="codex-exec", default_writable_roots=(self.worktree,),
            network_available=False, effective_sandbox="workspace-write",
        )
        jobs = self.state / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        parent_id = "att-parent"
        route_id = "rt-parent"
        route_hash = "sha256:" + "b" * 64
        effective_path, digest = publish_effective_grant(
            jobs=jobs, attempt_id=parent_id, route_id=route_id,
            route_hash=route_hash, runtime="codex-exec", sandbox="workspace-write",
            grant=grant, default_writable_roots=(self.worktree,), network_allowed=False,
        )
        metadata = {
            "attempt_id": parent_id, "route_id": route_id, "route_hash": route_hash,
            "runtime_sandbox": "workspace-write",
            "execution_access_effective_file": str(effective_path),
            "execution_access_effective_sha256": digest,
        }
        jobs.write_text(
            "now\topen\t12\tparent\tparent-slug\t"
            + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8",
        )
        parent = load_parent_effective_grant(
            jobs=jobs, parent_attempt_id=parent_id, context=self.context,
        )
        self.assertIn(self.worktree, parent.writable_roots)
        self.assertIn(self.root / "scoped" / "write", parent.writable_roots)
        self.assertFalse(parent.network_allowed)

        metadata["execution_access_effective_sha256"] = "0" * 64
        jobs.write_text(
            "now\topen\t12\tparent\tparent-slug\t"
            + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            load_parent_effective_grant(
                jobs=jobs, parent_attempt_id=parent_id, context=self.context,
            )
        self.assertEqual("execution-access-parent-record-digest-mismatch", raised.exception.reason)

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

    def test_codex_unprojectable_writes_and_strict_hosts_are_refused(self) -> None:
        writable = self.request(read_roots=[], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                writable,
                runtime="codex-exec",
                effective_sandbox="read-only",
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-read-only",
            raised.exception.reason,
        )

        strict_hosts = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "strict destination",
                "hosts": ["example.com:443"],
            },
            enforcement_required="os-sandbox",
            justification={},
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                strict_hosts,
                runtime="codex-app-server",
                network_available=True,
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-network-hosts",
            raised.exception.reason,
        )


if __name__ == "__main__":
    unittest.main()
