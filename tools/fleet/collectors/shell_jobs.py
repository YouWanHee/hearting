"""Shared, passive projections for native background shell jobs.

Native handles identify runtime tasks, never operating-system PIDs. This module
keeps those lifecycle facts separate from targeted process and output evidence.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from collections import OrderedDict


MAX_COMMAND = 120
MAX_READ_PER_TICK = 64 * 1024
MAX_PARTIAL_JSON_LINE = 1024 * 1024
MAX_RETAINED_TASKS = 1024
_JSONL_LIMIT = 4096
_STREAMS = OrderedDict()
_OUTPUTS = OrderedDict()
_VERIFIED_PROCESSES = OrderedDict()
_TASKS = OrderedDict()
_SECRET = re.compile(r"""(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)""")
_SECRET_FLAG = re.compile(r"""(?i)(--?(?:password|passwd|token|secret|api[-_]?key|authorization)(?:=|\s+))(?:"[^"]*"|'[^']*'|[^\s,;]+)""")


def valid_epoch(value, *, milliseconds=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value) / (1000.0 if milliseconds else 1.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def parse_time(value):
    """Parse native ISO timestamps without treating malformed values as now."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return valid_epoch(stamp.astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def command_summary(value):
    if isinstance(value, dict):
        value = value.get("cmd", value.get("command"))
    if not isinstance(value, str):
        return None
    value = "".join(ch if ch >= " " and ch not in "\x7f\x1b" else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip()
    value = _SECRET.sub(r"\1[redacted]", value)
    value = _SECRET_FLAG.sub(r"\1[redacted]", value)
    return value[:MAX_COMMAND] + ("…" if len(value) > MAX_COMMAND else "") if value else None


def _bounded(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def jsonl_delta(path, cache_key, *, max_bytes=MAX_READ_PER_TICK):
    """Read a bounded append-only JSONL range and retain source-generation cursor.

    Cold readers start at byte zero and advance over later ticks. Rotation or
    truncation starts a new generation; no tail-only read silently loses an open
    task. The caller owns its reduced records and must reset them on ``reset``.
    """
    identity = (str(cache_key), os.path.abspath(path))
    try:
        st = os.stat(path)
        stamp = (st.st_dev, st.st_ino)
        size = st.st_size
    except OSError as exc:
        return {"lines": [], "state": "unavailable", "reason": type(exc).__name__,
                "reset": False, "covered": False, "source": identity,
                "generation": None}
    state = _STREAMS.get(identity)
    reset = not isinstance(state, dict) or state.get("stamp") != stamp or size < state.get("offset", 0)
    if isinstance(state, dict) and not reset and state.get("offset", 0) > 0:
        try:
            with open(path, "rb") as stream:
                boundary_start = max(0, state["offset"] - 256)
                stream.seek(boundary_start)
                boundary = stream.read(state["offset"] - boundary_start)
            reset = boundary != state.get("boundary", b"")
        except OSError as exc:
            return {"lines": [], "state": "unavailable", "reason": type(exc).__name__,
                    "reset": False, "covered": False, "source": identity,
                    "generation": state.get("generation")}
    if reset:
        revision = state.get("revision", 0) + 1 if isinstance(state, dict) else 0
        state = {"stamp": stamp, "offset": 0, "carry": b"", "read_error": None,
                 "revision": revision, "boundary": b""}
    start = state["offset"]
    end = min(size, start + max(1, int(max_bytes)))
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            data = stream.read(end - start)
        if len(data) != end - start:
            raise OSError("short-read")
    except OSError as exc:
        state["read_error"] = type(exc).__name__
        _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
        return {"lines": [], "state": "unavailable", "reason": state["read_error"],
                "reset": reset, "covered": False, "source": identity,
                "generation": stamp}
    state["read_error"] = None
    state["offset"] = end
    raw = state["carry"] + data
    pieces = raw.split(b"\n")
    state["carry"] = pieces.pop() if pieces else raw
    lines = []
    for item in pieces:
        if not item:
            continue
        try:
            row = json.loads(item.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            state["gap"] = state.get("gap") or "malformed-json-line"
            continue
        if isinstance(row, dict):
            lines.append(row)
    if len(state["carry"]) > MAX_PARTIAL_JSON_LINE:
        state["carry"] = b""
        state["gap"] = state.get("gap") or "partial-json-line-exceeds-limit"
    try:
        with open(path, "rb") as stream:
            boundary_start = max(0, end - 256)
            stream.seek(boundary_start)
            state["boundary"] = stream.read(end - boundary_start)
    except OSError as exc:
        state["read_error"] = type(exc).__name__
        _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
        return {"lines": lines, "state": "unavailable", "reason": state["read_error"],
                "reset": reset, "covered": False, "source": identity,
                "generation": (stamp[0], stamp[1], state["revision"])}
    _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
    return {"lines": lines, "state": "partial" if state.get("gap") else "observed",
            "reason": state.get("gap"),
            "reset": reset, "covered": end >= size and not state["carry"],
            "offset": end, "size": size, "source": identity,
            "generation": (stamp[0], stamp[1], state["revision"])}


def source_status(path, cache_key):
    """Describe exact source coverage without interpreting a miss as no jobs."""
    identity = (str(cache_key), os.path.abspath(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"state": "unavailable", "reason": type(exc).__name__,
                "coverage": "unavailable", "source": os.path.abspath(path)}
    cursor = _STREAMS.get(identity)
    current = (st.st_dev, st.st_ino)
    if not isinstance(cursor, dict) or cursor.get("stamp") != current:
        return {"state": "catching-up", "reason": "source-not-scanned",
                "coverage": "partial", "offset": 0, "size": st.st_size,
                "source": os.path.abspath(path)}
    if cursor.get("read_error"):
        return {"state": "unavailable", "reason": cursor["read_error"],
                "coverage": "unavailable", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    if cursor.get("gap"):
        return {"state": "partial", "reason": cursor["gap"],
                "coverage": "partial", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    task_state = _TASKS.get(cache_key)
    if isinstance(task_state, dict) and task_state.get("overflow"):
        return {"state": "partial", "reason": "task-retention-limit",
                "coverage": "partial", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    offset = cursor.get("offset", 0)
    complete = offset >= st.st_size and not cursor.get("carry")
    return {"state": "observed" if complete else "catching-up",
            "reason": None if complete else "bounded-source-scan-in-progress",
            "coverage": "complete" if complete else "partial",
            "offset": offset, "size": st.st_size, "source": os.path.abspath(path)}


def _job_key(job):
    return tuple(str(job.get(key) or "") for key in (
        "harness", "session_id", "attempt_id", "source_generation",
        "source_locator", "native_id"))


def merge_jobs(*groups):
    """Merge projections of the same exact native task without cwd/time guesses."""
    merged = OrderedDict()
    for group in groups:
        for item in group or ():
            if not isinstance(item, dict) or not item.get("native_id"):
                continue
            key = tuple(str(item.get(field) or "") for field in (
                "harness", "session_id", "attempt_id", "source_generation", "native_id"))
            merged[key] = {**merged.get(key, {}), **item}
    return list(merged.values())


def _row_time(row):
    if not isinstance(row, dict):
        return None
    value = row.get("timestamp") or row.get("created_at") or row.get("time_created")
    parsed = parse_time(value)
    return parsed if parsed is not None else valid_epoch(
        value, milliseconds=isinstance(value, (int, float)) and value > 100_000_000_000)


def _task_cache(cache_key):
    state = _TASKS.get(cache_key)
    if not isinstance(state, dict):
        state = {"calls": {}, "jobs": {}, "handles": {}}
    _TASKS[cache_key] = state
    _TASKS.move_to_end(cache_key)
    while len(_TASKS) > _JSONL_LIMIT:
        _TASKS.popitem(last=False)
    return state


def _store_task(state, key, item):
    state["jobs"].pop(key, None)
    state["jobs"][key] = item
    while len(state["jobs"]) > MAX_RETAINED_TASKS:
        state["jobs"].pop(next(iter(state["jobs"])))
        state["overflow"] = True


def _claude_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content
                          if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


_CLAUDE_TASK_PATH = re.compile(r"(/[^\s\"'`<>]*/tasks/([A-Za-z0-9_-]{4,})\.output)")
_CLAUDE_TASK_ID = re.compile(r"(?i)\btask(?:\s+id)?\s*[:=]\s*[`\"']?([A-Za-z0-9_-]{4,})")


def _claude_task_ack(text, *, background_requested=False):
    lower = text.lower()
    background_ack = ("background" in lower
                      and any(word in lower for word in ("running", "started", "launched")))
    match = _CLAUDE_TASK_PATH.search(text)
    if match and (background_requested or background_ack):
        return match.group(2), match.group(1)
    if not background_requested or not background_ack:
        return None, None
    match = _CLAUDE_TASK_ID.search(text)
    return (match.group(1), None) if match else (None, None)


def read_claude_jobs(path, *, session_id, attempt_id=None, now=None):
    """Read exact Claude Bash acknowledgements from an incrementally scanned transcript."""
    source_key = ("claude", os.path.abspath(path), session_id, attempt_id)
    state = _task_cache(source_key)
    delta = jsonl_delta(path, source_key)
    if delta.get("reset"):
        if state["jobs"]:
            retired = list(state.get("retired_jobs", ()))
            for old in state["jobs"].values():
                item = dict(old)
                item.update(observation_state="unknown",
                            observation_reason="source-generation-changed",
                            process_state="unknown",
                            process_reason="source-generation-changed")
                retired.append(item)
            state["overflow"] = state.get("overflow", False) or len(retired) > 256
            state["retired_jobs"] = retired[-256:]
        state["calls"].clear()
        state["jobs"].clear()
    for row in delta.get("lines", ()):
        declared_sid = row.get("session_id") or row.get("sessionId")
        if isinstance(declared_sid, str) and declared_sid != session_id:
            continue
        stamp = _row_time(row)
        row_type = row.get("type")
        message = row.get("message") if isinstance(row.get("message"), dict) else row
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and row_type in ("assistant", "user"):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if row_type == "assistant" and part.get("type") == "tool_use" and part.get("name") == "Bash":
                    call_id = part.get("id")
                    inputs = part.get("input") if isinstance(part.get("input"), dict) else {}
                    if isinstance(call_id, str) and call_id:
                        state["calls"][call_id] = {
                            "command": command_summary(inputs.get("command")),
                            "started_at": stamp,
                            "run_in_background": inputs.get("run_in_background") is True,
                        }
                elif row_type == "user" and part.get("type") == "tool_result":
                    call_id = part.get("tool_use_id")
                    call = state["calls"].get(call_id)
                    if not isinstance(call, dict):
                        continue
                    task_id, output_path = _claude_task_ack(
                        _claude_result_text(part.get("content")),
                        background_requested=call.get("run_in_background") is True)
                    if not task_id:
                        continue
                    _store_task(state, task_id, {
                        "harness": "claude", "session_id": session_id,
                        "attempt_id": attempt_id, "source_generation": str(delta.get("generation")),
                        "source_locator": os.path.abspath(path), "native_id": task_id,
                        "native_call_id": call_id, "native_task_id": task_id,
                        "command": call.get("command"),
                        "command_source": "Bash.input.command" if call.get("command") else None,
                        "started_at": call.get("started_at"),
                        "start_source": "Bash.tool_use.timestamp", "observed_at": stamp,
                        "native_status": "acknowledged", "process_state": "unknown",
                        "process_reason": "task-ack-is-not-process-proof",
                        "output_path": output_path,
                        "output_reason": None if output_path else "ack-has-no-exact-output-path",
                        "output_coverage": "sampled" if output_path else "unavailable",
                        "observation_state": delta.get("state"),
                        "observation_reason": delta.get("reason"),
                    })
    result = []
    for item in list(state.get("retired_jobs", ())) + list(state["jobs"].values()):
        current = dict(item)
        current["observed_at"] = time.time() if now is None else now
        if delta.get("state") != "observed":
            current["observation_state"] = delta.get("state")
            current["observation_reason"] = delta.get("reason")
        normalized = normalize_job(current, now=now)
        if normalized is not None:
            result.append(normalized)
    return result


def _codex_payload(row):
    if not isinstance(row, dict):
        return None
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else row


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _codex_output_objects(value):
    result = []
    if isinstance(value, dict):
        result.append(value)
    elif isinstance(value, str):
        parsed = _json_object(value)
        if parsed is not None:
            result.append(parsed)
    elif isinstance(value, list):
        for part in value:
            if isinstance(part, dict):
                candidate = _json_object(part.get("text"))
                if candidate is not None:
                    result.append(candidate)
    return result


def read_codex_jobs(path, *, session_id, attempt_id=None, now=None):
    """Project exact exec_command/write_stdin lifecycle envelopes from a Codex rollout."""
    source_key = ("codex", os.path.abspath(path), session_id, attempt_id)
    state = _task_cache(source_key)
    delta = jsonl_delta(path, source_key)
    if delta.get("reset"):
        if state["jobs"]:
            retired = list(state.get("retired_jobs", ()))
            for old in state["jobs"].values():
                item = dict(old)
                item.update(observation_state="unknown",
                            observation_reason="source-generation-changed",
                            process_state="unknown",
                            process_reason="source-generation-changed")
                retired.append(item)
            state["overflow"] = state.get("overflow", False) or len(retired) > 256
            state["retired_jobs"] = retired[-256:]
        state["calls"].clear()
        state["jobs"].clear()
        state["handles"].clear()
    for row in delta.get("lines", ()):
        payload = _codex_payload(row)
        if not isinstance(payload, dict):
            continue
        declared_sid = (row.get("thread_id") or row.get("session_id")
                        or payload.get("thread_id")) if isinstance(row, dict) else None
        if isinstance(declared_sid, str) and declared_sid != session_id:
            continue
        kind = payload.get("type")
        call_id = payload.get("call_id") or payload.get("id")
        name = payload.get("name")
        stamp = _row_time(row) or _row_time(payload)
        if kind in {"custom_tool_call", "function_call"} and isinstance(call_id, str):
            args = _json_object(payload.get("input", payload.get("arguments")))
            if name in {"exec_command", "command_execution", "shell", "write_stdin",
                        "functions.exec", "functions.write_stdin"}:
                handle = args.get("session_id") if isinstance(args, dict) else None
                target = state["handles"].get(str(handle)) if handle is not None else None
                state["calls"][call_id] = {
                    "call_id": call_id, "name": name, "args": args,
                    "started_at": stamp,
                    "handle": str(handle) if handle is not None else None,
                    "job_key": target,
                }
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            call = state["calls"].get(call_id)
            if not isinstance(call, dict):
                continue
            raw_output = payload.get("output")
            objects = _codex_output_objects(raw_output)
            if not objects:
                objects = [{}]
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            for detail in objects:
                exit_code = detail.get("exit_code")
                status_value = str(detail.get("status") or "").lower()
                terminal = ((isinstance(exit_code, int) and not isinstance(exit_code, bool))
                            or (call.get("name") not in {"functions.exec", "functions.write_stdin"}
                                and status_value in {"exited", "completed", "failed",
                                                     "cancelled", "canceled"}))
                handle_value = detail.get("session_id")
                # The installed tool envelope uses an integer exec session handle.
                # Never reinterpret a model thread id, cell id, or arbitrary string as it.
                handle = (str(handle_value) if isinstance(handle_value, int)
                          and not isinstance(handle_value, bool) else call.get("handle"))
                if handle is None and not terminal:
                    continue
                job_key = ("handle:" + handle if handle is not None
                           else call.get("job_key") or "call:" + str(call_id))
                prior = state["jobs"].get(job_key, {})
                command = (args.get("cmd", args.get("command"))
                           if isinstance(args, dict) else None)
                if not command:
                    command = prior.get("command")
                # An outer functions.exec script may contain arbitrary code that
                # starts several commands. Its source is not a safe command parser.
                if call.get("name") in {"functions.exec", "functions.write_stdin"}:
                    command = prior.get("command")
                nested_output = detail.get("output")
                output_seen = isinstance(nested_output, str) and bool(nested_output)
                if (detail == {} and call.get("name") in {"write_stdin", "functions.write_stdin"}
                        and isinstance(raw_output, list)):
                    output_seen = any(isinstance(part, dict)
                                      and isinstance(part.get("text"), str)
                                      and bool(part.get("text")) for part in raw_output)
                if not objects or detail == {}:
                    if call.get("name") not in {"write_stdin", "functions.write_stdin"}:
                        output_seen = False
                started = prior.get("started_at") or call.get("started_at")
                job = dict(prior)
                job.update({
                    "harness": "codex", "session_id": session_id, "attempt_id": attempt_id,
                    "source_generation": str(delta.get("generation")),
                    "source_locator": os.path.abspath(path), "native_id": job_key,
                    "native_call_id": call_id, "continuation_handle": handle,
                    "command": command_summary(command),
                    "command_source": prior.get("command_source") or (
                        ("exec_command.arguments.cmd" if args.get("cmd") is not None
                         else "exec_command.arguments.command") if command else None),
                    "command_reason": None if command else "nested-command-unavailable",
                    "started_at": started, "start_source": "native-call.timestamp",
                    "observed_at": stamp,
                    "native_status": "completed" if terminal else "running" if handle is not None else "unknown",
                    "completed_at": stamp if terminal else prior.get("completed_at"),
                    "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else prior.get("exit_code"),
                    "process_state": "unknown", "process_reason": "codex-handle-is-not-a-pid",
                    "output_coverage": "sampled", "output_reason": "poll-response-is-sampled",
                    "last_output_at": stamp if output_seen else prior.get("last_output_at"),
                    "last_output_observed_at": stamp,
                    "observation_state": delta.get("state"),
                    "observation_reason": delta.get("reason"),
                })
                _store_task(state, job_key, job)
                if handle is not None:
                    state["handles"][handle] = job_key
                call["job_key"] = job_key
                call["handle"] = handle
    result = []
    for item in list(state.get("retired_jobs", ())) + list(state["jobs"].values()):
        current = dict(item)
        current["observed_at"] = time.time() if now is None else now
        if state.get("overflow"):
            current["observation_state"] = "partial"
            current["observation_reason"] = "task-retention-limit"
        if delta.get("state") != "observed":
            current["observation_state"] = delta.get("state")
            current["observation_reason"] = delta.get("reason")
        normalized = normalize_job(current, now=now)
        if normalized is not None:
            result.append(normalized)
    return result


def normalize_job(job, *, now=None):
    """Sanitize a native projection and compute time/output advisory fields."""
    if not isinstance(job, dict):
        return None
    now = time.time() if now is None else valid_epoch(now)
    if now is None:
        return None
    result = dict(job)
    result["command"] = command_summary(result.get("command"))
    result["started_at"] = valid_epoch(result.get("started_at"))
    observed_at = valid_epoch(result.get("observed_at"))
    result["observed_at"] = observed_at if observed_at is not None else now
    result["observation_time_state"] = (
        "future" if observed_at is not None and observed_at > now
        else "valid" if observed_at is not None else "collection-time")
    result["observation_time_reason"] = (
        "future-observation-time" if observed_at is not None and observed_at > now
        else "missing-or-invalid-observation-time" if observed_at is None else None)
    completed_at = valid_epoch(result.get("completed_at"))
    result["completed_at"] = completed_at if completed_at is not None and completed_at <= now else None
    result["completion_time_state"] = (
        "future" if completed_at is not None and completed_at > now
        else "valid" if completed_at is not None else "unavailable")
    result["completion_time_reason"] = (
        "future-completion-time" if completed_at is not None and completed_at > now
        else None)
    if result.get("exit_code") is not None and (
            not isinstance(result["exit_code"], int) or isinstance(result["exit_code"], bool)):
        result["exit_code"] = None
        result["exit_code_reason"] = "invalid-exit-code"
    started = result["started_at"]
    result["elapsed_s"] = max(0, int(now - started)) if started is not None and started <= now else None
    reason = result.get("observation_reason")
    if started is None:
        result["time_state"] = "start-unavailable"
        result["time_reason"] = reason or "missing-or-invalid-start"
    elif started > now:
        result["time_state"] = "invalid"
        result["time_reason"] = "future-start"
        result["elapsed_s"] = None
    else:
        result["time_state"] = "valid"
        result["time_reason"] = None

    status = str(result.get("native_status") or "unknown").lower()
    terminal = status in {"completed", "complete", "exited", "failed", "error", "cancelled", "canceled"}
    if result["completed_at"] is not None or terminal or result.get("exit_code") is not None:
        result["lifecycle_state"] = "completed"
    elif status in {"running", "in_progress", "ongoing", "background"}:
        result["lifecycle_state"] = "running"
    else:
        result["lifecycle_state"] = "unknown"

    output_path = result.get("output_path")
    output_reason = result.get("output_reason")
    if isinstance(output_path, str) and output_path:
        try:
            st = os.stat(output_path)
            if not stat.S_ISREG(st.st_mode):
                raise OSError("not-regular-file")
            output_id = _job_key(result) + (output_path,)
            sequence = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            previous = _OUTPUTS.get(output_id)
            continuous = bool(previous and previous[0][:2] == sequence[:2]
                              and st.st_size >= previous[0][2])
            changed = not previous or previous[0] != sequence
            observed = now
            mtime = valid_epoch(st.st_mtime)
            if st.st_size == 0:
                last_output = None
            elif mtime is not None and mtime <= now:
                last_output = mtime
            elif previous:
                last_output = previous[1]
            else:
                last_output = None
            if changed and (not previous or previous[0][:2] != sequence[:2]
                            or st.st_size < previous[0][2]):
                quiet_since = max(started or observed, observed)
            else:
                quiet_since = previous[2] if previous and continuous else observed
            _bounded(_OUTPUTS, output_id, (sequence, last_output, quiet_since), _JSONL_LIMIT * 8)
            result.update(output_coverage="continuous-file" if continuous else "sampled",
                          output_state="observed", output_size=st.st_size,
                          output_identity={"device": st.st_dev, "inode": st.st_ino},
                          last_output_at=last_output,
                          last_output_observed_at=observed,
                          quiet_since=quiet_since,
                          output_time_state=("future" if mtime is not None and mtime > now
                                             else "invalid" if mtime is None else "valid"),
                          output_time_reason=("future-output-mtime" if mtime is not None and mtime > now
                                              else "invalid-output-mtime" if mtime is None else None),
                          output_reason=None)
        except OSError as exc:
            result.update(output_coverage="unavailable", output_state="unavailable",
                          output_reason=type(exc).__name__, last_output_observed_at=now)
    else:
        result.update(output_coverage=result.get("output_coverage") or "unavailable",
                      output_state="unavailable",
                      output_reason=output_reason or "native-output-source-unavailable",
                      last_output_observed_at=now)
    process_state = result.get("process_state")
    if process_state not in {"alive", "dead", "unknown"}:
        result["process_state"] = "unknown"
        result["process_reason"] = "process-state-invalid"
    last_output = valid_epoch(result.get("last_output_at"))
    quiet_since = valid_epoch(result.get("quiet_since"))
    output_age = now - last_output if last_output is not None else None
    observation_age = now - quiet_since if quiet_since is not None else None
    quiet_age = (min(output_age, observation_age) if output_age is not None
                 and observation_age is not None else observation_age)
    result["quiet_suspected"] = bool(
        result["lifecycle_state"] != "completed"
        and result.get("output_coverage") == "continuous-file"
        and (result.get("process_state") == "alive"
             or result.get("lifecycle_state") == "running")
        and quiet_age is not None and quiet_age >= 600
        and quiet_since is not None and quiet_since <= now)
    result.setdefault("process_state", "unknown")
    result.setdefault("process_reason", "native-handle-is-not-a-pid")
    result["elapsed_s"] = result.get("elapsed_s")
    return result


def _pid_namespace(pid):
    try:
        return os.readlink(f"/proc/{int(pid)}/ns/pid")
    except (OSError, TypeError, ValueError):
        return None


def _proc_start(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="ascii") as stream:
            raw = stream.read()
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (OSError, IndexError, TypeError, ValueError):
        return None


def _fd_is_writer(pid, output_path):
    try:
        target = os.stat(output_path)
        for fd in os.listdir(f"/proc/{int(pid)}/fd"):
            try:
                opened = os.stat(f"/proc/{int(pid)}/fd/{fd}")
                if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
                    continue
                with open(f"/proc/{int(pid)}/fdinfo/{fd}", encoding="ascii") as stream:
                    info = stream.read()
                flags_line = next((line for line in info.splitlines() if line.startswith("flags:")), None)
                flags = int(flags_line.split()[1], 8) if flags_line else 0
                if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                    return True
            except (OSError, ValueError, IndexError):
                continue
    except (OSError, TypeError, ValueError):
        return False
    return False


def correlate_output_writers(job, candidates, *, namespace=None, root_pid=None):
    """Bind a job only to a writable FD on an exact, identity-checked descendant."""
    path = job.get("output_path") if isinstance(job, dict) else None
    if not isinstance(path, str) or not path:
        return {"process_state": "unknown", "process_reason": "no-exact-output-writer-locator"}
    if not isinstance(namespace, str) or not namespace:
        return {"process_state": "unknown", "process_reason": "parent-pid-namespace-unavailable"}
    matched = []
    for candidate in candidates or ():
        if not isinstance(candidate, dict) or candidate.get("ownership_verified") is not True:
            continue
        ancestry = candidate.get("ancestry") if isinstance(candidate.get("ancestry"), list) else []
        if (not ancestry or ancestry[0].get("pid") != root_pid
                or ancestry[-1].get("pid") != candidate.get("pid")
                or candidate.get("root_pid") != root_pid):
            continue
        pid, start = candidate.get("pid"), candidate.get("proc_start")
        current_ns = _pid_namespace(pid) if pid else None
        if not pid or not start or _proc_start(pid) != str(start):
            continue
        ancestry_namespaces = [_pid_namespace(item.get("pid")) for item in ancestry
                               if isinstance(item, dict) and item.get("pid")]
        if current_ns != namespace or any(value != namespace for value in ancestry_namespaces):
            continue
        if _fd_is_writer(pid, path):
            matched.append({"pid": pid, "start": str(start), "namespace": current_ns,
                            "ancestry": candidate.get("ancestry")})
    if len(matched) == 1:
        identity = matched[0]
        _bounded(_VERIFIED_PROCESSES, _job_key(job), identity, _JSONL_LIMIT * 8)
        return {"process_state": "alive", "process_reason": "exact-descendant-writable-output-fd",
                "process_identity": identity}
    if len(matched) > 1:
        return {"process_state": "unknown", "process_reason": "multiple-output-writers"}
    previous = _VERIFIED_PROCESSES.get(_job_key(job))
    if previous and (_proc_start(previous["pid"]) != previous["start"]
                     or _pid_namespace(previous["pid"]) != previous["namespace"]):
        return {"process_state": "dead", "process_reason": "verified-process-generation-gone",
                "process_identity": previous}
    return {"process_state": "unknown", "process_reason": "exact-writer-not-observed"}


def observe_jobs(jobs, *, now=None, parent_pid=None, parent_start=None, attempt_id=None,
                 tree=None, kids=None):
    """Normalize jobs and add only targeted exact output-writer process evidence."""
    jobs = list(jobs or ())
    now = time.time() if now is None else now
    candidates = []
    needs_candidates = any(isinstance(job, dict) and job.get("output_path") for job in jobs)
    parent_namespace = None
    if needs_candidates and parent_pid and parent_start and tree is not None and kids is not None:
        try:
            parent_namespace = _pid_namespace(parent_pid)
            from . import procscan
            candidates = procscan.exec_children(
                parent_pid, tree=tree, kids=kids, min_age=0, expected_start=parent_start,
                attempt_id=attempt_id)
        except Exception:
            candidates = []
    result = []
    for raw in jobs or ():
        item = normalize_job(raw, now=now)
        if item is None:
            continue
        if item.get("output_path"):
            item.update(correlate_output_writers(
                item, candidates, namespace=parent_namespace, root_pid=parent_pid))
        result.append(item)
    return result


def shell_job_strip_label(job):
    """Compact, safe display text shared by interactive and dispatch rows."""
    command = job.get("command") or "command unavailable"
    status = ("exit %s" % job["exit_code"] if job.get("exit_code") is not None
              else "complete" if job.get("lifecycle_state") == "completed"
              else "alive" if job.get("process_state") == "alive"
              else "process dead" if job.get("process_state") == "dead"
              else "live ?")
    parts = [command]
    started = valid_epoch(job.get("started_at"))
    if started is not None:
        try:
            parts.append("start " + time.strftime("%H:%M", time.localtime(started)))
        except (OverflowError, OSError, ValueError):
            parts.append("start ?")
    else:
        parts.append("start ?")
    elapsed = job.get("elapsed_s")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool):
        parts.append("%ds" % elapsed if elapsed < 60 else "%dm" % (elapsed // 60))
    parts.append(status)
    if job.get("quiet_suspected") and job.get("lifecycle_state") != "completed":
        parts.append("quiet ≥10m?")
    elif job.get("output_coverage") == "sampled":
        parts.append("output sampled")
    elif job.get("output_coverage") == "unavailable":
        parts.append("output ?")
    elif job.get("last_output_at") is not None:
        age = job.get("last_output_observed_at")
        parts.append("output observed")
    return " · ".join(parts)
