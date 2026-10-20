"""Fleet-independent, read-only owner attempt transcript viewer."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import NamedTuple


class Line(NamedTuple):
    kind: str
    text: str


class Source(NamedTuple):
    path: Path
    harness: str
    kind: str = "log"
    session_id: str | None = None
    label: str = ""


class Cursor:
    """Small in-memory file cursor; no viewer state is persisted."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.identity = None
        self.head = b""
        self.notices: list[str] = []
        self.reader_state = {"cursor": 0, "refresh": [], "rows": {}, "entities": {}}

    def read_new(self, *, final: bool = False) -> list[str]:
        try:
            info = self.path.stat()
            identity = (info.st_dev, info.st_ino)
            rewrite = False
            if self.identity == identity and self.head:
                with self.path.open("rb") as probe:
                    rewrite = probe.read(len(self.head)) != self.head
            if identity != self.identity or info.st_size < self.offset or rewrite:
                if self.identity is not None:
                    self.notices.append("source truncated or replaced; reopened")
                self.offset, self.pending, self.identity = 0, b"", identity
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
            with self.path.open("rb") as probe:
                self.head = probe.read(min(self.offset, 4096))
        except OSError:
            return []
        pieces = (self.pending + chunk).split(b"\n")
        self.pending = pieces.pop()
        rows = [piece.decode("utf-8", "replace") for piece in pieces]
        if final and self.pending:
            rows.append(self.pending.decode("utf-8", "replace"))
            self.pending = b""
        return rows


@dataclass
class RegistryReader:
    """One in-memory snapshot per registry, refreshed on change and at most 1/s."""
    path: Path
    lines: list[str] = field(default_factory=list)
    stamp: tuple[int, int] | None = None
    checked: float = 0.0

    def read(self, contains: str | None = None, *, now=None, force=False):
        now = time.monotonic() if now is None else now
        try:
            stat = self.path.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            return []
        if force or self.stamp != stamp and now - self.checked >= 1 or self.stamp is None:
            try:
                self.lines = self.path.read_text(errors="replace").splitlines()
                self.stamp = stamp
                self.checked = now
            except OSError:
                return []
        return [line for line in self.lines if contains is None or contains in line]


_REGISTRY_READERS: dict[str, RegistryReader] = {}


def _registry_rows(root, contains=None, *, force=False):
    path = (Path(root) / "jobs.log").resolve()
    reader = _REGISTRY_READERS.setdefault(str(path), RegistryReader(path))
    return reader.read(contains, force=force)


_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/?#\s@]*@")
_BEARER_TOKEN = re.compile(r"\b(Bearer)\s+[^\s]+", re.I)


def sanitize(value: object) -> str:
    """Remove terminal controls and mask URL userinfo and bearer credentials."""
    text = str(value)
    out = []
    i = 0
    while i < len(text):
        code = ord(text[i])
        if code == 0x1b:
            i += 1
            if i >= len(text):
                break
            kind = text[i]
            i += 1
            if kind == "[":
                while i < len(text) and not (0x40 <= ord(text[i]) <= 0x7e):
                    i += 1
                i += i < len(text)
            elif kind in "]P^_X":
                while i < len(text):
                    if text[i] == "\a":
                        i += 1
                        break
                    if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "\\":
                        i += 2
                        break
                    i += 1
            continue
        if 0x80 <= code <= 0x9f:
            if code == 0x9b:
                i += 1
                while i < len(text) and not (0x40 <= ord(text[i]) <= 0x7e):
                    i += 1
                i += i < len(text)
                continue
            i += 1
            if code in (0x90, 0x98, 0x9d, 0x9e, 0x9f):
                while i < len(text) and ord(text[i]) != 0x9c:
                    i += 1
                i += i < len(text)
            continue
        if code < 32 and text[i] not in "\n\t" or code == 127:
            i += 1
            continue
        out.append(" " if text[i] == "\t" else text[i])
        i += 1
    clean = "".join(out)
    clean = _URL_USERINFO.sub(r"\1[redacted]@", clean)
    return _BEARER_TOKEN.sub(r"\1 [redacted]", clean)


def _compact(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


_HIDDEN_TYPES = {"token_usage", "thinking_tokens", "tool_progress", "api-request-blob",
                 "cost-state", "queue-operation", "dispatch.supervisor.token_usage",
                 "supervisor.token_usage", "last-prompt", "atis-latch", "mode",
                 "api-request", "api-request-shape"}


def _supervisor_type(kind):
    if kind.startswith("dispatch.supervisor."):
        kind = kind[len("dispatch.supervisor."):]
    elif kind.startswith("supervisor."):
        kind = kind[len("supervisor."):]
    return kind.replace("-", ".")


def format_record(harness: str, record: object) -> list[Line]:
    """Convert a native record to safe display lines without truncating content."""
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except (ValueError, TypeError):
            return [Line("raw", sanitize(record))]
    if not isinstance(record, dict):
        return [Line("raw", sanitize(_compact(record)))]
    kind = str(record.get("type") or record.get("kind") or record.get("method") or "unknown")
    if kind in _HIDDEN_TYPES or _supervisor_type(kind) == "token.usage":
        return []
    if kind.startswith(("dispatch.supervisor.", "supervisor.")):
        event = _supervisor_type(kind)
        known = {"turn.started", "turn.completed", "session", "session.announced", "parked", "resumed",
                 "join", "terminal"}
        label = event if event in known else f"event {event}"
        return [Line("supervisor", sanitize(f"supervisor: {label}"))]
    result: list[Line] = []
    def add(label, value, category):
        if value is not None and value != "":
            if isinstance(value, (dict, list)):
                value = _compact(value)
            result.append(Line(category, sanitize(f"{label}: {value}")))
    def add_tool_input(name, value):
        label = "tool " + str(name)
        if not isinstance(value, dict):
            add(label, value, "tool")
            return
        is_shell = str(name).casefold() == "bash"
        command = value.get("command") if is_shell else None
        if command is not None:
            result.append(Line("tool", sanitize(f"{label}:\n{command}")))
        else:
            result.append(Line("tool", sanitize(label + ":")))
        for key, argument in value.items():
            if is_shell and key == "command":
                continue
            if isinstance(argument, str) and "\n" in argument:
                result.append(Line("tool", sanitize(f"{key}:\n{argument}")))
            else:
                rendered = argument if isinstance(argument, str) else _compact(argument)
                result.append(Line("tool", sanitize(f"{key}: {rendered}")))
    if kind == "notice":
        add("notice", record.get("text") or record.get("reason"), "notice")
    elif harness == "claude":
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if kind == "attachment":
            attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else record
            add("attachment", attachment.get("type") or "unknown", "attachment")
        elif kind == "rate_limit_event":
            fields = [f"{key}={record[key]}" for key in ("status", "resets_at", "reset_at", "resetsAt", "resetAt", "rate_limit_type")
                      if record.get(key) is not None]
            add("rate_limit_event", " ".join(fields) or "status unknown", "notice")
        elif kind in ("assistant", "user") or record.get("message"):
            content = message.get("content")
            if isinstance(content, str):
                add("assistant" if kind == "assistant" else "user", content, "message" if kind == "assistant" else "prompt")
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        add("content", part, "raw"); continue
                    ptype = part.get("type")
                    if ptype == "text": add("assistant" if kind == "assistant" else "user", part.get("text"), "message" if kind == "assistant" else "prompt")
                    elif ptype == "thinking": add("thinking", part.get("thinking"), "thinking")
                    elif ptype == "tool_use":
                        add_tool_input(part.get("name", ""), part.get("input"))
                    elif ptype == "tool_result": add("tool output", part.get("content"), "output")
                    elif ptype == "attachment": add("attachment", part.get("type") or "unknown", "attachment")
                    else: add(str(ptype or "content"), part, "unknown")
        elif kind == "result": add("result", record, "result")
    elif harness == "codex":
        item = record.get("item") if isinstance(record.get("item"), dict) else record
        itype = str(item.get("type") or kind)
        if kind in ("error", "turn.failed") or itype == "error":
            message = (record.get("message") or record.get("error") or record.get("reason")
                       or item.get("message") or item.get("error") or item.get("text"))
            add("error", message or "Codex reported an error", "notice")
        elif itype in ("agent_message", "user_message", "userMessage"):
            add("assistant" if "agent" in itype else "user", item.get("text") or item.get("message"), "message" if "agent" in itype else "prompt")
            if not result:
                add("userMessage", "본문 없음", "prompt")
        elif itype in ("command_execution", "commandExecution"):
            add("command", item.get("command") or item.get("cmd"), "tool")
            add("output", item.get("aggregated_output") or item.get("aggregatedOutput") or item.get("output"), "output")
            exit_code = item.get("exit_code") if item.get("exit_code") is not None else item.get("exitCode")
            add("exit", exit_code, "result")
        elif itype in ("reasoning",):
            add("reasoning", item.get("text"), "thinking")
        elif itype in ("file_change", "fileChange"):
            changes = item.get("changes") or item.get("files")
            if isinstance(changes, list) and changes:
                for change in changes:
                    if isinstance(change, dict):
                        path = (change.get("path") or change.get("file") or change.get("filename")
                                or change.get("new_path") or change.get("old_path"))
                        kind_name = (change.get("kind") or change.get("type") or change.get("change_type")
                                     or change.get("status"))
                        add("file change", " ".join(str(x) for x in (kind_name, path) if x), "result")
                    else:
                        add("file change", change, "result")
            else:
                add("file change", item.get("text") or item.get("id") or "change recorded", "result")
        elif itype in ("web_search", "webSearch"):
            add("web search", item.get("query") or item.get("search_query") or item.get("searchQuery") or item.get("text"), "tool")
        elif kind in ("thread.started", "turn.started", "turn.completed"):
            add(kind, record.get("thread_id") or record.get("turn_id") or record.get("status") or "", "result")
            if not result: result.append(Line("result", kind))
        elif itype in ("sleep", "contextCompaction", "imageView", "subAgentActivity",
                       "collabAgentToolCall", "mcpToolCall"):
            keys = {
                "sleep": ("duration_ms", "durationMs", "duration", "ms"),
                "contextCompaction": ("summary", "reason", "trigger", "tokens"),
                "imageView": ("path", "url", "detail"),
                "subAgentActivity": ("agent_id", "agentId", "status", "message"),
                "collabAgentToolCall": ("tool", "name", "status", "call_id", "callId"),
                "mcpToolCall": ("server", "tool", "name", "status"),
            }[itype]
            fields = [f"{key}={_compact(item[key])[:100]}" for key in keys if item.get(key) is not None]
            detail = " ".join(fields)[:240] if fields else "recorded"
            add(itype, detail, "result")
        elif "text" in item:
            add(itype, item.get("text"), "message")
    elif harness == "opencode":
        part = record.get("part") if isinstance(record.get("part"), dict) else record
        if kind in ("text", "message", "assistant", "user"):
            value = part.get("text") or part.get("content")
            add(kind, value, "prompt" if kind == "user" else "message")
        elif kind in ("tool_use", "tool"):
            state = part.get("state") if isinstance(part.get("state"), dict) else part
            name = part.get("name") or part.get("tool") or ""
            status = state.get("status")
            tool_input = state.get("input")
            if tool_input is not None:
                add_tool_input(name, tool_input)
            elif status is not None:
                result.append(Line("result", sanitize(f"tool {name} {status}")))
            output = state.get("output") or state.get("error")
            if isinstance(output, dict): output = output.get("message") or output.get("text") or _compact(output)
            if isinstance(output, list):
                output = "\n".join(str(block.get("text", "")) for block in output
                                    if isinstance(block, dict) and block.get("type") == "text")
            add("output", output, "output")
        elif kind in ("step_start", "step-start"):
            pass
        elif kind in ("step_finish", "step-finish"):
            state = part.get("state") if isinstance(part.get("state"), dict) else part
            reason = state.get("reason") or part.get("reason")
            totals = state.get("tokens") or part.get("tokens") or state.get("cost") or part.get("cost")
            summary = " ".join(str(value) for value in (reason, totals) if value not in (None, "", {}))
            add("step_finish", summary or "completed", "result")
        elif kind == "reasoning":
            add("thinking", part.get("text") or part.get("reason"), "thinking")
        elif kind == "result":
            add("result", part.get("text") or part.get("message") or part.get("status") or "completed", "result")
    silent = (kind in ("system", "init", "heartbeat", "step_start", "step-start")
              or harness == "claude" and kind == "assistant"
              and isinstance(message.get("content"), list)
              and all(isinstance(part, dict) and part.get("type") == "thinking"
                      and not part.get("thinking") for part in message["content"])
              or harness == "codex" and itype == "reasoning"
              or kind in _HIDDEN_TYPES)
    if not result and not silent:
        result.append(Line("unknown", sanitize(f"· {kind} {_compact(record)}")))
    return result


def owned_attempt_log_path(job, *, fallback_home=None):
    """Shared path admission used by Fleet's compatibility adapter."""
    raw = getattr(job, "_log_file", None)
    attempt_id, harness = getattr(job, "attempt_id", None), getattr(job, "harness", None)
    if harness not in ("claude", "codex", "opencode") or not raw or not attempt_id:
        return None
    path = os.path.realpath(raw)
    roots = []
    registry_path = getattr(job, "_registry_path", None)
    if registry_path:
        registry_dir = os.path.dirname(os.path.realpath(registry_path))
        # Registry sibling logs are the canonical local dispatch log owner.
        roots.append(os.path.join(registry_dir, "logs"))
        if os.path.basename(registry_dir) == "dispatch":
            # Installed registries live under <home>/.dispatch; their log sibling is shared.
            roots.append(os.path.join(os.path.dirname(registry_dir), ".dispatch", "logs"))
    artifact_root = getattr(job, "artifact_root", None)
    if artifact_root:
        ar = os.path.realpath(artifact_root)
        roots.extend((ar, os.path.join(os.path.dirname(ar), ".dispatch", "logs")))
        # Managed gateway artifacts are nested below the canonical .dispatch/logs root.
    launch_home = getattr(job, "_launch_home", None)
    if launch_home:
        # SD-49 seals launch_home for isolated worker dispatches.
        roots.append(os.path.join(os.path.realpath(launch_home), ".dispatch", "logs"))
    if not roots and fallback_home:
        # Legacy fallback applies only to rows with no owning root.
        roots.append(os.path.realpath(os.path.join(fallback_home, ".dispatch", "logs")))
    if not any(_within(root, path) for root in roots):
        return None
    base = os.path.basename(path)
    if not base.endswith(f".{harness}.jsonl") or f".{attempt_id}." not in base:
        return None
    return path if os.path.isfile(path) else None


def _within(root, path):
    try:
        return os.path.commonpath((root, path)) == root
    except (TypeError, ValueError):
        return False


def _metadata(line):
    fields = line.rstrip("\n").split("\t", 5)
    if len(fields) != 6:
        return None
    values = {}
    for pair in fields[5].split(","):
        key, sep, value = pair.partition("=")
        if sep: values[key.strip()] = value
    return fields, values


def _dispatch_roots():
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    home = Path(os.environ.get("AGENT_HOME") or resolve_agent_home())
    roots = dispatch_state_roots(home)
    return tuple(dict.fromkeys(Path(root) for root in roots))


def _find_attempt(attempt):
    prefix_matches=[]
    for root in _dispatch_roots():
        jobs = root / "jobs.log"
        lines = _registry_rows(root, attempt)
        rows = [_metadata(line) for line in lines]
        matches = [(f, m) for item in rows if item and (f := item[0]) and (m := item[1])
                   if m.get("attempt_id") == attempt]
        if not matches:
            prefix_matches.extend((root,f,m) for item in rows if item and (f:=item[0]) and (m:=item[1])
                                  if str(m.get("attempt_id", "")).startswith(attempt))
        if matches:
            fields, meta = matches[-1]
            return root, fields, meta
    unique={meta.get("attempt_id"):(root,fields,meta) for root,fields,meta in prefix_matches}
    if len(unique)==1: return next(iter(unique.values()))
    return None


def _attempt_prefix_matches(attempt):
    found = {}
    for root in _dispatch_roots():
        rows = _registry_rows(root, attempt)
        for raw in rows:
            parsed = _metadata(raw)
            if parsed and parsed[1].get("attempt_id", "").startswith(attempt):
                fields, meta = parsed
                found[meta.get("attempt_id")] = (root, fields, meta)
    return [found[key] for key in sorted(found) if key]


def _owner_candidates(parent_sid=None):
    latest={}
    for root in _dispatch_roots():
        rows=_registry_rows(root, '"worker_type":"owner"')
        if not rows: rows=_registry_rows(root, "worker_type=owner")
        for raw in rows:
            parsed=_metadata(raw)
            if not parsed: continue
            fields,meta=parsed
            attempt=meta.get("attempt_id")
            if not attempt or meta.get("worker_type") != "owner": continue
            if parent_sid and meta.get("parent_sid") != parent_sid: continue
            latest[attempt]=(root,fields,meta)
    return sorted(latest.values(),key=lambda item:(item[1][1] in ("open","running"),item[1][0]),reverse=True)


def _log_path(root, fields, meta):
    raw = meta.get("log_file") or meta.get("log")
    attempt = meta.get("attempt_id")
    harness = meta.get("harness")
    if not raw: return None
    class Job: pass
    job = Job(); job._log_file = raw; job.attempt_id = attempt; job.harness = harness
    job._registry_path = str(root / "jobs.log"); job.artifact_root = meta.get("artifact_root")
    job._launch_home = meta.get("launch_home")
    return owned_attempt_log_path(job, fallback_home=os.environ.get("AGENT_HOME"))


def _announced(log, attempt):
    found = []
    cursor = Cursor(Path(log))
    for raw in cursor.read_new(final=True):
        try: row = json.loads(raw)
        except (ValueError, TypeError): continue
        if not isinstance(row, dict): continue
        typ = str(row.get("type", ""))
        if _supervisor_type(typ) in ("session", "session.announced"):
            if row.get("parent_attempt_id") not in (None, attempt): continue
            sid = row.get("session_id") or row.get("sessionId") or row.get("sid")
            event_cwd = row.get("cwd")
            if isinstance(row.get("data"), dict):
                sid = sid or row["data"].get("session_id")
                event_cwd = event_cwd or row["data"].get("cwd")
            if sid and sid not in [item[0] for item in found]: found.append((sid, event_cwd))
    return found


def resolve_sources(attempt: str) -> tuple[list[Source], str | None]:
    """Resolve validated attempt log and any announced native transcript source."""
    found = _find_attempt(attempt)
    if not found: return [], "attempt not found"
    root, fields, meta = found
    log = _log_path(root, fields, meta)
    if not log: return [], "attempt log is missing or outside its allowed directory"
    harness = meta.get("harness")
    sources = [Source(Path(log), harness, "log")]
    sid_list = _announced(log, attempt)
    runtime_home = meta.get("runtime_home") or meta.get("worker_home")
    opencode_runtime_dir = meta.get("opencode_runtime_dir") or meta.get("runtime_dir")
    cwd = meta.get("cwd") or fields[3]
    if runtime_home or opencode_runtime_dir:
        for sid, event_cwd in sid_list:
            if harness == "claude":
                from claude_project_dir import resolve_session_transcript
                transcript, _ = resolve_session_transcript(sid, event_cwd or cwd,
                                                           expected_cwd=cwd, runtime_home=runtime_home)
                if transcript: sources.append(Source(transcript, harness, "claude", sid))
            elif harness == "opencode":
                from opencode_runtime import opencode_db_path
                if not opencode_runtime_dir:
                    continue
                db = opencode_db_path({"OPENCODE_DATA_DIR": str(Path(opencode_runtime_dir) / "data" / "opencode")})
                if db.is_file(): sources.append(Source(db, harness, "opencode-db", sid))
    return sources, None


def _registry_sources_for_path(path: Path):
    target = path.resolve()
    matches = []
    for root in _dispatch_roots():
        rows = _registry_rows(root, path.name)
        for raw in rows:
            parsed = _metadata(raw)
            if not parsed: continue
            fields, meta = parsed
            admitted = _log_path(root, fields, meta)
            if admitted and Path(admitted).resolve() == target:
                matches.append((root, fields, meta))
    return matches


def _find_children(parent_attempt, *, force=False):
    children=[]
    for root in _dispatch_roots():
        rows=_registry_rows(root, parent_attempt, force=force)
        for raw in rows:
            parsed=_metadata(raw)
            if not parsed: continue
            fields, meta=parsed
            attempt=meta.get("attempt_id")
            if meta.get("parent_attempt_id") != parent_attempt or not attempt: continue
            path=_log_path(root, fields, meta)
            if path:
                node=meta.get("route_node") or fields[4] or "stage"
                children.append((attempt, Source(Path(path), meta.get("harness"), "log", None, node)))
        if children: break
    return children


def normalize_opencode_db_row(row, generation):
    """Map one native DB row to the OpenCode run-json event envelope."""
    data, role = row.get("data"), row.get("role")
    if not isinstance(data, dict):
        return [{"type": "unknown", "part": data if data is not None else row.get("raw")}]

    def event(kind, part):
        return {"type": kind, "part": part}

    def tool_event(part):
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        output = state.get("output") or state.get("error")
        if isinstance(output, dict): output = output.get("message") or output.get("text") or _compact(output)
        if isinstance(output, list):
            output = "\n".join(str(block.get("text", "")) for block in output
                                if isinstance(block, dict) and block.get("type") == "text")
        if output is None and isinstance(state.get("content"), list):
            output = "\n".join(str(block.get("text", "")) for block in state["content"]
                                if isinstance(block, dict) and block.get("type") == "text")
        return event("tool_use", {"tool": part.get("tool") or part.get("name"),
                                   "callID": part.get("callID") or part.get("id"),
                                   "state": {"status": state.get("status"),
                                             "input": state.get("input", part.get("input")),
                                             "output": output,
                                             "error": state.get("error") if output is None else None}})

    if generation == "v1":
        kind = data.get("type")
        if kind == "tool": return [tool_event(data)]
        if kind == "text": return [event("user" if role == "user" else "text", {"text": data.get("text")})]
        if kind == "reasoning": return [event("reasoning", {"text": data.get("text") or data.get("reason")})]
        if kind in ("step-start", "step_start"): return [event("step_start", data)]
        if kind in ("step-finish", "step_finish"): return [event("step_finish", data)]
        return [event("unknown", {"part": data})]
    if role == "user": return [event("user", {"text": data.get("text") or ""})]
    if role != "assistant" or not isinstance(data.get("content"), list):
        return [event("unknown", {"part": data})]
    events = []
    for part in data["content"]:
        if not isinstance(part, dict):
            events.append(event("unknown", {"part": part})); continue
        kind = part.get("type")
        if kind == "tool": events.append(tool_event(part))
        elif kind in ("text", "reasoning"):
            events.append(event(kind, {"text": part.get("text")}))
        elif kind in ("step-start", "step_start"): events.append(event("step_start", part))
        elif kind in ("step-finish", "step_finish"): events.append(event("step_finish", part))
        else: events.append(event("unknown", {"part": part}))
    return events


def _sqlite_records(path: Path, sid: str):
    from opencode_runtime import read_session_parts
    snapshot = read_session_parts(sid, db_path=path)
    if snapshot.get("state") != "observed":
        yield {"type": "notice", "text": snapshot.get("reason", "database unavailable")}
        return
    for row in snapshot["parts"]:
        yield from normalize_opencode_db_row(row, snapshot["generation"])


def read_new(source: Source, cursor: Cursor) -> tuple[list[object], Cursor]:
    if source.kind == "opencode-db":
        try:
            return _follow_db(source, cursor.reader_state), cursor
        except Exception as exc:
            return [Line("notice", sanitize(f"source unavailable: {exc}"))], cursor
    return cursor.read_new(), cursor


def _follow_db(source, state):
    from opencode_runtime import read_session_parts
    snapshot = read_session_parts(source.session_id or "", db_path=source.path,
                                  after_cursor=state["cursor"], refresh_cursors=state["refresh"])
    if snapshot.get("state") != "observed":
        return [Line("notice", "OpenCode DB: " + str(snapshot.get("reason", "unavailable")))]
    changed = []
    entities = state.setdefault("entities", {})
    pending = set()
    for row in snapshot["parts"]:
        key = row["cursor"]
        if snapshot["generation"] == "v2" and row.get("role") == "assistant":
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
            parts = data.get("content", []) if isinstance(data.get("content"), list) else []
            if not time_data.get("completed") or any(
                    isinstance(part, dict) and part.get("type") == "tool"
                    and isinstance(part.get("state"), dict)
                    and part["state"].get("status") in ("running", "streaming") for part in parts):
                pending.add(key)
        signature = _compact(row)
        if state["rows"].get(key) == signature: continue
        state["rows"][key] = signature
        for index, record in enumerate(normalize_opencode_db_row(row, snapshot["generation"])):
            entity_key = (key, index)
            part = record.get("part") if isinstance(record.get("part"), dict) else {}
            if snapshot["generation"] == "v2" and record.get("type") == "tool_use":
                previous = entities.get(entity_key)
                entities[entity_key] = record
                if previous:
                    old_state = previous.get("part", {}).get("state", {})
                    new_state = part.get("state", {})
                    old_output, new_output = old_state.get("output") or "", new_state.get("output") or ""
                    if old_state.get("status") != new_state.get("status"):
                        changed.append(Line("result", sanitize(f"tool {part.get('tool')} {new_state.get('status')}")))
                    delta = new_output[len(old_output):] if isinstance(new_output, str) and new_output.startswith(old_output) else new_output
                    if delta:
                        changed.extend(format_record("opencode", {"type": "tool_use", "part": {
                            "tool": part.get("tool"), "state": {"output": delta}}}))
                    continue
            elif snapshot["generation"] == "v2" and record.get("type") in ("text", "reasoning"):
                value = part.get("text") or ""
                previous = entities.get(entity_key, "")
                entities[entity_key] = value
                delta = value[len(previous):] if isinstance(value, str) and value.startswith(previous) else value
                if not delta: continue
                record = {"type": record["type"], "part": {"text": delta}}
            changed.extend(format_record("opencode", record))
    state["cursor"] = max(state["cursor"], snapshot.get("cursor", state["cursor"]))
    if snapshot["generation"] == "v1" and state["cursor"]:
        pending.add(state["cursor"])
    state["refresh"] = sorted(pending)
    return changed


def _present(lines, full=False):
    for line in lines:
        text=line.text
        if line.kind == "unknown" and not full and len(text) > 300:
            omitted = len(text) - 300
            text = text[:300] + f"… (+{omitted}자 생략, --full)"
        chunks=text.splitlines() or [text]
        if line.kind in ("prompt", "output") and not full and len(chunks)>24:
            omitted=len(chunks)-24
            chunks=chunks[:12]+[f"… (+{omitted}줄 생략, --full)"]+chunks[-12:]
        if line.kind == "prompt" and not full and len(chunks)==1 and len(chunks[0])>2000:
            omitted=len(chunks[0])-2000
            chunks=[chunks[0][:1000]+f"… (+{omitted}자 생략, --full)…"+chunks[0][-1000:]]
        for chunk in chunks: print(sanitize(chunk))


def _format_present(harness, raw_rows, state, full=False, prefix=""):
    """Stateful Codex event presentation, preserving live command starts."""
    lines = []
    state.setdefault("started", set())
    state.setdefault("messages", set())
    for raw in raw_rows:
        if isinstance(raw, Line):
            lines.append(raw); continue
        try:
            record = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            lines.extend(format_record(harness, raw)); continue
        if harness == "codex" and isinstance(record, dict):
            event = str(record.get("type", ""))
            item = record.get("item") if isinstance(record.get("item"), dict) else record
            ident = item.get("id") or record.get("id")
            item_type = str(item.get("type", ""))
            if item_type in ("command_execution", "commandExecution"):
                if event.endswith("started"):
                    if ident is not None: state["started"].add(ident)
                    lines.extend(format_record(harness, item)); continue
                if event.endswith("completed"):
                    subset = {k: v for k, v in item.items()
                              if k in ("aggregated_output", "aggregatedOutput", "output", "exit_code", "exitCode")}
                    if subset: lines.extend(format_record(harness, {"type": item_type, **subset}))
                    elif ident not in state["started"]: lines.extend(format_record(harness, item))
                    continue
            if item_type in ("agent_message", "user_message", "userMessage"):
                if ident is not None and ident in state["messages"]: continue
                if ident is not None: state["messages"].add(ident)
                lines.extend(format_record(harness, item)); continue
        if harness == "claude" and full and isinstance(record, dict) and record.get("type") == "attachment":
            attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else record
            payload = {key: value for key, value in attachment.items() if key != "type"}
            lines.extend(format_record(harness, record))
            if payload:
                lines.append(Line("output", sanitize("attachment payload: " + _compact(payload))))
            continue
        lines.extend(format_record(harness, record))
    _present([Line(line.kind, prefix + line.text) for line in lines], full)


def _has_session_envelope(rows):
    for raw in rows:
        try:
            record = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and _supervisor_type(str(record.get("type", ""))) in (
                "session", "session.announced"):
            return True
    return False


def main(argv=None, *, clock=None, sleep=None) -> int:
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    parser=argparse.ArgumentParser(prog="attempt-view", description="Show an attempt's recorded output.")
    parser.add_argument("attempt", nargs="?")
    parser.add_argument("--full", action="store_true", help="show the full initial prompt and tool output")
    parser.add_argument("--follow", action="store_true", help="follow new output until the owner finishes")
    parser.add_argument("--session", help="select an owner by its exact parent session")
    parser.add_argument("--poll-interval", type=float, default=.5, help=argparse.SUPPRESS)
    parser.add_argument("--grace", type=float, default=2.0, help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    log_only = False
    if args.attempt and os.path.isfile(args.attempt):
        if Path(args.attempt).is_symlink():
            print("Log path symlink does not match its registry-bound source.", file=sys.stderr); return 2
        path = Path(args.attempt).resolve()
        matches = _registry_sources_for_path(path)
        if len(matches) == 1:
            root, fields, meta = matches[0]
            args.attempt = meta.get("attempt_id")
            selected_path = path
        else:
            bind = re.search(r"\.att-([A-Za-z0-9_-]+)(?:\.|$)", path.name)
            if not bind:
                print("Log path must contain a registry-bound .att-<attempt> filename.", file=sys.stderr); return 2
            bound = bind.group(1)
            attempts = _attempt_prefix_matches(bound)
            if bound and not bound.startswith("att-"):
                attempts.extend(_attempt_prefix_matches("att-" + bound))
            attempts = list({item[2].get("attempt_id"): item for item in attempts}.values())
            exact = [item for item in attempts if item[2].get("attempt_id") in (bound, "att-" + bound)]
            if len(exact) == 1:
                expected = _log_path(*exact[0])
                if not expected or Path(expected).resolve() != path:
                    print("Log path does not match its registered attempt row.", file=sys.stderr); return 2
                args.attempt = bind.group(1); selected_path = path
            elif len(attempts) > 1:
                print("Ambiguous attempt prefix; candidates:", file=sys.stderr)
                for _root, _fields, meta in attempts:
                    print("  " + sanitize(meta.get("attempt_id", "")), file=sys.stderr)
                return 2
            else:
                print("No registry row for log path; showing log only.")
                harness = next((h for h in ("claude", "codex", "opencode") if path.name.endswith("."+h+".jsonl")), "unknown")
                log_source = Source(path, harness)
                log_only = True
    if not args.attempt:
        candidates=_owner_candidates(args.session)
        if args.session and len(candidates) == 1:
            args.attempt = candidates[0][2].get("attempt_id")
        else:
            if not candidates: print("No owner attempts found.")
            for _root,fields,meta in candidates:
                print(sanitize(f"{meta.get('attempt_id','')}  {meta.get('harness','unknown')}  {fields[1]}  {fields[4]}"))
            return 0
    if not log_only and not os.path.isfile(args.attempt):
        matches = _attempt_prefix_matches(args.attempt)
        exact = [item for item in matches if item[2].get("attempt_id") == args.attempt]
        if len(exact) > 1:
            matches = exact
        elif len(matches) > 1:
            print("Ambiguous attempt prefix; candidates:", file=sys.stderr)
            for _root, _fields, meta in matches: print("  " + sanitize(meta.get("attempt_id", "")), file=sys.stderr)
            return 2
        elif not matches:
            print(f"Unknown attempt: {sanitize(args.attempt)}", file=sys.stderr); return 2
        elif len(matches) == 1:
            args.attempt = matches[0][2].get("attempt_id")
    sources, error = ([log_source], None) if log_only else resolve_sources(args.attempt)
    if error:
        print(sanitize(error), file=sys.stderr); return 2
    cursors = {}
    owner_log_rows = []
    native_source_shown = False
    with ExitStack() as stack:
        for source in sources:
            if source.kind != "log":
                try:
                    from worker_home_retention import worker_home_read_admission
                    jobs = _find_attempt(args.attempt)
                    if jobs:
                        root, _fields, meta = jobs
                        runtime_home = meta.get("runtime_home") or meta.get("worker_home")
                        if runtime_home:
                            admitted = stack.enter_context(worker_home_read_admission(
                                root / "jobs.log", runtime_home, source.path,
                                args.attempt, source.harness))
                            if not admitted:
                                _present([Line("notice", "native source is no longer available")], args.full)
                                continue
                except Exception:
                    _present([Line("notice", "native source could not be admitted")], args.full)
                    continue
            if source.kind == "claude":
                native_source_shown = True
            cursors[source] = Cursor(source.path)
            if source.kind == "opencode-db":
                fresh, cursors[source] = read_new(source, cursors[source])
                native_source_shown = not any(
                    isinstance(line, Line) and line.kind == "notice" and
                    (line.text.startswith("OpenCode DB:") or line.text.startswith("source unavailable:"))
                    for line in fresh)
                _present(fresh, args.full)
            else:
                initial_rows, cursors[source] = read_new(source, cursors[source])
                if source == sources[0]: owner_log_rows = initial_rows
                if "present_state" not in locals(): present_state = {"started": set(), "messages": set()}
                _format_present(source.harness, initial_rows, present_state, args.full)
        if sources and sources[0].harness in ("claude", "opencode") and not native_source_shown \
                and _has_session_envelope(owner_log_rows):
            print("감독 기록만 남음")
        if args.follow and not log_only:
            seen_children=set()
            terminal_seen=False
            closed_since=None
            present_state = locals().get("present_state", {"started": set(), "messages": set()})
            while True:
                owner_cursor = cursors.get(sources[0])
                owner_rows = []
                if owner_cursor:
                    owner_rows, owner_cursor = read_new(sources[0], owner_cursor)
                    cursors[sources[0]] = owner_cursor
                for raw in owner_rows:
                    try: kind=json.loads(raw).get("type")
                    except Exception: kind=None
                    terminal_seen = terminal_seen or kind == "result" or _supervisor_type(str(kind or "")) == "terminal"
                for child_id, child in _find_children(args.attempt):
                    if child_id in seen_children: continue
                    seen_children.add(child_id); cursors[child]=Cursor(child.path)
                    print(f"[{sanitize(child.label)}] ── 시작 ({sanitize(child.harness)}) ──")
                for source, cursor in list(cursors.items()):
                    prefix = f"[{source.label}] " if source.label else ""
                    if source.kind == "opencode-db":
                        fresh, cursor = read_new(source, cursor)
                        cursors[source] = cursor
                        _present([Line(line.kind, prefix + line.text) for line in fresh], args.full)
                        continue
                    if source == sources[0]:
                        fresh = owner_rows
                    else:
                        fresh, cursor = read_new(source, cursor)
                        cursors[source] = cursor
                    for notice in cursor.notices:
                        fresh.insert(0, Line("notice", notice))
                    cursor.notices.clear()
                    _format_present(source.harness, fresh, present_state, args.full, prefix=prefix)
                current=_find_attempt(args.attempt)
                status=current[1][1] if current else "unknown"
                if status not in ("open", "running"):
                    if closed_since is None: closed_since=clock()
                    if terminal_seen or clock()-closed_since >= args.grace:
                        # Refresh once before the final drain: a child can register
                        # while the owner status is being read, inside the registry
                        # reader's normal one-second throttle window.
                        for child_id, child in _find_children(args.attempt, force=True):
                            if child_id in seen_children: continue
                            seen_children.add(child_id); cursors[child]=Cursor(child.path)
                            print(f"[{sanitize(child.label)}] ── 시작 ({sanitize(child.harness)}) ──")
                        # Drain every source after closure so the last write is visible.
                        for source, cursor in list(cursors.items()):
                            if source.kind == "opencode-db":
                                drain, cursor = read_new(source, cursor)
                                cursors[source] = cursor
                                _present(drain, args.full)
                            else:
                                drain, cursor = read_new(source, cursor)
                                cursors[source] = cursor
                                for raw in drain:
                                    try: kind=json.loads(raw).get("type")
                                    except Exception: kind=None
                                    terminal_seen = terminal_seen or kind == "result" or _supervisor_type(str(kind or "")) == "terminal"
                                _format_present(source.harness, drain, present_state, args.full,
                                                prefix=f"[{source.label}] " if source.label else "")
                        failure=current[2].get("failure_class", "없음") if current else "없음"
                        ending=f"[끝] {status} failure_class={failure}"
                        if not terminal_seen: ending += " 종료 기록 없음"
                        print(sanitize(ending)); break
                sleep(args.poll_interval)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        raise SystemExit(0)
