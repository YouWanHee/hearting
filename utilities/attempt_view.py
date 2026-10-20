"""Fleet-independent, read-only owner attempt transcript viewer.

The ANSI and credential sanitizer is adapted from OpenRig v0.6.13
(commit 9eeb2e42c, packages/tui/src/render.ts:1645-1646, 1671-1675),
Apache-2.0; see OpenRig-Apache-2.0.txt. Modified for Python, full output,
OSC/DCS/C1 control sequences, and terminal-safe handling of native records.
"""
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


_URL_AUTH = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.I)
_BEARER = re.compile(r"\bBearer\s+[^\s]+", re.I)


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
    clean = _URL_AUTH.sub(r"\1[redacted]@", clean)
    return _BEARER.sub("Bearer [redacted]", clean)


def _compact(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


_HIDDEN_TYPES = {"token_usage", "thinking_tokens", "tool_progress", "api-request-blob",
                 "cost-state", "queue-operation"}


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
    if kind in _HIDDEN_TYPES:
        return []
    result: list[Line] = []
    def add(label, value, category):
        if value is not None and value != "":
            if isinstance(value, (dict, list)):
                value = _compact(value)
            result.append(Line(category, sanitize(f"{label}: {value}")))
    if kind == "notice":
        add("notice", record.get("text") or record.get("reason"), "notice")
    elif harness == "claude":
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if kind in ("assistant", "user") or record.get("message"):
            content = message.get("content")
            if isinstance(content, str):
                add("assistant" if kind == "assistant" else "user", content, "message")
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        add("content", part, "raw"); continue
                    ptype = part.get("type")
                    if ptype == "text": add("assistant" if kind == "assistant" else "user", part.get("text"), "message" if kind == "assistant" else "prompt")
                    elif ptype == "thinking": add("thinking", part.get("thinking"), "thinking")
                    elif ptype == "tool_use":
                        add("tool " + str(part.get("name", "")), part.get("input"), "tool")
                    elif ptype == "tool_result": add("tool output", part.get("content"), "output")
                    else: add(str(ptype or "content"), part, "unknown")
        elif kind == "result": add("result", record, "result")
    elif harness == "codex":
        item = record.get("item") if isinstance(record.get("item"), dict) else record
        itype = str(item.get("type") or kind)
        if itype in ("agent_message", "user_message", "userMessage"):
            add("assistant" if "agent" in itype else "user", item.get("text") or item.get("message"), "message" if "agent" in itype else "prompt")
        elif itype in ("command_execution", "commandExecution"):
            add("command", item.get("command") or item.get("cmd"), "tool")
            add("output", item.get("aggregated_output") or item.get("aggregatedOutput") or item.get("output"), "output")
            add("exit", item.get("exit_code") or item.get("exitCode"), "result")
        elif itype in ("reasoning", "file_change", "fileChange"):
            add(itype, item.get("text") or item.get("id") or "본문 없음", "unknown")
        elif "text" in item:
            add(itype, item.get("text"), "message")
    elif harness == "opencode":
        if kind in ("text", "message", "assistant", "user"): add(kind, record.get("text") or record.get("content"), "prompt" if kind == "user" else "message")
        elif kind in ("tool_use", "tool"):
            add("tool " + str(record.get("name") or record.get("tool") or ""), record.get("input"), "tool")
            add("output", record.get("output") or record.get("state"), "output")
        elif kind in ("reasoning", "step-start", "step-finish", "result"):
            add(kind, record.get("text") or record, "thinking" if kind == "reasoning" else kind)
    if not result and kind not in ("system", "init", "heartbeat"):
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
        roots.append(os.path.join(registry_dir, "logs"))
        if os.path.basename(registry_dir) == "dispatch":
            roots.append(os.path.join(os.path.dirname(registry_dir), ".dispatch", "logs"))
    artifact_root = getattr(job, "artifact_root", None)
    if artifact_root:
        ar = os.path.realpath(artifact_root)
        roots.extend((ar, os.path.join(os.path.dirname(ar), ".dispatch", "logs")))
    launch_home = getattr(job, "_launch_home", None)
    if launch_home:
        roots.append(os.path.join(os.path.realpath(launch_home), ".dispatch", "logs"))
    if not roots and fallback_home:
        roots.append(os.path.realpath(os.path.join(fallback_home, ".dispatch", "logs")))
    if not any(_within(root, path) for root in roots):
        return None
    base = os.path.basename(path)
    if not base.endswith(f".{harness}.jsonl") or f".{attempt_id}." not in base:
        return None
    return path if os.path.isfile(path) else None


def _within(root, path):
    try:
        return os.path.commonpath((os.path.realpath(root), path)) == os.path.realpath(root)
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
        try: lines = jobs.read_text(errors="replace").splitlines()
        except OSError: continue
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
        try: rows = (root / "jobs.log").read_text(errors="replace").splitlines()
        except OSError: continue
        for raw in rows:
            parsed = _metadata(raw)
            if parsed and parsed[1].get("attempt_id", "").startswith(attempt):
                fields, meta = parsed
                found[meta.get("attempt_id")] = (root, fields, meta)
    return [found[key] for key in sorted(found) if key]


def _owner_candidates(parent_sid=None):
    latest={}
    for root in _dispatch_roots():
        try: rows=(root / "jobs.log").read_text(errors="replace").splitlines()
        except OSError: continue
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
    harness = meta.get("harness") or fields[2]
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
        if "supervisor.session" in typ or typ in ("dispatch.supervisor.session", "session"):
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
    harness = meta.get("harness") or fields[2]
    sources = [Source(Path(log), harness, "log")]
    sid_list = _announced(log, attempt)
    runtime_home = meta.get("runtime_home") or meta.get("worker_home")
    cwd = meta.get("cwd") or fields[3]
    if runtime_home:
        for sid, event_cwd in sid_list:
            if harness == "claude":
                from claude_project_dir import resolve_session_transcript
                transcript, _ = resolve_session_transcript(sid, event_cwd or cwd,
                                                           expected_cwd=cwd, runtime_home=runtime_home)
                if transcript: sources.append(Source(transcript, harness, "claude", sid))
            elif harness == "opencode":
                db = Path(runtime_home) / "runtime" / "data" / "opencode" / "opencode.db"
                if not db.is_file(): db = Path(runtime_home) / "data" / "opencode" / "opencode.db"
                if db.is_file(): sources.append(Source(db, harness, "opencode-db", sid))
    return sources, None


def _registry_sources_for_path(path: Path):
    target = path.resolve()
    matches = []
    for root in _dispatch_roots():
        try: rows = (root / "jobs.log").read_text(errors="replace").splitlines()
        except OSError: continue
        for raw in rows:
            parsed = _metadata(raw)
            if not parsed: continue
            fields, meta = parsed
            admitted = _log_path(root, fields, meta)
            if admitted and Path(admitted).resolve() == target:
                matches.append((root, fields, meta))
    return matches


def _find_children(parent_attempt):
    children=[]
    for root in _dispatch_roots():
        try: rows=(root / "jobs.log").read_text(errors="replace").splitlines()
        except OSError: continue
        for raw in rows:
            parsed=_metadata(raw)
            if not parsed: continue
            fields, meta=parsed
            attempt=meta.get("attempt_id")
            if meta.get("parent_attempt_id") != parent_attempt or not attempt: continue
            path=_log_path(root, fields, meta)
            if path:
                node=meta.get("route_node") or fields[4] or "stage"
                children.append((attempt, Source(Path(path), meta.get("harness") or fields[2], "log", None, node)))
        if children: break
    return children


def _sqlite_records(path: Path, sid: str):
    from opencode_runtime import read_session_parts
    snapshot = read_session_parts(sid, db_path=path)
    if snapshot.get("state") != "observed":
        yield {"type": "notice", "text": snapshot.get("reason", "database unavailable")}
        return
    generation = snapshot["generation"]
    for row in snapshot["parts"]:
        data, role = row.get("data"), row.get("role")
        if data is None:
            yield {"type": "unknown", "raw": row.get("raw")}
            continue
        if generation == "v2":
            if role == "user":
                yield {"type": "user", "text": data.get("text", "") if isinstance(data, dict) else data}
            elif role == "assistant" and isinstance(data, dict):
                for part in data.get("content", []):
                    if not isinstance(part, dict):
                        yield {"type": "unknown", "part": part}; continue
                    kind = part.get("type")
                    if kind in ("text", "reasoning"):
                        yield {"type": kind, "text": part.get("text")}
                    elif kind == "tool":
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        blocks = state.get("content")
                        output = "\n".join(str(block.get("text", "")) for block in blocks
                                            if isinstance(block, dict) and block.get("type") == "text") \
                            if isinstance(blocks, list) else state.get("output") or state.get("error")
                        yield {"type": "tool", "name": part.get("name"),
                               "input": state.get("input"), "output": output,
                               "status": state.get("status")}
                    else:
                        yield {"type": "unknown", "part": part}
        elif isinstance(data, dict):
            kind = data.get("type")
            if kind == "text": yield {"type": role or "text", "text": data.get("text")}
            elif kind == "tool":
                state = data.get("state") if isinstance(data.get("state"), dict) else {}
                yield {"type": "tool", "name": data.get("tool") or data.get("name"),
                       "input": state.get("input"), "output": state.get("output") or state.get("content"),
                       "status": state.get("status")}
            elif kind == "reasoning": yield {"type": "reasoning", "text": data.get("text") or data.get("reason")}
            elif kind in ("step-start", "step-finish"): yield data
            else: yield {"type": "unknown", "part": data}
        else:
            yield {"type": "unknown", "part": data}


def read_new(source: Source, *, final=True) -> list[Line]:
    if source.kind == "opencode-db":
        lines=[]
        try:
            for record in _sqlite_records(source.path, source.session_id or ""):
                lines.extend(format_record("opencode", record))
        except Exception as exc: return [Line("notice", sanitize(f"source unavailable: {exc}"))]
        return lines
    cursor=Cursor(source.path)
    output=[]
    for raw in cursor.read_new(final=final):
        for line in format_record(source.harness, raw): output.append(line)
    return output


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
        if snapshot["generation"] == "v2" and row.get("role") == "assistant":
            data = row.get("data")
            parts = data.get("content", []) if isinstance(data, dict) else []
            for index, part in enumerate(parts):
                if not isinstance(part, dict): continue
                entity_key = (key, index)
                typ = part.get("type")
                if typ == "tool":
                    toolstate = part.get("state") if isinstance(part.get("state"), dict) else {}
                    blocks = toolstate.get("content")
                    output = "\n".join(str(block.get("text", "")) for block in blocks
                                        if isinstance(block, dict) and block.get("type") == "text") \
                        if isinstance(blocks, list) else toolstate.get("output") or toolstate.get("error")
                    previous = entities.get(entity_key)
                    current = {"type": "tool", "name": part.get("name"),
                               "input": toolstate.get("input"), "output": output,
                               "status": toolstate.get("status")}
                    entities[entity_key] = current
                    record = current
                    if previous:
                        if previous.get("status") != current.get("status"):
                            changed.append(Line("result", sanitize(f"tool {current['name']} status: {current['status']}")))
                        old_output = previous.get("output") or ""
                        new_output = current.get("output") or ""
                        delta = new_output[len(old_output):] if isinstance(new_output, str) and new_output.startswith(old_output) else new_output
                        if delta:
                            changed.extend(format_record("opencode", {"type":"tool", "name":current["name"], "output":delta}))
                        continue
                elif typ in ("text", "reasoning"):
                    value = part.get("text") or ""
                    previous = entities.get(entity_key, "")
                    entities[entity_key] = value
                    delta = value[len(previous):] if isinstance(value, str) and value.startswith(previous) else value
                    if not delta: continue
                    record = {"type": typ, "text": delta}
                else: continue
                changed.extend(format_record("opencode", record))
        else:
            changed.extend(format_record("opencode", row.get("data") or {"type":"unknown", "raw":row.get("raw")}))
    state["cursor"] = max(state["cursor"], snapshot.get("cursor", state["cursor"]))
    if snapshot["generation"] == "v1" and state["cursor"]:
        pending.add(state["cursor"])
    state["refresh"] = sorted(pending)
    return changed


def _present(lines, full=False):
    for line in lines:
        text=line.text
        chunks=text.splitlines() or [text]
        if line.kind in ("prompt", "output") and not full and len(chunks)>24:
            omitted=len(chunks)-24
            chunks=chunks[:12]+[f"… (+{omitted}줄 생략, --full)"]+chunks[-12:]
        if line.kind == "prompt" and not full and len(chunks)==1 and len(chunks[0])>2000:
            omitted=len(chunks[0])-2000
            chunks=[chunks[0][:1000]+f"… (+{omitted}자 생략, --full)…"+chunks[0][-1000:]]
        for chunk in chunks: print(sanitize(chunk))


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
            attempts = _attempt_prefix_matches(bind.group(1))
            exact = [item for item in attempts if item[2].get("attempt_id") == bind.group(1)]
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
                print(sanitize(f"{meta.get('attempt_id','')}  {meta.get('harness',fields[2])}  {fields[1]}  {fields[4]}"))
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
    db_states = {source: {"cursor": 0, "refresh": [], "rows": {}}
                 for source in sources if source.kind == "opencode-db"}
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
            cursors[source] = Cursor(source.path)
            if source.kind == "opencode-db":
                _present(_follow_db(source, db_states[source]), args.full)
            else:
                initial_rows = cursors[source].read_new(final=True)
                if source == sources[0]: owner_log_rows = initial_rows
                _present([line for raw in initial_rows
                          for line in format_record(source.harness, raw)], args.full)
        if len(sources) == 1 and any("supervisor.session" in raw for raw in owner_log_rows):
            print("감독 기록만 남음")
        if args.follow and not log_only:
            seen_children=set()
            terminal_seen=False
            closed_since=None
            while True:
                owner_cursor = cursors.get(sources[0])
                for raw in owner_cursor.read_new() if owner_cursor else []:
                    try: kind=json.loads(raw).get("type")
                    except Exception: kind=None
                    terminal_seen = terminal_seen or kind in ("result", "dispatch.supervisor.terminal", "turn.completed")
                for child_id, child in _find_children(args.attempt):
                    if child_id in seen_children: continue
                    seen_children.add(child_id); cursors[child]=Cursor(child.path)
                    print(f"[{sanitize(child.label)}] ── 시작 ({sanitize(child.harness)}) ──")
                for source, cursor in cursors.items():
                    prefix = f"[{source.label}] " if source.label else ""
                    fresh = _follow_db(source, db_states[source]) if source.kind == "opencode-db" else [
                        line for raw in cursor.read_new() for line in format_record(source.harness, raw)]
                    for notice in cursor.notices:
                        fresh.insert(0, Line("notice", notice))
                    cursor.notices.clear()
                    _present([Line(line.kind, prefix + line.text) for line in fresh], args.full)
                current=_find_attempt(args.attempt)
                status=current[1][1] if current else "unknown"
                if status not in ("open", "running"):
                    if closed_since is None: closed_since=clock()
                    if terminal_seen or clock()-closed_since >= args.grace:
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
