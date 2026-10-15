#!/usr/bin/env python3
"""Read-only diagnosis for a Git index-lock creation collision."""
from __future__ import annotations

import argparse
import base64
import io
import json
import locale
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

DIAGNOSIS_BUDGET_SECONDS = 2.0
_LOCK_ERROR = re.compile(
    rb"unable to create ['\"](?P<path>[^\r\n]*?index\.lock)['\"]:\s*file exists\b",
    re.IGNORECASE,
)
_SHELL_META = set(";&|<>$()\n\r") | {chr(96)}
_GIT_ENV_PATHS = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
}
_RESPONSE_CODES = ("exit_code", "exitCode", "returncode", "code")


def compatibility_text(raw: bytes, encoding: str, *, failed: bool) -> str:
    """Match text-mode newline conversion; preserve undecodable failures visibly."""
    errors = "replace" if failed else "strict"
    with io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, errors=errors, newline=None) as stream:
        return stream.read()


def failure_record(returncode: int, stdout: bytes, stderr: bytes, encoding: str,
                   *, raw_bytes_available: bool = True) -> dict:
    return {
        "returncode": int(returncode),
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "encoding": encoding,
        "raw_bytes_available": bool(raw_bytes_available),
    }


def _shell_words(command: str) -> list[str] | None:
    if not command or any(ord(char) < 32 and char not in "\t" for char in command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>$()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    if any(word in {";", "|", "||", "|&", ">", ">>", "<", "<<", "$", "(", ")"}
           for word in words):
        return None
    return words


def parse_shell_command(command: str, cwd: str) -> tuple[list[str], str, dict[str, str]] | None:
    """Parse one Git command with literal cwd/env prefixes; never execute it."""
    words = _shell_words(command)
    if not words:
        return None
    base = os.path.abspath(cwd or os.getcwd())
    if "&&" in words:
        if words.count("&&") != 1 or len(words) < 4 or words[0] != "cd":
            return None
        split = words.index("&&")
        if split != 2:
            return None
        base = os.path.abspath(os.path.join(base, words[1]))
        words = words[3:]
    elif any(word in {"&&", "&", ";"} for word in words):
        return None

    env: dict[str, str] = {}
    if words and words[0] == "env":
        words = words[1:]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        key, value = words.pop(0).split("=", 1)
        env[key] = value
    if not words or Path(words[0]).name != "git":
        return None
    argv = [words[0], *words[1:]]
    if any(any(char in _SHELL_META for char in word) for word in argv):
        return None
    return argv, base, env


def _global_args(argv: list[str]) -> list[str] | None:
    if not argv or Path(argv[0]).name != "git":
        return None
    result = [argv[0]]
    index = 1
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--super-prefix", "--exec-path"}
    flags = {"--bare", "--no-optional-locks", "--literal-pathspecs",
             "--no-replace-objects", "--paginate", "--no-pager"}
    while index < len(argv):
        token = argv[index]
        if token in takes_value:
            if index + 1 >= len(argv):
                return None
            result.extend((token, argv[index + 1]))
            index += 2
            continue
        if any(token.startswith(option + "=") for option in
               ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path")):
            result.append(token)
            index += 1
            continue
        if token in flags:
            result.append(token)
            index += 1
            continue
        if token.startswith("-"):
            return None
        return result
    return None


def _run_git_path(global_args: list[str], cwd: str, env: dict[str, str],
                  path: str, timeout: float) -> str | None:
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            [*global_args, "rev-parse", "--path-format=absolute", "--git-path", path],
            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    value = os.fsdecode(result.stdout).rstrip("\r\n")
    return value if value and "\n" not in value and "\r" not in value else None


def _run_git_index(argv: list[str], cwd: str, env: dict[str, str], timeout: float):
    global_args = _global_args(argv)
    if global_args is None or timeout <= 0:
        return None
    deadline = time.monotonic() + timeout
    index_path = _run_git_path(global_args, cwd, env, "index", max(0.0, deadline - time.monotonic()))
    lock_path = _run_git_path(global_args, cwd, env, "index.lock",
                               max(0.0, deadline - time.monotonic()))
    return {"index": index_path, "lock": lock_path}


def errno_name(exc: OSError) -> str:
    return {1: "permission-denied", 2: "not-found", 13: "permission-denied"}.get(
        exc.errno, "observation-error")


def _observer_main(path: str) -> int:
    try:
        before = os.lstat(path)
        after = os.lstat(path)
    except FileNotFoundError:
        print(json.dumps({"state": "not_found"}))
        return 0
    except OSError as exc:
        print(json.dumps({"state": "unavailable", "reason": errno_name(exc)}))
        return 0
    before_id = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
    after_id = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
    if before_id != after_id:
        print(json.dumps({"state": "replaced"}))
        return 0
    mode = after.st_mode
    kind = "symlink" if stat.S_ISLNK(mode) else "regular-file" if stat.S_ISREG(mode) else (
        "directory" if stat.S_ISDIR(mode) else "other")
    print(json.dumps({
        "state": "present", "kind": kind, "mode": oct(stat.S_IMODE(mode)),
        "size": after.st_size, "mtime_ns": after.st_mtime_ns,
    }))
    return 0


def _observe(path: str, timeout: float) -> dict:
    if timeout <= 0:
        return {"state": "unavailable", "reason": "deadline-exhausted"}
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--observe", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return {"state": "unavailable", "reason": "observer-start-failed"}
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        if child.stdout:
            child.stdout.close()
        return {"state": "unavailable", "reason": "observer-timeout"}
    if child.returncode:
        return {"state": "unavailable", "reason": "observer-failed"}
    try:
        data = json.loads(output)
        return data if isinstance(data, dict) and isinstance(data.get("state"), str) else {
            "state": "unavailable", "reason": "observer-invalid-result"}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"state": "unavailable", "reason": "observer-invalid-result"}


def _lock_path_from_error(stderr: bytes) -> str | None:
    match = _LOCK_ERROR.search(stderr)
    return os.fsdecode(match.group("path")) if match else None


def _context_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        for key, value in extra.items():
            if key in _GIT_ENV_PATHS or key.startswith("GIT_CONFIG_"):
                env[key] = value
    return env


def _git_c_directories(argv: list[str], cwd: str) -> list[str]:
    global_args = _global_args(argv)
    if global_args is None:
        return [os.path.abspath(cwd)]
    directories = [os.path.abspath(cwd)]
    index = 1
    while index < len(global_args):
        token = global_args[index]
        if token == "-C" and index + 1 < len(global_args):
            directories.append(os.path.abspath(os.path.join(directories[-1], global_args[index + 1])))
            index += 2
        else:
            index += 1
    return directories


def _read_only_commands(argv: list[str], cwd: str, env: dict[str, str], lock_path: str) -> list[str]:
    global_args = _global_args(argv)
    commands = [f"ls -ld -- {shlex.quote(lock_path)}"]
    if global_args is None or any(token == "-c" or token.startswith("-c=") for token in global_args):
        return commands
    if any(key.startswith("GIT_CONFIG") for key in env):
        return commands
    safe_env = [(key, env[key]) for key in sorted(_GIT_ENV_PATHS) if env.get(key)]
    git_command = ["git", "--no-optional-locks", *global_args[1:], "status", "--short", "--branch"]
    prefix = "env" + "".join(f" {key}={shlex.quote(value)}" for key, value in safe_env)
    status = f"cd {shlex.quote(os.path.abspath(cwd))} && {prefix} " + " ".join(
        shlex.quote(value) for value in git_command)
    commands.insert(0, status)
    return commands


def diagnose_failure(argv: list[str], cwd: str, returncode: int, stdout: bytes, stderr: bytes,
                     *, env: dict[str, str] | None = None,
                     budget_seconds: float = DIAGNOSIS_BUDGET_SECONDS,
                     observer=None, git_runner=None) -> dict | None:
    """Return an advisory only for a context-bound index.lock EEXIST failure."""
    if returncode <= 0 or not _LOCK_ERROR.search(stderr):
        return None
    deadline = time.monotonic() + max(0.0, min(float(budget_seconds), DIAGNOSIS_BUDGET_SECONDS))
    process_env = _context_env(env)
    runner = git_runner or _run_git_index
    resolved = runner(argv, cwd, process_env, max(0.0, deadline - time.monotonic()))
    error_path = _lock_path_from_error(stderr)
    if not resolved or not error_path:
        return {"state": "unbound", "message": (
            "Could not bind the reported index.lock failure to this Git context; no path was inspected.")}
    reported = error_path if os.path.isabs(error_path) else os.path.abspath(os.path.join(cwd, error_path))
    default_lock = resolved.get("lock") if isinstance(resolved, dict) else None
    index_path = resolved.get("index") if isinstance(resolved, dict) else str(resolved)
    index_lock = os.path.abspath(index_path + ".lock") if index_path else None
    explicit_locks = []
    explicit_index = process_env.get("GIT_INDEX_FILE")
    if explicit_index:
        for directory in _git_c_directories(argv, cwd):
            path = explicit_index if os.path.isabs(explicit_index) else os.path.join(directory, explicit_index)
            explicit_locks.append(os.path.abspath(path + ".lock"))
    allowed = [path for path in (default_lock, index_lock, *explicit_locks)
               if isinstance(path, str) and path]
    matched = next((path for path in allowed
                    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(reported)), None)
    if matched is None:
        return {"state": "unbound", "message": (
            "The reported lock path differs from this Git context; no path was inspected.")}
    # Git may report the repository's index.lock label for a resolved index
    # symlink. Inspect the actual selected index path; never follow the lock
    # path quoted by stderr.
    if explicit_locks and matched in explicit_locks:
        expected = matched
    else:
        expected = index_lock or matched
    observe = observer or _observe
    observation = observe(expected, max(0.0, deadline - time.monotonic()))
    state = observation.get("state", "unavailable")
    if state == "present" and observation.get("kind") == "symlink":
        observed = "present (symlink; target not followed)"
    elif state == "present":
        observed = (f"present ({observation.get('kind', 'unknown')}, mode {observation.get('mode', 'unknown')}, "
                    f"{observation.get('size', 'unknown')} bytes, mtime_ns {observation.get('mtime_ns', 'unknown')})")
    elif state == "not_found":
        observed = "not found at observation time"
    elif state == "replaced":
        observed = "changed during observation; stable attributes withheld"
    else:
        observed = f"unavailable ({observation.get('reason', 'unknown reason')})"
    checks = _read_only_commands(argv, cwd, process_env, expected)
    check_lines = "\n".join(f"  {command}" for command in checks)
    return {
        "state": state, "lock_path": expected, "observation": observation,
        "message": (
            "Hearting Git index-lock diagnosis\n"
            f"Index lock: {expected}\n"
            f"Observation: {observed}\n"
            "Ownership: unknown. Local process visibility and file age/attributes do not prove "
            "ownership or staleness across NFS hosts and namespaces.\n"
            "No file was changed and Git was not retried. Review active work in this worktree. "
            "Optional read-only checks:\n"
            f"{check_lines}"
        ),
    }


def _text_bytes(value, encoding: str) -> tuple[bytes, bool]:
    if isinstance(value, bytes):
        return value, True
    if isinstance(value, str):
        return value.encode(encoding, errors="replace"), False
    return b"", False


def _response_code(response: dict) -> int | None:
    for key in _RESPONSE_CODES:
        value = response.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _hook_fields(payload: dict, harness: str):
    if harness in {"codex", "claude"}:
        event = payload.get("hook_event_name")
        allowed = {"PostToolUse"} if harness == "codex" else {"PostToolUse", "PostToolUseFailure"}
        if event not in allowed:
            return None
        name, inputs, response, cwd = (
            payload.get("tool_name"), payload.get("tool_input"),
            payload.get("tool_response"), payload.get("cwd"))
        if (harness == "codex" and isinstance(inputs, dict)
                and name in {"exec_command", "functions.exec_command"}):
            command = inputs.get("cmd")
            supplied_cwd = inputs.get("workdir")
            if isinstance(supplied_cwd, str) and supplied_cwd:
                if os.path.isabs(supplied_cwd):
                    cwd = supplied_cwd
                elif isinstance(cwd, str) and cwd:
                    cwd = os.path.join(cwd, supplied_cwd)
                else:
                    cwd = ""
        else:
            command = inputs.get("command") if isinstance(inputs, dict) else None
        if harness == "claude" and not isinstance(response, dict):
            response = payload.get("tool_result")
        if harness == "claude" and event == "PostToolUseFailure":
            if not isinstance(name, str) or name.lower() not in {"bash", "shell"}:
                return None
            error = payload.get("error")
            if not isinstance(error, str):
                return None
            match = re.match(r"^Exit code (-?[0-9]+)(?:\r?\n|$)", error)
            if not match:
                return None
            try:
                code = int(match.group(1))
            except ValueError:
                return None
            return (
                inputs.get("command") if isinstance(inputs, dict) else None,
                cwd, code, b"", error.encode(locale.getpreferredencoding(False) or "utf-8",
                                              errors="replace"), False)
    elif harness == "opencode":
        name, inputs, response, cwd = (
            payload.get("tool_name"), payload.get("tool_input"),
            payload.get("tool_response"), payload.get("cwd"))
        command = inputs.get("command") if isinstance(inputs, dict) else None
    else:
        return None
    if not isinstance(name, str) or name.lower() not in {
        "bash", "shell", "exec_command", "functions.exec_command"
    }:
        return None
    if not isinstance(inputs, dict):
        return None
    if not isinstance(command, str) or not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(response, dict):
        return None
    code = _response_code(response)
    if code is None or code == 0:
        return None
    encoding = locale.getpreferredencoding(False) or "utf-8"
    stdout, stdout_raw = _text_bytes(response.get("stdout", ""), encoding)
    stderr, stderr_raw = _text_bytes(response.get("stderr", ""), encoding)
    raw_available = bool(response.get("raw_bytes_available")) and stdout_raw and stderr_raw
    return command, cwd, code, stdout, stderr, bool(raw_available)


def diagnose_hook_payload(payload: dict, harness: str) -> dict | None:
    fields = _hook_fields(payload, harness)
    if fields is None:
        return None
    command, cwd, code, stdout, stderr, raw_available = fields
    parsed = parse_shell_command(command, cwd)
    if parsed is None:
        return None
    argv, effective_cwd, extra_env = parsed
    diagnosis = diagnose_failure(argv, effective_cwd, code, stdout, stderr, env=extra_env)
    if diagnosis is not None:
        diagnosis["raw_bytes_available"] = raw_available
    return diagnosis


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=("codex", "claude", "opencode"))
    parser.add_argument("--observe", metavar="PATH")
    args = parser.parse_args()
    if args.observe is not None:
        return _observer_main(args.observe)
    try:
        payload = json.load(sys.stdin)
        result = diagnose_hook_payload(payload, args.harness or "") if isinstance(payload, dict) else None
        print(json.dumps({"diagnosis": result}, ensure_ascii=False) if result and result["state"] != "unbound" else "{}")
    except (OSError, ValueError, TypeError, UnicodeError):
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
