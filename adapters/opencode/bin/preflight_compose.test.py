#!/usr/bin/env python3
"""OpenCode `preflight.sh compose` bind parity (SD-135; canary review round 2, M3).

Claude/Codex bind a `compose` route through the PostToolUse hook that reads the
sealed route off stdout. OpenCode has its own shell bind rule, so this suite
pins that rule end to end: a `compose` with no `--output` binds the canonical
route, an explicit `--output` still binds, a failed or non-JSON compile binds
nothing and keeps its exit status, and no session id means no bind.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"
GUARD_SPEC = importlib.util.spec_from_file_location("material_route_guard_for_opencode", ROOT / "hooks" / "material-route-guard.py")
assert GUARD_SPEC and GUARD_SPEC.loader
GUARD = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(GUARD)


class OpenCodeComposeBindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "Test"]):
            subprocess.run(["git", "-C", str(self.repo)] + args, check=True)
        (self.repo / "app.py").write_text("print('one')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.markers: list[Path] = []
        self.addCleanup(self._unlink_markers)

    def _unlink_markers(self) -> None:
        for path in self.markers:
            path.unlink(missing_ok=True)

    def _session(self, name: str) -> str:
        session = f"oc-compose-{name}-{os.getpid()}"
        self.markers.append(GUARD.marker_path(ROOT, session))
        return session

    def _run(self, *args: str, session: str | None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "AGENT_HOME": str(ROOT)}
        env.pop("OPENCODE_SESSION_ID", None)
        if session:
            env["OPENCODE_SESSION_ID"] = session
        return subprocess.run([str(PREFLIGHT), *args], text=True, capture_output=True, env=env)

    def _marker(self, session: str) -> dict | None:
        path = GUARD.marker_path(ROOT, session)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_compose_without_output_binds_the_canonical_route(self) -> None:
        session = self._session("noout")
        result = self._run("compose", "--slug", "oc-noout", "--cwd", str(self.repo), session=session)
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(route["selection"]["route_origin"], "compose")
        canonical = Path(route["artifact_root"]) / ".runtime" / "routes" / f"{route['route_id']}.json"
        self.assertTrue(canonical.is_file())
        marker = self._marker(session)
        self.assertIsNotNone(marker, "no bind marker written")
        self.assertEqual(marker["route_id"], route["route_id"])
        self.assertEqual(Path(marker["route_file"]).resolve(), canonical.resolve())
        self.assertEqual(Path(marker["cwd"]).resolve(), self.repo.resolve())
        # stdout is echoed back unchanged: the route JSON is the last line
        self.assertIn(route["route_id"], result.stdout)

    def test_compose_without_cwd_uses_the_sealed_cwd(self) -> None:
        session = self._session("nocwd")
        env = {**os.environ, "AGENT_HOME": str(ROOT), "OPENCODE_SESSION_ID": session}
        result = subprocess.run([str(PREFLIGHT), "compose", "--slug", "oc-nocwd"], text=True,
                                capture_output=True, env=env, cwd=str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout.strip().splitlines()[-1])
        marker = self._marker(session)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["route_id"], route["route_id"])
        self.assertEqual(Path(marker["cwd"]).resolve(), self.repo.resolve())

    def test_compose_with_explicit_output_still_binds(self) -> None:
        session = self._session("out")
        explain = self._run("compose", "--slug", "oc-out", "--cwd", str(self.repo), "--explain", session=session)
        self.assertEqual(explain.returncode, 0, explain.stderr)
        self.assertIsNone(self._marker(session), "--explain must not bind")
        route_id = json.loads(explain.stdout.strip().splitlines()[-1])["route_id"]
        output = self.repo / ".agent_reports" / ".runtime" / "routes" / f"{route_id}.json"
        result = self._run("compose", "--slug", "oc-out", "--cwd", str(self.repo), "--output", str(output), session=session)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(session)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["route_id"], route_id)

    def test_failed_compose_binds_nothing_and_keeps_its_exit_status(self) -> None:
        session = self._session("fail")
        result = self._run("compose", "--slug", "oc-fail", "--cwd", str(self.repo), "--shape", "direct",
                           "--graph", "execute", session=session)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compose-graph-only-staged", result.stderr)
        self.assertIsNone(self._marker(session))

    def test_no_session_id_binds_nothing(self) -> None:
        session = self._session("nosid")
        result = self._run("compose", "--slug", "oc-nosid", "--cwd", str(self.repo), session=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self._marker(session))
        self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["route_id"].startswith("rt-"))


if __name__ == "__main__":
    unittest.main()
