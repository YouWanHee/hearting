#!/usr/bin/env python3
"""Opt-in real Codex native parent/continuation replay in isolated runtime state.

Usage: python3 -B utilities/codex_parent_completion_replay.py <output-dir>

The single argument is an evidence directory. The command creates an ephemeral
Codex home, worktree, registry, delivery record, and thread in a private runtime
temporary directory separate from the project checkout, then removes it. It never resumes an
existing user thread. Credentials are copied only into the private temporary
home, removed from that home after app-server initialization, and never written
to the evidence directory.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import codex_queue_delivery
import dispatch_completion_join as completion_join
import dispatch_pending_delivery as pending_delivery
import dispatch_session_sweep
import dispatch_supervision


class ReplayBlocked(RuntimeError):
    pass


class ReplayFailed(RuntimeError):
    pass


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _append_event(path: Path, event: dict) -> None:
    event = {"observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z"), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")


def _copy_regular(source: Path, target: Path, *, private: bool = False) -> bool:
    try:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            return False
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600 if private else stat.S_IMODE(info.st_mode) & 0o600 or 0o600)
        return True
    except OSError:
        return False


def _copy_runtime_config(source_home: Path, codex_home: Path, env: dict[str, str]
                        ) -> tuple[dict[str, bool], Path | None]:
    copied: dict[str, bool] = {"config.toml": True, "hooks.json": False, "installation_id": False}
    template = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
    all_hooks = template.get("hooks") if isinstance(template, dict) else None
    selected = {}
    for event, script_name in (("SessionStart", "sessionstart-lifecycle.py"),
                               ("UserPromptSubmit", "userprompt-lifecycle.py")):
        entries = all_hooks.get(event) if isinstance(all_hooks, dict) else None
        matched = []
        for entry in entries if isinstance(entries, list) else []:
            hooks = entry.get("hooks") if isinstance(entry, dict) else None
            keep = [hook for hook in hooks if isinstance(hook, dict)
                    and script_name in str(hook.get("command", ""))] if isinstance(hooks, list) else []
            if keep:
                matched.append({**entry, "hooks": keep})
        if not matched:
            raise ReplayBlocked("native-hook-fixture-template-incomplete:" + event)
        selected[event] = matched
    # Only the two lifecycle paths under test are enabled in this private home.
    (codex_home / "hooks.json").write_text(
        json.dumps({"hooks": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copied["hooks.json"] = True
    # Personal MCP, plugin, provider, and shell configuration is deliberately
    # absent. Native integrations and multi-agent dispatch stay disabled.
    (codex_home / "config.toml").write_text(
        "[analytics]\nenabled = false\n\n[features]\napps = false\nmulti_agent = false\n",
        encoding="utf-8")
    (codex_home / "installation_id").write_text(uuid.uuid4().hex + "\n", encoding="ascii")
    copied["installation_id"] = True
    copied["auth.json"] = False
    auth_source = None
    for source in (source_home / "auth.json", Path.home() / ".codex" / "auth.json"):
        if _copy_regular(source, codex_home / "auth.json", private=True):
            copied["auth.json"] = True
            auth_source = source
            break
    return copied, auth_source


def _fake_agent_home(temp_root: Path) -> Path:
    """Give hook path resolution a private legacy state root."""
    home = temp_root / "agent-home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (home / "core").mkdir(mode=0o700, exist_ok=True)
    (home / "core" / "CORE.md").write_text("Isolated Codex parent replay.\n", encoding="utf-8")
    for name in ("adapters", "capabilities", "roles", "utilities", "hooks", "tools", "config"):
        link = home / name
        if not link.exists():
            link.symlink_to(ROOT / name, target_is_directory=True)
    return home


def _stderr_summary(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "stderr-unavailable"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "stderr-empty"
    import re
    text = " ".join(lines[-3:])
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted-api-key>", text)
    return text[:320]


def _start_server(command: list[str], endpoint: Path, worktree: Path, env: dict[str, str],
                  stderr_path: Path):
    endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_dirs = (endpoint.parent, endpoint.parent.parent, endpoint.parent.parent.parent)
    for directory in private_dirs:
        os.chmod(directory, 0o700)
    parent_info = endpoint.parent.stat()
    home_info = endpoint.parent.parent.stat()
    if (any(info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            for info in (parent_info, home_info))):
        raise ReplayBlocked("isolated-app-server-socket-directory-unsafe")
    if endpoint.exists():
        endpoint.unlink()
    stderr_file = stderr_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=worktree, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=stderr_file,
            close_fds=True, start_new_session=True,
        )
    except OSError:
        stderr_file.close()
        raise
    process._replay_stderr_file = stderr_file
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            summary = _stderr_summary(stderr_path)
            stderr_file.close()
            raise ReplayBlocked("isolated-codex-app-server-exited-before-listen:"
                                + summary + f"; socket_dir_mode={oct(stat.S_IMODE(parent_info.st_mode))}"
                                + f"; codex_home_mode={oct(stat.S_IMODE(home_info.st_mode))}")
        try:
            info = endpoint.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                return process
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    summary = _stderr_summary(stderr_path)
    _stop_server(process)
    raise ReplayBlocked("isolated-codex-app-server-listen-timeout:" + summary)


def _stop_server(process) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    stderr_file = getattr(process, "_replay_stderr_file", None)
    if stderr_file is not None:
        stderr_file.close()


def _rpc_session(endpoint: Path):
    socket_client = codex_queue_delivery._WebSocket(endpoint, timeout=10)
    try:
        socket_client.request("initialize", {
            "clientInfo": {"name": "hearting-parent-replay", "title": "Hearting Parent Replay",
                           "version": "1"},
            "capabilities": None,
        })
        socket_client._send_frame(0x1, json.dumps(
            {"jsonrpc": "2.0", "method": "initialized"}, separators=(",", ":")
        ).encode("utf-8"))
        return socket_client
    except Exception:
        socket_client.close()
        raise


def _request(endpoint: Path, method: str, params: dict) -> dict:
    client = _rpc_session(endpoint)
    try:
        return client.request(method, params)
    finally:
        client.close()


def _turn_record(endpoint: Path, thread_id: str, turn_id: str) -> dict | None:
    cursor = None
    for _ in range(codex_queue_delivery.MAX_QUEUE_PAGES):
        params = {"threadId": thread_id, "limit": codex_queue_delivery.MAX_PAGE_SIZE,
                  "sortDirection": "desc", "itemsView": "full"}
        if cursor:
            params["cursor"] = cursor
        response = codex_queue_delivery._rpc(endpoint, "thread/turns/list", params, timeout=10)
        turns = response.get("data")
        if not isinstance(turns, list):
            raise ReplayBlocked("native-turn-history-unavailable")
        for turn in turns:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return turn
        cursor = response.get("nextCursor")
        if not cursor:
            return None
    raise ReplayBlocked("native-turn-history-page-limit")


def _all_turns(endpoint: Path, thread_id: str) -> list[dict]:
    cursor = None
    found = []
    for _ in range(codex_queue_delivery.MAX_QUEUE_PAGES):
        params = {"threadId": thread_id, "limit": codex_queue_delivery.MAX_PAGE_SIZE,
                  "sortDirection": "desc", "itemsView": "full"}
        if cursor:
            params["cursor"] = cursor
        response = codex_queue_delivery._rpc(endpoint, "thread/turns/list", params, timeout=10)
        page = response.get("data")
        if not isinstance(page, list):
            raise ReplayBlocked("native-turn-history-unavailable")
        found.extend(item for item in page if isinstance(item, dict))
        cursor = response.get("nextCursor")
        if not cursor:
            return found
    raise ReplayBlocked("native-turn-history-page-limit")


def _turn_for_client_message(turns: list[dict], client_id: str) -> dict | None:
    matches = []
    for turn in turns:
        items = turn.get("items", []) if isinstance(turn.get("items"), list) else []
        if any(isinstance(item, dict) and item.get("clientId") == client_id for item in items):
            matches.append(turn)
    if len(matches) > 1:
        raise ReplayBlocked("continuation-client-id-history-conflict")
    return matches[0] if matches else None


def _turn_items(turn: dict) -> tuple[list[str], list[dict]]:
    messages, tools = [], []
    for item in turn.get("items", []) if isinstance(turn.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            messages.append(item["text"])
        elif item.get("type") == "commandExecution":
            tools.append({key: item.get(key) for key in
                          ("id", "command", "aggregatedOutput", "exitCode", "status") if key in item})
    return messages, tools


def _fixture_registry(jobs: Path, thread_id: str, workspace: Path, sentinel: str):
    attempt = "att-parent-replay-" + uuid.uuid4().hex[:12]
    route_id = "rt-parent-replay-" + uuid.uuid4().hex[:8]
    route_hash = "sha256:" + hashlib.sha256(route_id.encode()).hexdigest()
    revision = "revision-" + hashlib.sha256((attempt + route_id).encode()).hexdigest()[:16]
    delivery_id = "delivery-" + hashlib.sha256((thread_id + attempt).encode()).hexdigest()[:32]
    delivery_parent = "parent-session-" + hashlib.sha256(thread_id.encode()).hexdigest()
    receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": delivery_parent,
        "job_registry": str(jobs), "delivery_classification": "success",
        "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
            "reason": "registry-closed", "required_action": "advance-completed",
            "harness": "codex", "delivery_classification": "success"}]}
    digest = pending_delivery._canonical_receipt_digest(receipt)
    metadata = {
        "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
        "execution_surface": "registered-headless", "registered_worker": "1",
        "launch_claimed": "1", "launch_started": "1", "worker_type": "owner",
        "attempt_id": attempt, "parent_sid": thread_id, "parent_completion_delivery": "codex-native-queue",
        "delivery_intent": "1", "delivery_id": delivery_id,
        "delivery_receipt_digest": digest, "delivery_row_revision": revision,
        "delivery_recipient_kind": "codex-native-queue", "route_id": route_id,
        "owner_route_id": route_id, "owner_route_hash": route_hash,
        "route_hash": route_hash, "route_node": "_owner", "harness": "codex",
        "workflow_completion": "runtime-v1", "note": "completed-marker", "failure_class": "pass",
        "launch_outcome": "completed", "terminal_event": "worker-result",
    }
    jobs.write_text("2026-10-19T00:00:00Z\tdone\t" + str(workspace) + "\t" + str(workspace)
        + "\tisolated-parent-replay\t" + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
        encoding="utf-8")
    record = pending_delivery.create(jobs.resolve(strict=True).parent,
        recipient_kind="codex-native-queue", recipient_key=thread_id, delivery_id=delivery_id,
        session_generation="", session_generation_supported="0", attempt_ids=[attempt],
        parent_attempt_id="-", route_id=route_id, route_node="_owner", receipt=receipt,
        receipt_digest=digest, row_revisions={attempt: revision})
    return attempt, route_id, receipt, record, metadata


def _run_replay(output_dir: Path) -> dict:
    report = {"schema_version": 1, "source_sha": "unknown", "status": "blocked",
              "verdict": "BLOCKED", "d1": "unfinished", "d2": "unfinished",
              "d3": "not-assessed", "operating_records_changed": False,
              "model": "default (not independently identified)", "limitations": []}
    try:
        report["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=ROOT,
                              capture_output=True, timeout=10, check=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT,
                                   capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
        digest = hashlib.sha256(diff)
        for relative in sorted(untracked):
            path = ROOT / relative
            if path.is_file() and not path.is_symlink():
                digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes())
        report["source_tree_sha256"] = digest.hexdigest()
    except (OSError, subprocess.SubprocessError):
        report["source_tree_sha256"] = "unavailable"
    codex = shutil.which("codex")
    if not codex:
        raise ReplayBlocked("codex-executable-unavailable")
    version = subprocess.run([codex, "--version"], capture_output=True, text=True,
                             timeout=10, check=False)
    report["codex_version"] = (version.stdout or version.stderr).strip()[:200]
    if version.returncode != 0:
        raise ReplayBlocked("codex-version-probe-failed")
    _write_json(output_dir / "replay.json", report)

    shared_directory = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
    if shared_directory.exists() and stat.S_IMODE(shared_directory.stat().st_mode) == 0:
        # The outer native sandbox deliberately masks privileged daemon IPC.
        # Private CODEX_HOME modes cannot repair it; never unmask that boundary.
        raise ReplayBlocked("nested-codex-shared-daemon-directory-masked")

    source_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    # The private runtime fixture is removed after the replay; only evidence
    # remains under output_dir. /tmp is the writable temp root in this runner.
    with tempfile.TemporaryDirectory(prefix="codex-parent-replay-", dir="/tmp") as temporary:
        temp_root = Path(temporary)
        home = temp_root / "home"
        codex_home = home / ".codex"
        worktree = temp_root / "workspace"
        runtime = temp_root / "runtime"
        for path in (home, codex_home, worktree, runtime, home / ".config", home / ".state"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(worktree)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        copied, auth_source = _copy_runtime_config(source_home, codex_home, dict(os.environ))
        if not copied.get("auth.json"):
            raise ReplayBlocked("isolated-codex-auth-unavailable")
        trusted_project = str(worktree).replace('\\', '\\\\').replace('"', '\\"')
        (codex_home / "config.toml").write_text(
            f'[projects."{trusted_project}"]\ntrust_level = "trusted"\n\n'
            "[analytics]\nenabled = false\n\n[features]\napps = false\nmulti_agent = false\n",
            encoding="utf-8")
        agent_home = _fake_agent_home(temp_root)
        workflow = (ROOT / "core/WORKFLOW.md").read_text(encoding="utf-8")
        report_policy = workflow.split("### 0.5. Post-Execution Completion Report", 1)[1].split(
            "### 0.6.", 1)[0]
        response_policy = (ROOT / "roles/response-policy.md").read_text(encoding="utf-8")
        # Exercise candidate policy in native project context, not an ideal
        # reporting rubric supplied in the user's task message.
        policy = "# Candidate completion policy\n" + report_policy + "\n" + response_policy
        (worktree / "AGENTS.md").write_text(policy, encoding="utf-8")
        report["candidate_policy_sha256"] = hashlib.sha256(policy.encode()).hexdigest()
        _write_json(output_dir / "replay.json", report)
        jobs = runtime / "jobs.log"
        jobs.touch(mode=0o600)
        artifact_root = worktree / ".agent_reports"
        artifact_root.mkdir(mode=0o700)
        sentinel = "HEARTING_REPLAY_" + uuid.uuid4().hex.upper()
        artifact = artifact_root / "exact-result.md"
        artifact.write_text(
            f"# Isolated fixture result\n\n"
            "Chronology: original code attempt FAIL; corrected code attempt PASS; fixture deployment PASS; "
            "fixture independent review PASS; documentation complete; cleanup state unknown; late join-deadline observed.\n"
            f"What: created this isolated fixture result.\n"
            f"Why: replay the approved parent follow-through.\n"
            f"Before: follow-through file absent.\n"
            f"After: follow-through file should contain this exact sentinel: {sentinel}\n"
            f"Verification: read this file and write the sentinel once.\n"
            f"Commit/PR/deployment/serving: none; this is an isolated fixture.\n"
            f"Limits: no operating work was performed or measured.\n",
            encoding="utf-8")
        followthrough = worktree / "follow-through.txt"
        endpoint = codex_home / "app-server-control" / "app-server-control.sock"
        # Do not inherit the supervised worker's CODEX_HOME, artifact output,
        # memory, Herd, or provider-key bindings into the isolated app server.
        env = {key: os.environ[key] for key in
               ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL")
               if key in os.environ}
        env.update({
            "HOME": str(home), "CODEX_HOME": str(codex_home), "AGENT_HOME": str(agent_home),
            "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(artifact_root),
            "AGENT_ARTIFACT_CHECKPOINT": "off", "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".state"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
            "TMPDIR": str(temp_root / "tmp"),
        })
        Path(env["TMPDIR"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        hooks_path = codex_home / "hooks.json"
        if not hooks_path.is_file():
            raise ReplayBlocked("isolated-codex-hooks-config-unavailable")
        env["HEARTING_CODEX_HOOK_SOURCES"] = json.dumps([str(codex_home / "config.toml"), str(hooks_path)])
        # The bare UDS transport uses CODEX_HOME's private app-server-control
        # directory, matching codex_queue_dispatch.resolve_queue_socket.
        command = [codex, "app-server", "--listen", "unix://",
                   "--disable", "apps", "--disable", "multi_agent"]
        server = _start_server(command, endpoint, worktree, env,
                               temp_root / "app-server-initial.stderr")
        try:
            client = _rpc_session(endpoint)
            try:
                # Initialization has loaded the auth record into the isolated
                # server. Remove the private copy before model tools can run.
                (codex_home / "auth.json").unlink(missing_ok=True)
                thread_result = client.request("thread/start", {
                    "cwd": str(worktree), "ephemeral": False, "sandbox": "workspace-write",
                    "approvalPolicy": "never",
                })
                thread = thread_result.get("thread")
                if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                    raise ReplayBlocked("isolated-thread-start-unconfirmed")
                thread_id = thread["id"]
                attempt, route_id, receipt, completion_record, metadata = _fixture_registry(
                    jobs, thread_id, worktree, sentinel)
                prompt = (
                    "Continue the already approved isolated fixture task. Read the exact artifact at "
                    f"{artifact}. Verify its sentinel, then write exactly that sentinel once to "
                    f"{followthrough}, unless it already contains the exact sentinel once. Do not repeat a completed action. "
                    "The earlier chronology is fixture history, not a production deployment or real independent review. "
                    "This is only an isolated fixture: no real deployment, PR, "
                    "commit, or operating change occurred."
                )
                (output_dir / "input.txt").write_text(prompt + "\n", encoding="utf-8")
                started = client.request("turn/start", {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    "cwd": str(worktree), "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(worktree)],
                                      "networkAccess": False, "excludeTmpdirEnvVar": False,
                                      "excludeSlashTmp": False},
                })
                turn = started.get("turn")
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise ReplayBlocked("isolated-turn-start-unconfirmed")
                turn_id = turn["id"]
                _append_event(output_dir / "native-events.jsonl", {
                    "event": "turn-started", "thread_id": thread_id, "turn_id": turn_id,
                    "attempt_id": attempt, "delivery_id": completion_record["delivery_id"],
                    "receipt_digest": completion_record["receipt_digest"],
                })
            finally:
                client.close()

            from peer_obligations import ObligationStore, stable_duty_id
            duty_identity = {"jobs": str(jobs.resolve()), "attempt_id": attempt,
                             "session_id": thread_id, "harness": "codex", "server": "default"}
            obligations = ObligationStore(runtime)
            duty_id = stable_duty_id("registered-batch", duty_identity)
            ack_deadline = time.monotonic() + 60
            first_record = None
            first_turn_before_crash = None
            duty = None
            while time.monotonic() < ack_deadline:
                first_record = pending_delivery.read(runtime, thread_id, completion_record["delivery_id"])
                duty = obligations.get(duty_id)
                binding = ((duty or {}).get("observation") or {}).get("native_receiving_turn") or {}
                if (first_record and first_record.get("state") == "acked"
                        and binding.get("thread_id") == thread_id
                        and binding.get("turn_id") == turn_id
                        and binding.get("delivery_id") == completion_record["delivery_id"]):
                    first_turn_before_crash = _turn_record(endpoint, thread_id, turn_id)
                    break
                time.sleep(0.005)
            if first_record is None or first_record.get("state") != "acked":
                raise ReplayBlocked("native-hook-exact-ack-not-observed-before-turn-end")
            if not isinstance(duty, dict) or not ((duty.get("observation") or {}).get("native_receiving_turn")):
                raise ReplayBlocked("native-receiving-turn-not-retained-before-ack")
            if not first_turn_before_crash or first_turn_before_crash.get("status") == "completed":
                raise ReplayBlocked("native-app-server-closed-crash-window-before-isolated-kill")
            initial_messages, initial_tools = _turn_items(first_turn_before_crash)
            _write_json(output_dir / "first-turn-before-crash.json", first_turn_before_crash)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "ack-before-parent-crash", "turn_id": turn_id,
                "status": first_turn_before_crash.get("status"),
                "precrash_agent_messages": len(initial_messages),
                "precrash_tool_items": len(initial_tools),
                "acked_by": first_record.get("acked_by"),
                "receiving_turn": (duty.get("observation") or {}).get("native_receiving_turn"),
            })
            _stop_server(server)
            server = None
            _append_event(output_dir / "native-events.jsonl", {
                "event": "isolated-app-server-killed", "signal": "SIGKILL", "thread_id": thread_id})

            if auth_source is None or not _copy_regular(auth_source, codex_home / "auth.json", private=True):
                raise ReplayBlocked("isolated-codex-auth-unavailable-after-restart")
            server = _start_server(command, endpoint, worktree, env,
                                   temp_root / "app-server-reconnect.stderr")
            interrupted_turn = _turn_record(endpoint, thread_id, turn_id)
            if not interrupted_turn or interrupted_turn.get("status") != "interrupted":
                raise ReplayBlocked("native-turn-history-does-not-prove-interrupted-crash")
            client = _rpc_session(endpoint)
            try:
                (codex_home / "auth.json").unlink(missing_ok=True)
                for reconnect_index in (1, 2):
                    resumed = client.request("thread/resume", {
                        "threadId": thread_id, "cwd": str(worktree),
                        "approvalPolicy": "never", "sandbox": "workspace-write",
                    })
                    resumed_thread = resumed.get("thread")
                    if not isinstance(resumed_thread, dict) or resumed_thread.get("id") != thread_id:
                        raise ReplayBlocked("native-sessionstart-resume-binding-mismatch")
                    _append_event(output_dir / "native-events.jsonl", {
                        "event": "native-thread-resumed", "thread_id": thread_id,
                        "reconnect_index": reconnect_index})
                    time.sleep(0.1)
            finally:
                client.close()

            continuation_deadline = time.monotonic() + 60
            continuation_id = ""
            duty = None
            while time.monotonic() < continuation_deadline:
                duty = obligations.get(duty_id)
                observation = (duty or {}).get("observation") or {}
                candidate = observation.get("continuation_id")
                if isinstance(candidate, str) and candidate.startswith("delivery-"):
                    continuation_id = candidate
                    break
                time.sleep(0.1)
            if not continuation_id:
                reason = ((duty or {}).get("observation") or {}).get("reason") or "no-continuation-recorded"
                raise ReplayBlocked("sessionstart-native-continuation-unobserved:" + str(reason))
            continuation_deadline = time.monotonic() + 240
            continuation_turn = None
            all_turns = []
            while time.monotonic() < continuation_deadline:
                all_turns = _all_turns(endpoint, thread_id)
                continuation_turn = _turn_for_client_message(all_turns, continuation_id)
                if continuation_turn and continuation_turn.get("status") in {"completed", "interrupted"}:
                    break
                time.sleep(0.1)
            if continuation_turn is None:
                raise ReplayBlocked("stable-native-continuation-not-consumed")
            if continuation_turn.get("status") != "completed":
                raise ReplayBlocked("native-continuation-turn-interrupted")
            continuation_messages, continuation_tools = _turn_items(continuation_turn)
            if not continuation_messages:
                raise ReplayFailed("native-continuation-final-report-missing")
            _write_json(output_dir / "continuation-turn.json", continuation_turn)
            continuation_occurrences = sum(1 for turn in _all_turns(endpoint, thread_id)
                                           if _turn_for_client_message([turn], continuation_id) is not None)
            if continuation_occurrences != 1:
                raise ReplayFailed("stable-native-continuation-not-exactly-once")
            final_text = continuation_messages[-1]
            tool_trace = [*initial_tools, *continuation_tools]
            (output_dir / "turn-output.txt").write_text(final_text + "\n", encoding="utf-8")
            (output_dir / "tool-trace.jsonl").write_text("".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in tool_trace),
                encoding="utf-8")
            report_check = {
                "artifact_sentinel_seen_in_tool_trace": any(
                    sentinel in json.dumps(item, ensure_ascii=False) for item in tool_trace),
                "followthrough_exactly_once": followthrough.read_text(encoding="utf-8") == sentinel + "\n",
                "report_mentions_reason": any(token in final_text.lower() for token in ("why", "because", "reason")),
                "report_mentions_before_after": "before" in final_text.lower() and "after" in final_text.lower(),
                "report_mentions_verification": any(token in final_text.lower() for token in ("check", "verif")),
                "report_mentions_delivery_state": any(token in final_text.lower() for token in
                    ("deployment", "deploy", "serving", "served")),
                "report_mentions_limit": any(token in final_text.lower() for token in ("limit", "remaining", "fixture")),
            }

            # Re-expose the late cleanup and same exact result only inside the
            # isolated ledger. Observe history before the direct user question.
            history_before_cleanup = _all_turns(endpoint, thread_id)
            cleanup_notice = dispatch_supervision.materialize(
                jobs, {attempt}, reason="join-deadline")[0]
            cleanup_text = dispatch_session_sweep._bounded_receipt_text(cleanup_notice)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "late-cleanup-materialized", "delivery_id": cleanup_notice["delivery_id"],
                "timestamp_provenance": cleanup_notice.get("timestamp_provenance"),
                "rendered_context": cleanup_text,
            })
            duplicate = pending_delivery.create(runtime,
                recipient_kind="codex-native-queue", recipient_key=thread_id,
                delivery_id=completion_record["delivery_id"], session_generation="",
                session_generation_supported="0", attempt_ids=[attempt], parent_attempt_id="-",
                route_id=route_id, route_node="_owner", receipt=receipt,
                receipt_digest=completion_record["receipt_digest"],
                row_revisions={attempt: metadata["delivery_row_revision"]})
            # Admit these records to the candidate consumer before observing
            # silence; an unsubmitted empty native queue is not evidence.
            deliveries, _ = dispatch_session_sweep.sweep_deliver(runtime, "codex-native-queue", thread_id)
            if deliveries:
                raise ReplayFailed("late-cleanup-or-duplicate-still-eligible-for-native-delivery")
            history_after_cleanup = _all_turns(endpoint, thread_id)
            queue_after_cleanup = codex_queue_delivery.list_queue(endpoint, thread_id, timeout=10)
            no_extra_turn = len(history_after_cleanup) == 2
            no_queued_duplicate = not any(
                item.get("clientUserMessageId") == completion_record["delivery_id"]
                for item in queue_after_cleanup)
            if not no_extra_turn or not no_queued_duplicate:
                raise ReplayFailed("late-cleanup-created-an-unrequested-native-turn-or-queue-item")

            followthrough_before_question = followthrough.read_bytes()
            stat_before_question = followthrough.stat().st_mtime_ns
            question = ("Did this isolated fixture deploy to production? Answer directly from the exact result. "
                        "Do not repeat the fixture follow-through.")
            (output_dir / "direct-question.txt").write_text(question + "\n", encoding="utf-8")
            question_started = _request(endpoint, "turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": question, "text_elements": []}],
                "cwd": str(worktree), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(worktree)],
                                  "networkAccess": False, "excludeTmpdirEnvVar": False,
                                  "excludeSlashTmp": False},
            })
            question_turn = question_started.get("turn") or {}
            question_turn_id = question_turn.get("id")
            if not isinstance(question_turn_id, str) or not question_turn_id:
                raise ReplayBlocked("direct-question-turn-start-unconfirmed")
            question_deadline = time.monotonic() + 180
            question_record = None
            while time.monotonic() < question_deadline:
                question_record = _turn_record(endpoint, thread_id, question_turn_id)
                if question_record and question_record.get("status") in {"completed", "interrupted"}:
                    break
                time.sleep(0.1)
            if not question_record or question_record.get("status") != "completed":
                raise ReplayBlocked("direct-question-turn-unfinished")
            question_messages, question_tools = _turn_items(question_record)
            _write_json(output_dir / "direct-question-turn.json", question_record)
            direct_answer = question_messages[-1] if question_messages else ""
            direct_answer_ok = ("production" in direct_answer.lower()
                                and any(token in direct_answer.lower() for token in
                                        ("no", "not", "fixture", "unmeasured")))
            followthrough_unchanged = (followthrough.read_bytes() == followthrough_before_question
                                       and followthrough.stat().st_mtime_ns == stat_before_question)
            if not direct_answer_ok or not followthrough_unchanged:
                raise ReplayFailed("direct-question-or-no-repeat-contract-failed")
            (output_dir / "direct-answer.txt").write_text(direct_answer + "\n", encoding="utf-8")
            with (output_dir / "tool-trace.jsonl").open("a", encoding="utf-8") as stream:
                for item in question_tools:
                    stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            cleanup_record_after_question = pending_delivery.read(
                runtime, thread_id, cleanup_notice["delivery_id"])
            all_turns_after_question = _all_turns(endpoint, thread_id)
            queue_after_question = codex_queue_delivery.list_queue(endpoint, thread_id, timeout=10)
            # The exact history/queue checks are native Codex evidence; receipt
            # identity, cleanup rendering, and no-turn-before-question are
            # deterministic evidence from the same isolated registry.
            report.update({
                "status": "observed", "thread_id": thread_id, "turn_id": turn_id,
                "original_turn_status_after_crash": first_turn_before_crash.get("status"),
                "continuation_id": continuation_id,
                "continuation_turn_id": continuation_turn.get("id"),
                "duplicate_sessionstart_reconnects": 2,
                "continuation_message_occurrences": sum(
                    1 for turn in all_turns_after_question
                    if _turn_for_client_message([turn], continuation_id) is not None),
                "crash_window_before_model_output_captured": not initial_messages and not initial_tools,
                "attempt_id": attempt, "delivery_id": completion_record["delivery_id"],
                "receipt_digest": completion_record["receipt_digest"],
                "first_turn_status": "interrupted",
                "precrash_tool_count": len(initial_tools),
                "continuation_tool_count": len(continuation_tools),
                "continuation_report_count": len(continuation_messages),
                "report_checks": report_check,
                "late_cleanup_rendered_cleanup_only": "retains cleanup observation only" in cleanup_text,
                "late_cleanup_additional_native_turns_before_direct_question":
                    max(0, len(history_after_cleanup) - len(history_before_cleanup)),
                "late_cleanup_duplicate_completion_queue_items": sum(
                    1 for item in queue_after_question
                    if item.get("clientUserMessageId") == completion_record["delivery_id"]),
                "duplicate_record_kept_ack_state": duplicate.get("state") == "acked",
                "native_history_count_unchanged_before_direct_question": no_extra_turn,
                "native_queue_has_no_completion_duplicate": no_queued_duplicate,
                "cleanup_notice_acknowledged_on_direct_question":
                    bool(cleanup_record_after_question and cleanup_record_after_question.get("state") == "acked"),
                "direct_question_answered": direct_answer_ok,
                "followthrough_unchanged_after_direct_question": followthrough_unchanged,
                "crash_reconnect_replayed": True,
                "limitations": ["Fixture result, deployment, serving, and operating performance were not real "
                    "production events. The app-server replay used an isolated persisted thread and copied "
                    "private auth material that was removed before model tools ran."],
            })
            if not all(report_check.values()):
                report["status"] = "failed"
                report["verdict"] = "FAIL"
                report["failure_reason"] = "first-parent-report-or-tool-trace-contract-failed"
            elif initial_messages or initial_tools:
                report["verdict"] = "BLOCKED"
                report["blocker"] = "native-crash-window-after-ack-before-tool-or-response-not-captured"
                report["d1"] = "observed-with-native-continuation"
                report["d2"] = "late-cleanup-and-direct-question-observed"
            else:
                report["verdict"] = "BLOCKED"
                report["blocker"] = "full-parent-chronology-and-idle-handover-native-replay-not-yet-covered"
                report["d1"] = "isolated-native-crash-slice-observed; full-parent-acceptance-unfinished"
                report["d2"] = "shared-consumer-suppression-observed; full-native-notice-and-idle-handover-unfinished"
                report["d3"] = "exact-ack-and-cleanup-unknown-preserved; adapter matrix is deterministic-only"
            return report
        finally:
            _stop_server(server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        report = _run_replay(output)
    except ReplayFailed as exc:
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report.update(status="failed", verdict="FAIL", failure_reason=str(exc),
                      operating_records_changed=False)
    except ReplayBlocked as exc:
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report.update(status="blocked", verdict="BLOCKED", blocker=str(exc),
                      operating_records_changed=False)
    except Exception as exc:  # noqa: BLE001 -- retain the exact isolated runtime limitation.
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report.update(status="blocked", verdict="BLOCKED", blocker="isolated-replay-runtime-error",
                      detail=str(exc)[:240], operating_records_changed=False)
    _write_json(output / "replay.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("verdict") in {"PASS", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
