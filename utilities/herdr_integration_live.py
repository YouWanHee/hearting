#!/usr/bin/env python3
"""Bounded native Herdr integration evidence runner for an isolated test home.

This is an internal test driver. It writes only under ``--output-dir`` and the
supplied scratch project. It never copies credentials or records prompt text,
screen contents, or raw command output.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import shlex
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
MAX_TIMEOUT = 900
CASES = ("callbacks", "codex-async-local-skip", "codex-busy",
         "opencode-busy", "booked-seat", "events-lost")
RUNTIMES = ("claude", "codex", "opencode")
_SECRET_ENV = re.compile(r"(?:API[_-]?KEY|ACCESS[_-]?TOKEN|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)

_LIVE_PATH = Path(__file__).with_name("herdr_opencode_live.py")
_LIVE_SPEC = importlib.util.spec_from_file_location("herdr_opencode_live_helpers", _LIVE_PATH)
_LIVE = importlib.util.module_from_spec(_LIVE_SPEC)
sys.modules[_LIVE_SPEC.name] = _LIVE
_LIVE_SPEC.loader.exec_module(_LIVE)


class LiveRunError(RuntimeError):
    pass


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _result(value):
    return value.get("result") if isinstance(value, dict) else None


def _pane_ids(value):
    found = set()
    def visit(node):
        if isinstance(node, dict):
            pane = node.get("pane_id")
            if isinstance(pane, str) and pane:
                found.add(pane)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return found


def _session_id(value):
    result = _result(value)
    agent = result.get("agent") if isinstance(result, dict) else None
    session = agent.get("agent_session") if isinstance(agent, dict) else None
    sid = session.get("value") if isinstance(session, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _redact_argv(argv):
    """Keep exact safe argv while blanking URLs and free-form payloads."""
    raw = [str(item) for item in argv]
    sensitive_positions = {
        index + 3 for index in range(max(0, len(raw) - 3))
        if raw[index:index + 2] == ["agent", "prompt"]
    }
    out, redact_next = [], False
    payload_flags = {"--prompt", "--message", "--text", "--body", "--input"}
    for index, value in enumerate(raw):
        if index in sensitive_positions or redact_next:
            out.append("[redacted]")
            redact_next = False
        elif value in payload_flags:
            out.append(value)
            redact_next = True
        elif value in {"--attach-url", "--backend-url"}:
            out.append(value)
            redact_next = True
        elif "://" in value:
            out.extend(_LIVE._safe_argv([value]))
        else:
            out.append(value)
    return out


def _failure_reason(stderr, returncode, timed_out=False):
    if timed_out:
        return "command-timeout"
    text = (stderr or "").lower()
    if any(term in text for term in ("not logged in", "authentication", "unauthorized", "login required")):
        return "authentication-unavailable"
    if any(term in text for term in ("unknown command", "unsupported", "not supported")):
        return "native-surface-unsupported"
    if any(term in text for term in ("not found", "no such file", "executable unavailable")):
        return "executable-unavailable"
    return "exit-0" if returncode == 0 else f"exit-{returncode if returncode is not None else 'unknown'}"


class HerdrIntegrationLive:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output_dir).expanduser().resolve()
        self.project = Path(args.project).expanduser().resolve(strict=True)
        if not self.project.is_dir() or self.project.is_symlink():
            raise LiveRunError("scratch project must be an existing real directory")
        if self.project == ROOT or ROOT in self.project.parents:
            raise LiveRunError("scratch project must not be inside the Hearting source tree")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session = "hearting-live-" + uuid.uuid4().hex[:12]
        self.case_dir = self.output / self.session
        self.case_dir.mkdir(mode=0o700)
        self.commands_path = self.case_dir / "commands.jsonl"
        self.events_path = self.case_dir / "events.jsonl"
        self.result_path = self.case_dir / "result.json"
        self.project_root = self.project
        self.env = _LIVE.isolated_environment(self.case_dir, self.project)
        self.env.update({
            "AGENT_HOME": str(ROOT),
            "CODEX_HOME": str(Path(self.env["HOME"]) / ".codex"),
            "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude"),
            "OPENCODE_CONFIG_DIR": str(Path(self.env["XDG_CONFIG_HOME"]) / "opencode"),
            "AGENT_PEER_LEDGER_ROOT": str(Path(self.env["XDG_STATE_HOME"]) / "peer-ledger"),
            "HERDR_ENV": "1",
            "HERDR_SESSION": self.session,
            "AGENT_HERDR_SESSION": self.session,
            "HEARTING_HERDR_LIVE_ROOT": str(self.case_dir),
            "HEARTING_HERDR_LIVE_OBSERVATIONS": str(self.events_path),
        })
        for name in (self.env["CODEX_HOME"], self.env["CLAUDE_CONFIG_DIR"],
                     self.env["OPENCODE_CONFIG_DIR"]):
            Path(name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.herdr = shutil.which(args.herdr) if os.path.sep not in args.herdr else args.herdr
        self.native = {name: (shutil.which(getattr(args, name))
                              if os.path.sep not in getattr(args, name)
                              else getattr(args, name)) for name in RUNTIMES}
        self.started = time.monotonic()
        self.deadline = self.started + args.timeout
        self.salt = os.urandom(32)
        self.server = None
        self.panes = []
        self.transport = None
        self.decode_original = None
        self.event_capture = []
        self.cleanup = []
        self.attempts = []
        self.result = {
            "schema": "hearting-herdr-integration-live-v1",
            "case": args.case,
            "outcome": "unavailable",
            "started_at": _utc_now(),
            "named_session": self.session,
            "scratch_project": str(self.project),
            "case_root": str(self.case_dir),
            "isolation": {"home": self.env["HOME"], "xdg_config": self.env["XDG_CONFIG_HOME"],
                          "xdg_state": self.env["XDG_STATE_HOME"], "codex_home": self.env["CODEX_HOME"],
                          "claude_config": self.env["CLAUDE_CONFIG_DIR"],
                          "opencode_config": self.env["OPENCODE_CONFIG_DIR"],
                          "credentials_copied": False},
            "expected": {}, "observed": {}, "evidence_paths": [], "cleanup": self.cleanup,
        }

    def _append(self, path, row):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()

    def _remaining(self, limit=None):
        left = self.deadline - time.monotonic()
        if left <= 0:
            return 0.05
        return min(left, limit) if limit is not None else left

    def command(self, name, argv, *, cwd=None, timeout=None, env=None, input_text=None,
                native_output=False):
        actual = [str(item) for item in argv]
        started_at = _utc_now()
        started = time.monotonic()
        stdout = stderr = ""
        returncode = None
        timed_out = False
        try:
            done = subprocess.run(
                actual, cwd=cwd or self.project, env=env or self.env,
                input=input_text, stdin=subprocess.DEVNULL if input_text is None else None,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self._remaining(timeout or self.args.command_timeout), check=False,
            )
            stdout, stderr, returncode = done.stdout or "", done.stderr or "", done.returncode
        except FileNotFoundError as exc:
            stderr = type(exc).__name__
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        except OSError as exc:
            stderr = type(exc).__name__
        duration = time.monotonic() - started
        stdout_bytes = stdout.encode("utf-8", "replace")
        stderr_bytes = stderr.encode("utf-8", "replace")
        row = {
            "step": name, "argv": _redact_argv(actual), "started_at": started_at,
            "finished_at": _utc_now(), "returncode": returncode,
            "elapsed_ms": int(duration * 1000), "timed_out": timed_out,
            "stdout_bytes": len(stdout_bytes),
            "stdout_hmac": hmac.new(self.salt, stdout_bytes, hashlib.sha256).hexdigest(),
            "stderr_bytes": len(stderr_bytes),
            "stderr_hmac": hmac.new(self.salt, stderr_bytes, hashlib.sha256).hexdigest(),
            "stderr_reason": _failure_reason(stderr, returncode, timed_out),
            "native_output_not_recorded": bool(native_output),
        }
        self._append(self.commands_path, row)
        self.attempts.append(row)
        return returncode, stdout, stderr

    def herdr_command(self, name, args, *, timeout=None, with_session=True):
        if not self.herdr:
            self.command(name, ["herdr", *args], timeout=timeout)
            return None, "", ""
        argv = [self.herdr]
        if with_session:
            argv.extend(("--session", self.session))
        argv.extend(args)
        return self.command(name, argv, timeout=timeout)

    def _capture_event(self, envelope):
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        event = envelope.get("event") or data.get("type")
        pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
        session = (data.get("agent_session_id") or data.get("session_id")
                   or (pane.get("agent_session") or {}).get("value"))
        row = {"at": _utc_now(), "event": event or "unknown",
               "pane_id": data.get("pane_id") or pane.get("pane_id"),
               "session_id": session if isinstance(session, str) else None,
               "source": data.get("source") or pane.get("source"),
               "session_start_source": (data.get("session_start_source")
                                        or pane.get("session_start_source")),
               "seq": data.get("seq") or pane.get("seq"),
               "receipt_ref": data.get("receipt_ref") or data.get("request_id")}
        if isinstance(envelope.get("error"), dict):
            row["server_error"] = envelope["error"].get("code")
        self.event_capture.append(row)
        self._append(self.events_path, row)

    def _start_event_tap(self):
        import herdr_observation
        socket_path = herdr_observation.socket_path_for_endpoint(self.session)
        if not socket_path:
            return False
        self.decode_original = herdr_observation._decode_line
        def decode(line):
            envelope = self.decode_original(line)
            if (isinstance(envelope, dict)
                    and ("error" in envelope or "event" in envelope
                         or isinstance(envelope.get("data"), dict))):
                self._capture_event(envelope)
            return envelope
        herdr_observation._decode_line = decode
        self.transport = herdr_observation.ObservationTransport(
            self.session, socket_path, timeout=0.5)
        view = self.transport.observe()
        return bool(view and view.complete)

    def _stop_event_tap(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.decode_original is not None:
            import herdr_observation
            herdr_observation._decode_line = self.decode_original
            self.decode_original = None

    def probe_versions(self):
        if self.herdr:
            self.command("herdr-version", [self.herdr, "--version"], timeout=5)
            self.command("herdr-integration-install-help",
                         [self.herdr, "integration", "install", "--help"], timeout=5)
        else:
            self.command("herdr-version", ["herdr", "--version"], timeout=5)
        for runtime in RUNTIMES:
            binary = self.native[runtime]
            self.command(f"{runtime}-version", [binary or runtime, "--version"], timeout=5)

    def start_server(self):
        if not self.herdr:
            return False
        argv = [self.herdr, "--session", self.session, "server"]
        try:
            self.server = subprocess.Popen(
                argv, cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            self._append(self.commands_path, {"step": "herdr-server-start", "argv": _redact_argv(argv),
                                              "returncode": None, "stderr_reason": type(exc).__name__,
                                              "started_at": _utc_now(), "finished_at": _utc_now()})
            return False
        self._append(self.commands_path, {"step": "herdr-server-start", "argv": _redact_argv(argv),
                                          "pid": self.server.pid, "pid_start": _LIVE._proc_start(self.server.pid),
                                          "started_at": _utc_now(), "finished_at": None})
        ready_until = min(self.deadline, time.monotonic() + 30)
        while time.monotonic() < ready_until and self.server.poll() is None:
            rc, stdout, _ = self.herdr_command("private-pane-list-ready", ["pane", "list"], timeout=2)
            if rc == 0 and _json(stdout) is not None:
                return True
            time.sleep(min(0.2, max(0.0, ready_until - time.monotonic())))
        return False

    def create_pane(self, label):
        rc, stdout, _ = self.herdr_command("private-workspace-create",
            ["workspace", "create", "--cwd", str(self.project), "--label", label, "--no-focus"], timeout=10)
        payload = _json(stdout)
        panes = _pane_ids(payload)
        if rc == 0 and len(panes) == 1:
            pane = next(iter(panes))
            self.panes.append(pane)
            return pane
        result = _result(payload)
        workspace = None
        if isinstance(result, dict):
            workspace_value = result.get("workspace")
            workspace = (workspace_value.get("workspace_id") or workspace_value.get("id")
                         if isinstance(workspace_value, dict) else workspace_value)
        if not isinstance(workspace, str) or not workspace:
            return None
        rc, stdout, _ = self.herdr_command("private-tab-create",
            ["tab", "create", "--workspace", workspace, "--cwd", str(self.project), "--no-focus"], timeout=10)
        payload = _json(stdout)
        panes = _pane_ids(payload)
        if rc == 0 and len(panes) == 1:
            pane = next(iter(panes))
            self.panes.append(pane)
            return pane
        return None

    def _runtime_activation(self, runtime, step):
        command = [str(ROOT / "tools/install/harness.sh"), "runtime", "activate",
                   "--runtime", runtime, "--mode", "packaged", "--source", str(ROOT), "--json"]
        rc, stdout, stderr = self.command(step, command, cwd=self.project, timeout=60)
        payload = _json(stdout)
        return rc, payload, stderr

    def _herdr_status(self):
        return self.herdr_command("herdr-integration-status", ["integration", "status"],
                                  timeout=10, with_session=False)

    def _status_current(self, runtime, stdout):
        payload = _json(stdout)
        if payload:
            def scan(node):
                if isinstance(node, dict):
                    if node.get("runtime") == runtime and str(node.get("status", "")).lower() == "current":
                        return True
                    return any(scan(value) for value in node.values())
                if isinstance(node, list):
                    return any(scan(value) for value in node)
                return False
            return scan(payload)
        lines = [line.lower() for line in stdout.splitlines()]
        return any(runtime in line and "current" in line for line in lines)

    def _install_and_reinstall(self, runtimes):
        result = {}
        for runtime in runtimes:
            initial_rc, initial, _ = self._runtime_activation(runtime, f"hearting-{runtime}-install")
            upstream_rc, upstream_out, upstream_err = self.herdr_command(
                f"herdr-{runtime}-integration-install", ["integration", "install", runtime],
                timeout=30, with_session=False)
            update_rc, update, _ = self._runtime_activation(runtime, f"hearting-{runtime}-update")
            result[runtime] = {"hearting_install_exit": initial_rc,
                               "upstream_install_exit": upstream_rc,
                               "hearting_update_exit": update_rc,
                               "upstream_reason": _failure_reason(upstream_err, upstream_rc),
                               "install_ok": initial_rc == 0,
                               "update_ok": update_rc == 0}
        status_rc, status_out, status_err = self._herdr_status()
        result["status_exit"] = status_rc
        result["status_reason"] = _failure_reason(status_err, status_rc)
        for runtime in runtimes:
            result[runtime]["status_current"] = bool(status_rc == 0 and self._status_current(runtime, status_out))
        return result

    def _seed_user_callback(self, runtime):
        """Install one user-owned SessionStart/event sentinel in the isolated home."""
        log = Path(self.env["HOME"]) / f"callback-{runtime}.log"
        log.unlink(missing_ok=True)
        if runtime == "claude":
            destination = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            command = f"printf claude >> {shlex.quote(str(log))}"
            value = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
                {"type": "command", "command": command},
            ]}]}}
        elif runtime == "codex":
            destination = Path(self.env["CODEX_HOME"]) / "hooks.json"
            command = f"printf codex >> {shlex.quote(str(log))}"
            value = {"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": command},
            ]}]}}
        else:
            config = Path(self.env["OPENCODE_CONFIG_DIR"])
            plugins = config / "plugins"
            plugins.mkdir(parents=True, exist_ok=True)
            sentinel = plugins / "hearting-user-sentinel.js"
            sentinel.write_text(
                "import { appendFileSync } from 'node:fs';\n"
                "export default async () => ({ event: async ({ event }) => { "
                "if (event?.type === 'session.created') "
                f"appendFileSync({json.dumps(str(log))}, 'opencode\\n'); "
                "} });\n", encoding="utf-8")
            destination = config / "opencode.json"
            value = {"plugin": ["./plugins/hearting-user-sentinel.js"]}
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value), encoding="utf-8")
        self._append(self.commands_path, {
            "step": f"seed-user-callback-{runtime}", "started_at": _utc_now(),
            "finished_at": _utc_now(), "returncode": 0,
            "destination": str(destination), "private_fixture": True,
        })
        return log

    def _agent_start(self, runtime, pane, *, name=None):
        name = name or ("live-" + runtime + "-" + uuid.uuid4().hex[:6])
        timeout_ms = max(1000, min(60_000, int(self._remaining(60) * 1000)))
        rc, stdout, stderr = self.herdr_command(
            f"native-{runtime}-session-start",
            ["agent", "start", name, "--kind", runtime, "--pane", pane,
             "--timeout", str(timeout_ms)], timeout=min(65, self._remaining(65)),
        )
        start_payload = _json(stdout)
        rc_get, get_stdout, get_stderr = self.herdr_command(
            f"native-{runtime}-session-get", ["agent", "get", name], timeout=10)
        get_payload = _json(get_stdout)
        agent = (_result(get_payload) or {}).get("agent") if isinstance(_result(get_payload), dict) else None
        sid = _session_id(get_payload)
        if not sid and isinstance(start_payload, dict):
            sid = _session_id(start_payload)
        return {"name": name, "pane_id": pane, "start_exit": rc, "get_exit": rc_get,
                "session_id": sid, "start_reason": _failure_reason(stderr, rc),
                "get_reason": _failure_reason(get_stderr, rc_get),
                "agent_status": (agent or {}).get("agent_status") if isinstance(agent, dict) else None}

    def _wait_for_native_state(self, runtime, name, state, timeout_ms=30_000):
        return self.herdr_command(f"native-{runtime}-wait-{state}",
            ["agent", "wait", name, "--until", state, "--timeout", str(timeout_ms)],
            timeout=timeout_ms / 1000 + 3)

    def _busy_probe(self, runtime, pane, launched=None):
        launched = launched or self._agent_start(runtime, pane)
        if not launched.get("session_id"):
            return {"native_start": launched, "outcome": "unavailable",
                    "reason": launched["get_reason"]}
        # The prompt is passed only to the native child. command() redacts its
        # free-form argument from durable evidence.
        prompt = "Run one bounded shell command that sleeps for 20 seconds, then stop."
        rc, stdout, stderr = self.herdr_command(
            f"native-{runtime}-busy-probe",
            ["agent", "prompt", launched["name"], prompt, "--wait", "--until", "working",
             "--timeout", "30000"], timeout=35, native_output=True)
        payload = _json(stdout) or _json(stderr)
        busy_seen = rc == 0 or (isinstance(payload, dict) and not payload.get("error"))
        wait_rc, wait_out, wait_err = self._wait_for_native_state(runtime, launched["name"], "idle", 30_000)
        if self.transport is not None:
            self.transport.observe()
        ready = _json(wait_out) or _json(wait_err)
        return {"native_start": launched, "prompt_exit": rc,
                "prompt_reason": _failure_reason(stderr, rc),
                "idle_wait_exit": wait_rc,
                "idle_wait_reason": _failure_reason(wait_err, wait_rc),
                "busy_signal": "working" if busy_seen else None,
                "live_events": len(self.event_capture),
                "outcome": "unavailable" if rc != 0 or wait_rc != 0 else "measured",
                "session_id": launched["session_id"], "wait_payload_state": (ready or {}).get("result", {}).get("agent", {}).get("agent_status")}

    def _events_lost(self):
        if not self._start_event_tap():
            return {"outcome": "unavailable", "reason": "private-event-subscription-unavailable"}
        pane = self.create_pane("events-lost-scratch")
        if not pane:
            return {"outcome": "unavailable", "reason": "scratch-pane-create-failed"}
        before = self.transport.observe() if self.transport else None
        initial_count = len(self.event_capture)
        split_rows = []
        pane_chain = pane
        for index in range(32):
            rc, stdout, stderr = self.herdr_command(
                f"events-lost-burst-{index + 1}",
                ["pane", "split", "--pane", pane_chain, "--direction", "right",
                 "--ratio", "0.5", "--no-focus"], timeout=5)
            payload = _json(stdout)
            created = _pane_ids(payload)
            new_pane = next(iter(created), None)
            split_rows.append({"index": index + 1, "exit": rc, "pane_created": bool(new_pane)})
            if rc != 0 or not new_pane:
                break
            self.panes.append(new_pane)
            pane_chain = new_pane
        events_lost = [event for event in self.event_capture[initial_count:]
                       if event.get("server_error") == "events_lost"]
        recovered = None
        if self.transport:
            recovered = self.transport.observe()
        if events_lost and recovered and recovered.complete:
            outcome = "measured"
        else:
            outcome = "unavailable"
        return {"outcome": outcome, "before_complete": bool(before and before.complete),
                "burst_attempts": split_rows, "burst_limit": 32,
                "events_lost_count": len(events_lost),
                "fresh_snapshot_complete": bool(recovered and recovered.complete),
                "reason": None if outcome == "measured" else "events-lost-not-observed-within-bounded-burst"}

    def _install_callbacks(self):
        sentinels = {runtime: self._seed_user_callback(runtime) for runtime in RUNTIMES}
        status = self._install_and_reinstall(RUNTIMES)
        runtime_results = {}
        for runtime in RUNTIMES:
            pane = self.create_pane(f"callbacks-{runtime}")
            if not pane:
                runtime_results[runtime] = {"outcome": "unavailable", "reason": "scratch-pane-create-failed"}
                continue
            tap_ok = self._start_event_tap()
            native = self._agent_start(runtime, pane)
            view = self.transport.observe() if self.transport else None
            exact_events = [event for event in self.event_capture
                            if event.get("pane_id") == pane
                            and event.get("session_id") == native.get("session_id")
                            and event.get("session_start_source")
                            and isinstance(event.get("source"), str)
                            and event["source"].startswith("herdr:")]
            sentinel_path = sentinels[runtime]
            sentinel_count = 0
            if sentinel_path.exists():
                sentinel_count = len(sentinel_path.read_text(encoding="utf-8").splitlines())
            if sentinel_count is not None and sentinel_count > 1 or len(exact_events) > 1:
                outcome, reason = "fail", "duplicate-callback-observed"
            elif (status.get(runtime, {}).get("status_current") and native.get("session_id")
                  and tap_ok and len(exact_events) == 1
                  and (sentinel_count in {None, 1})):
                outcome, reason = "measured", None
            else:
                outcome, reason = "unavailable", "native-callback-consumption-not-proven"
            runtime_results[runtime] = {
                "outcome": outcome, "reason": reason,
                "status_current": status.get(runtime, {}).get("status_current", False),
                "native_session_id": native.get("session_id"),
                "native_start_exit": native.get("start_exit"),
                "matching_herdr_events": len(exact_events),
                "user_sentinel_count": sentinel_count,
                "snapshot_complete": bool(view and view.complete),
            }
            self._stop_event_tap()
        outcomes = {row["outcome"] for row in runtime_results.values()}
        result = "fail" if "fail" in outcomes else "measured" if outcomes == {"measured"} else "unavailable"
        return {"outcome": result, "runtimes": runtime_results, "install_update": status}

    def _case(self):
        if not self.herdr:
            self.probe_versions()
            return {"outcome": "unavailable", "reason": "herdr-executable-unavailable"}
        self.probe_versions()
        if not self.start_server():
            return {"outcome": "unavailable", "reason": "private-herdr-server-unavailable"}
        if self.args.case == "callbacks":
            return self._install_callbacks()
        if self.args.case == "events-lost":
            return self._events_lost()
        runtime = "opencode" if self.args.case == "opencode-busy" else "codex"
        status = self._install_and_reinstall((runtime,))
        pane = self.create_pane(f"{self.args.case}-scratch")
        if not pane:
            return {"outcome": "unavailable", "reason": "scratch-pane-create-failed",
                    "install_update": status}
        tap = self._start_event_tap()
        native = self._agent_start(runtime, pane)
        result = {"outcome": "unavailable", "native_start": native,
                  "install_update": status, "event_subscription": tap}
        if self.args.case in {"codex-busy", "opencode-busy"}:
            result.update(self._busy_probe(runtime, pane, native))
        elif self.args.case == "codex-async-local-skip":
            # Attempt the actual native prompt-session carrier and existing
            # next-prompt sweep. No synthetic peer record is created.
            rc, stdout, stderr = self.command(
                "codex-local-skip-sweep",
                [sys.executable, str(ROOT / "utilities/peer-steward.py"), "status",
                 "--undelivered", "--json"], timeout=10, native_output=True)
            payload = _json(stdout)
            result.update({"sweep_exit": rc, "sweep_reason": _failure_reason(stderr, rc),
                           "sweep_watch_count": len((payload or {}).get("watches", [])),
                           "outcome": "unavailable",
                           "reason": "no-native-advisory-receipt-consumption-observed"})
        else:
            rc, stdout, stderr = self.command(
                "booked-seat-enqueue",
                [sys.executable, str(ROOT / "utilities/session_tidy.py"), "enqueue",
                 "--harness", "codex", "--session-id", str(native.get("session_id") or ""),
                 "--cwd", str(self.project), "--no-continue"], timeout=15)
            result.update({"booking_exit": rc, "booking_reason": _failure_reason(stderr, rc),
                           "booking_stdout_bytes": len(stdout.encode("utf-8", "replace")),
                           "outcome": "unavailable",
                           "reason": "native-clear-successor-not-observed"})
        return result

    def cleanup_owned(self):
        self._stop_event_tap()
        for pane in reversed(self.panes):
            rc, _out, err = self.herdr_command("cleanup-private-pane-close", ["pane", "close", pane],
                                               timeout=5, with_session=True)
            self.cleanup.append({"action": "close-private-pane", "pane_id": pane,
                                 "exit": rc, "reason": _failure_reason(err, rc)})
        if self.server is not None:
            rc, _out, err = self.herdr_command("cleanup-private-server-stop", ["server", "stop"],
                                               timeout=8, with_session=True)
            self.cleanup.append({"action": "stop-private-server", "session": self.session,
                                 "exit": rc, "reason": _failure_reason(err, rc)})
            stopped = _LIVE.stop_owned_process(self.server, timeout=2)
            self.cleanup.append({"action": "reap-private-server-process", "result": stopped,
                                 "pid": self.server.pid, "pid_start": _LIVE._proc_start(self.server.pid)})

    def execute(self):
        try:
            if not self.herdr:
                result = {"outcome": "unavailable", "reason": "herdr-executable-unavailable"}
            else:
                result = self._case()
            self.result.update(result)
        except (LiveRunError, OSError, subprocess.SubprocessError, ValueError) as exc:
            self.result.update(outcome="unavailable",
                               reason=f"{type(exc).__name__}:{str(exc)[:160]}")
        finally:
            try:
                self.cleanup_owned()
            except Exception as exc:
                self.cleanup.append({"action": "cleanup-error", "reason": type(exc).__name__})
            self.result["finished_at"] = _utc_now()
            self.result["evidence_paths"] = [str(self.commands_path), str(self.events_path), str(self.result_path)]
            self._append(self.commands_path, {"step": "driver-finish", "started_at": self.result["finished_at"],
                                              "finished_at": self.result["finished_at"],
                                              "returncode": 0 if self.result.get("outcome") == "measured" else 2,
                                              "stderr_reason": self.result.get("reason")})
            self.result_path.write_text(json.dumps(self.result, ensure_ascii=False, indent=2,
                                                   sort_keys=True) + "\n", encoding="utf-8")
            self.result_path.chmod(0o600)
            print(json.dumps({"case": self.args.case, "outcome": self.result["outcome"],
                              "result": str(self.result_path)}, ensure_ascii=False))
        return 0 if self.result.get("outcome") == "measured" else 1 if self.result.get("outcome") == "fail" else 2


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--command-timeout", type=int, default=15)
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--opencode", default="opencode")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.timeout > MAX_TIMEOUT or args.command_timeout < 1:
        parser.error(f"--timeout must be 1..{MAX_TIMEOUT}; --command-timeout must be positive")
    try:
        return HerdrIntegrationLive(args).execute()
    except (LiveRunError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}:{str(exc)[:160]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
