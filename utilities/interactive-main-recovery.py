#!/usr/bin/env python3
"""Checked recovery for a visible Codex main in a Herdr pane.

This utility deliberately has one host integration: Herdr's pane/agent API.
It never searches for or falls back to tmux. ``--check`` only reads the current
pane inventory; ``--start`` repeats that proof, creates a visible pane through
the same native workspace/tab host, and asks Herdr to start the protected
managed Codex launcher there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable


EXIT_INVALID = 64
EXIT_UNAVAILABLE = 65


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RecoveryError(RuntimeError):
    def __init__(
        self, reason: str, detail: str = "", *, created_pane: str = ""
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.created_pane = created_pane


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError("herdr-invocation-failed", str(exc)) from exc


def validate_herdr_socket(path: str | None = None) -> Path:
    raw = path or os.environ.get("HERDR_SOCKET_PATH", "")
    if not raw:
        raise RecoveryError("herdr-socket-required", "HERDR_SOCKET_PATH is required")
    socket_path = Path(raw).expanduser()
    try:
        info = socket_path.lstat()
    except OSError as exc:
        raise RecoveryError("herdr-socket-unavailable", str(exc)) from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise RecoveryError("herdr-socket-not-socket", str(socket_path))
    if info.st_uid != os.geteuid():
        raise RecoveryError("herdr-socket-owner-mismatch", str(socket_path))
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RecoveryError("herdr-socket-mode-unsafe", str(socket_path))
    return socket_path.resolve(strict=True)


def protected_launcher(state_path: Path) -> Path:
    try:
        state_info = state_path.lstat()
    except OSError as exc:
        raise RecoveryError("launcher-state-unavailable", str(exc)) from exc
    if (
        not stat.S_ISREG(state_info.st_mode)
        or state_info.st_uid != os.geteuid()
        or stat.S_IMODE(state_info.st_mode) & 0o077
    ):
        raise RecoveryError("launcher-state-unsafe", str(state_path))
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RecoveryError("launcher-state-invalid", str(exc)) from exc
    if not isinstance(value, dict) or value.get("phase") != "installed":
        raise RecoveryError("launcher-state-incomplete", str(state_path))
    raw = value.get("ingress_path") or value.get("wrapper_path")
    if not isinstance(raw, str) or not raw:
        raise RecoveryError("launcher-ingress-missing", str(state_path))
    launcher = Path(raw)
    if not launcher.is_absolute():
        raise RecoveryError("launcher-ingress-invalid", raw)
    try:
        info = launcher.lstat()
        payload = launcher.read_bytes()
    except OSError as exc:
        raise RecoveryError("launcher-ingress-unavailable", str(exc)) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(launcher, os.X_OK)
    ):
        raise RecoveryError("launcher-ingress-unsafe", str(launcher))
    expected = value.get("ingress_sha256") or value.get("wrapper_sha256")
    if not isinstance(expected, str) or hashlib.sha256(payload).hexdigest() != expected:
        raise RecoveryError("launcher-ingress-digest-mismatch", str(launcher))
    if b"codex-launcher.py" not in payload:
        raise RecoveryError("launcher-ingress-unmanaged", str(launcher))
    return launcher


def herdr_panes(
    *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[dict[str, Any]]:
    if which("herdr") is None:
        raise RecoveryError("herdr-not-found", "the checked recovery surface requires Herdr")
    result = runner(["herdr", "pane", "list"])
    if result.returncode != 0:
        raise RecoveryError("herdr-pane-list-failed", (result.stderr or result.stdout).strip())
    try:
        payload = json.loads(result.stdout or "")
        panes = (payload.get("result") or {}).get("panes")
    except (ValueError, AttributeError, TypeError) as exc:
        raise RecoveryError("herdr-pane-list-malformed", "Herdr returned no pane inventory") from exc
    if not isinstance(panes, list) or not all(isinstance(item, dict) for item in panes):
        raise RecoveryError("herdr-pane-list-malformed", "Herdr pane inventory has the wrong shape")
    return panes


def herdr_pane(
    pane_id: str, *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    if which("herdr") is None:
        raise RecoveryError("herdr-not-found", "the checked recovery surface requires Herdr")
    result = runner(["herdr", "pane", "get", pane_id])
    if result.returncode != 0:
        raise RecoveryError("herdr-pane-get-failed", (result.stderr or result.stdout).strip())
    try:
        payload = json.loads(result.stdout or "")
        pane = (payload.get("result") or {}).get("pane")
    except (ValueError, AttributeError, TypeError) as exc:
        raise RecoveryError("herdr-pane-get-malformed", "Herdr returned no pane evidence") from exc
    if not isinstance(pane, dict):
        raise RecoveryError("herdr-pane-get-malformed", "Herdr pane evidence has the wrong shape")
    if _field(pane, "pane_id", "paneId", "id") != pane_id:
        raise RecoveryError("pane-id-mismatch", f"expected Herdr pane {pane_id!r}")
    return pane


def _field(pane: dict[str, Any], *names: str) -> str:
    for name in names:
        value = pane.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def select_pane(panes: list[dict[str, Any]], pane_id: str) -> dict[str, Any]:
    matches = [pane for pane in panes if _field(pane, "pane_id", "paneId", "id") == pane_id]
    if len(matches) != 1:
        raise RecoveryError("pane-not-found", f"expected one Herdr pane {pane_id!r}")
    pane = matches[0]
    expected_workspace = os.environ.get("HERDR_WORKSPACE_ID", "")
    expected_tab = os.environ.get("HERDR_TAB_ID", "")
    workspace = _field(pane, "workspace_id", "workspaceId")
    tab = _field(pane, "tab_id", "tabId")
    if expected_workspace and workspace != expected_workspace:
        raise RecoveryError("workspace-mismatch", "the pane is not in the expected Herdr workspace")
    if expected_tab and tab != expected_tab:
        raise RecoveryError("tab-mismatch", "the pane is not in the expected Herdr tab")
    return pane


def checked_pane(
    pane_id: str, *, workspace: str | None = None, socket_path: str | None = None,
    runner=_run, which=shutil.which,
) -> dict[str, Any]:
    validate_herdr_socket(socket_path)
    pane = select_pane(herdr_panes(runner=runner, which=which), pane_id)
    result = {
        "pane_id": _field(pane, "pane_id", "paneId", "id"),
        "workspace_id": _field(pane, "workspace_id", "workspaceId"),
        "tab_id": _field(pane, "tab_id", "tabId"),
        "cwd": _field(pane, "cwd", "working_directory", "workingDirectory"),
        "agent": pane.get("agent"),
    }
    if not result["cwd"]:
        raise RecoveryError("pane-cwd-missing", pane_id)
    if workspace is not None:
        expected = Path(workspace).expanduser().resolve()
        observed = Path(result["cwd"]).expanduser().resolve()
        if observed != expected:
            raise RecoveryError("pane-cwd-mismatch", f"expected {expected}, observed {observed}")
    return result


def start(args: argparse.Namespace, *, runner=_run, which=shutil.which) -> dict[str, Any]:
    # Re-read the source pane at the mutation boundary, then use only this
    # host's native visible-pane API. Headless workers use a different path.
    workspace = str(Path(args.workspace).expanduser().resolve())
    source_pane = checked_pane(
        args.pane, workspace=workspace, socket_path=args.socket,
        runner=runner, which=which,
    )
    launcher = protected_launcher(Path(args.launcher_state).expanduser())
    split = runner([
        "herdr", "pane", "split", "--pane", args.pane, "--direction", "right",
        "--cwd", workspace, "--focus",
    ])
    if split.returncode != 0:
        raise RecoveryError("herdr-pane-split-failed", (split.stderr or split.stdout).strip())
    try:
        split_payload = json.loads(split.stdout or "")
        split_pane = (split_payload.get("result") or {}).get("pane")
        created_pane = _field(split_pane, "pane_id", "paneId", "id")
    except (ValueError, AttributeError, TypeError):
        created_pane = ""
    if not created_pane:
        raise RecoveryError("herdr-pane-split-malformed", "Herdr returned no created pane ID")
    try:
        pane = herdr_pane(created_pane, runner=runner, which=which)
        if _field(pane, "workspace_id", "workspaceId") != source_pane["workspace_id"]:
            raise RecoveryError("workspace-mismatch", "the created pane left the source workspace")
        if _field(pane, "tab_id", "tabId") != source_pane["tab_id"]:
            raise RecoveryError("tab-mismatch", "the created pane left the source tab")
        created_cwd = _field(pane, "cwd", "working_directory", "workingDirectory")
        if not created_cwd or Path(created_cwd).expanduser().resolve() != Path(workspace):
            raise RecoveryError("pane-cwd-mismatch", f"expected created pane cwd {workspace}")
    except RecoveryError as exc:
        raise RecoveryError(
            exc.reason, exc.detail, created_pane=created_pane
        ) from exc
    command = ["herdr", "agent", "start", args.name, "--kind", "codex", "--pane", created_pane,
               "--", str(launcher), "--cd", workspace, *args.agent_args]
    result = runner(command)
    if result.returncode != 0:
        raise RecoveryError(
            "herdr-agent-start-failed", (result.stderr or result.stdout).strip(),
            created_pane=created_pane,
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("error"):
        raise RecoveryError(
            "herdr-agent-start-rejected", str(payload["error"]), created_pane=created_pane
        )
    return {"status": "started", "pane": pane, "workspace": workspace, "herdr": payload}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="interactive-main-recovery")
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--start", action="store_true")
    value.add_argument("--pane", default=os.environ.get("HERDR_PANE_ID", ""))
    value.add_argument("--socket", default=os.environ.get("HERDR_SOCKET_PATH", ""))
    value.add_argument("--name", default="hearting-managed-main")
    value.add_argument("--workspace", default=os.getcwd())
    value.add_argument(
        "--launcher-state",
        default=str(Path.home() / ".codex" / ".harness" / "codex-launcher.json"),
    )
    value.add_argument("agent_args", nargs=argparse.REMAINDER)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.pane:
        print(_json({"status": "blocked", "reason": "pane-id-required"}))
        return EXIT_INVALID
    if args.agent_args[:1] == ["--"]:
        args.agent_args = args.agent_args[1:]
    try:
        if args.check:
            print(_json({
                "status": "ready", "mode": "check",
                "pane": checked_pane(
                    args.pane, workspace=args.workspace, socket_path=args.socket,
                ),
            }))
        else:
            print(_json(start(args)))
        return 0
    except RecoveryError as exc:
        result = {"status": "blocked", "reason": exc.reason, "detail": exc.detail}
        if exc.created_pane:
            result["created_pane"] = exc.created_pane
        print(_json(result))
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
