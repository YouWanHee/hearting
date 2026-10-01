#!/usr/bin/env python3
"""session-tidy runner: queue, detached supervisor, registered memory worker, closed apply.

``session_tidy.py enqueue`` calls :func:`enqueue_item`, which writes a queue entry
and starts this file as a detached process (own session, no stdio), then returns at
once.  The detached runner:

1. waits for the server-wide ``runner.lock`` (queueing behind a running tidy, never
   refusing; after 30 minutes it leaves its entry ``queued`` and exits — the next
   scheduled tidy picks the entry up),
2. for every unfinished entry, oldest first: builds ``input_v1.json``, starts one
   *registered* headless worker through the adapter's checked dispatch wrapper,
   waits for its process to end (checked exact-identity state) and then for its output
   file, validates ``actions.json``, runs ``mem tidy-apply``, advances the watermarks and leaves one
   result line for the seat,
3. on any failure leaves the card and the watermarks as they were and leaves one
   failure line.  When the applier wrote, or may have written, part of the batch (its
   undo journal has finished or unconfirmed steps, or cannot be read) the line says so
   and carries ``mem tidy-undo <batch>`` instead of claiming the memory is untouched.  There is no operator step: the next
   scheduled tidy simply sees the same unprocessed conversation again (source keys and
   the duplicate check keep the writes that already landed from being made twice).

State (all under ``session_tidy.state_root()``): ``queue/<id>.json`` (+ ``<id>.pid``),
``runs/<batch>/{input_v1.json,prompt.md,actions.json,runner.log,undo.json,result.json}``,
``runner.lock``.

Test hooks are honoured only inside a test root: ``HEARTING_TIDY_TEST_ROOT`` must
contain the state folder, and an injected executable must live under it.  They are
``HEARTING_TIDY_WORKER_CMD`` / ``HEARTING_TIDY_STATE_CMD`` / ``HEARTING_TIDY_PEER_STEWARD``
(replace the checked commands), ``HEARTING_TIDY_SLUG`` and the timing knobs in :data:`TUNABLES`.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import session_tidy as st  # noqa: E402
import tidy_decisions as td  # noqa: E402
import tidy_transcripts as tt  # noqa: E402

SCHEMA = 1
UNIT = "ops/session-tidy-memory"
CAPABILITY = "session-tidy"
MODEL_PROFILE = "balanced-deep"
MODEL_ROLE = "deep editor"

TUNABLES = {
    "LOCK_WAIT": 30 * 60.0,         # runner.lock wait cap; past it the entry stays queued
    "GOVERNOR_WAIT": 20 * 60.0,     # total retry window while the governor has no free seat
    "WORKER_WAIT": 25 * 60.0,       # wait for the worker's registry row to close
    "POLL": 5.0,
    "GOVERNOR_SLEEP_MIN": 5.0,
    "GOVERNOR_SLEEP_MAX": 120.0,
    "APPLY_TIMEOUT": 40 * 60.0,
}
SESSION_CHUNK_BYTES = 192 * 1024
TOTAL_CHUNK_BYTES = 512 * 1024
MAX_TARGETS = 6
CANDIDATE_MAX = 60
CANDIDATE_BODY_CHARS = 400
CARD_CONTEXT_CHARS = 3000
PENDING_DECISIONS_MAX = 20
DUPLICATE_SIGNAL_MAX = 5
ACTIONS_MAX_BYTES = 1024 * 1024
KEEP_FINISHED_DAYS = 14
KEEP_RUN_DAYS = 60
MAX_ITEMS_PER_RUN = 10

# The worker runs on the caller's own harness where that harness's checked wrapper
# accepts a route-free support tuple under a modeless capability; OpenCode's does not
# (its capability catalog has no mode line for one), so its seats use Claude's.
WORKER_HARNESS = {"claude": "claude", "codex": "codex", "opencode": "claude"}

UNFINISHED = ("queued", "waiting-lock", "assembling", "dispatching", "waiting-worker", "validating", "applying")
ENV_DROP_KEYS = ("AGENT_SESSION_ROLE", "FLEET_TITLE_REFRESH", "MEM_DISTILL", "CLAUDE_CODE_SESSION_ID",
                 "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")
ENV_DROP_PREFIXES = ("AGENT_DISPATCH_", "OPENCODE_DISPATCH", "AGENT_ROUTE_", "AGENT_ARTIFACT_", "HERDR_")


class RunnerFailure(Exception):
    """One failed step; the message is the reason shown in the failure line."""


# ---------------------------------------------------------------------------
# Test hooks (valid only inside a test root) and small helpers
# ---------------------------------------------------------------------------

def test_root() -> Optional[Path]:
    raw = os.environ.get("HEARTING_TIDY_TEST_ROOT")
    if not raw or not os.path.isabs(raw):
        return None
    try:
        root = Path(raw).resolve()
        state = st.state_root().resolve()
    except OSError:
        return None
    return root if (state == root or root in state.parents) else None


def tunable(name: str) -> float:
    default = TUNABLES[name]
    if test_root() is None:
        return default
    try:
        return float(os.environ["HEARTING_TIDY_" + name])
    except (KeyError, ValueError):
        return default


def injected_executable(name: str) -> Optional[Path]:
    value, root = os.environ.get(name), test_root()
    if not value or root is None:
        return None
    path = Path(value).resolve()
    return path if root in path.parents and path.is_file() else None


def clean_env(environ=None) -> dict:
    """The environment of a detached tidy: no worker marker, session id or pane of the caller."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k not in ENV_DROP_KEYS and not k.startswith(ENV_DROP_PREFIXES)}


def _now() -> float:
    return time.time()


def _queue_dir() -> Path:
    return st.state_root() / "queue"


def _runs_dir() -> Path:
    return st.state_root() / "runs"


def _item_path(qid: str) -> Path:
    return _queue_dir() / f"{qid}.json"


def new_id(now: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(_now() if now is None else now))
    return f"tidy-{stamp}-{secrets.token_hex(3)}"


def read_item(qid: str) -> Optional[dict]:
    data = st.read_json(_item_path(qid))
    return data if isinstance(data, dict) and data.get("id") == qid else None


def write_item(item: dict) -> None:
    st.atomic_write_json(_item_path(item["id"]), item)


def set_status(item: dict, status: str, **extra) -> None:
    item["status"] = status
    item["updated"] = _now()
    item.update(extra)
    write_item(item)


def list_items() -> list:
    try:
        names = sorted(_queue_dir().iterdir())
    except OSError:
        return []
    items = []
    for path in names:
        if path.suffix == ".json" and not path.name.startswith("."):
            data = st.read_json(path)
            if isinstance(data, dict) and data.get("id") and data.get("status"):
                items.append(data)
    return sorted(items, key=lambda i: (float(i.get("created", 0) or 0), i["id"]))


def _log(run_dir: Path, text: str) -> None:
    with contextlib.suppress(OSError, st.StateError):
        st.ensure_dir(run_dir)
        fd = os.open(run_dir / "runner.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{st.iso_utc(_now())} {text}\n")


# ---------------------------------------------------------------------------
# enqueue: write the entry, start the detached runner, return
# ---------------------------------------------------------------------------

def enqueue_item(seat: "st.Seat", harness: str, sid: str, cwd: str, transcript: str = "") -> dict:
    item = {"schema": SCHEMA, "id": new_id(), "created": _now(), "status": "queued", "updated": _now(),
            "seat": seat.as_dict(), "harness": harness, "sid": sid, "cwd": cwd, "transcript": transcript,
            "attempts": 0}
    write_item(item)
    start_detached(item["id"])
    return item


def start_detached(qid: str) -> int:
    """Start ``run`` in its own session so neither /clear, a closing pane nor a harness
    process-group cleanup of the caller can take it down."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", "--queue-id", qid],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, env=clean_env(), cwd=str(st.ensure_dir(st.state_root())))
    return proc.pid


# ---------------------------------------------------------------------------
# The server-wide lock
# ---------------------------------------------------------------------------

def acquire_runner_lock(wait: float) -> Optional[int]:
    path = st.state_root() / "runner.lock"
    st.ensure_dir(path.parent)
    st._reject_symlink(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.25)


# ---------------------------------------------------------------------------
# Input bundle
# ---------------------------------------------------------------------------

def _mem_cli(*args, cwd: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "tools" / "memory" / "mem.py"), *args], cwd=cwd,
                          env=clean_env(), capture_output=True, text=True, timeout=timeout)


_SNAP_LINE = re.compile(r"^\[([^\]\s]+)\](?: [^:]*)? :: (.*)$")


def parse_snapshot(text: str) -> tuple[list, list]:
    """(candidates, allowlist ids) from ``mem curate-snapshot`` output: durable + working only."""
    section, rows, ids = "", [], []
    for line in text.splitlines():
        if line.startswith("PROTECTED PENDING"):
            section = "pending"
        elif line.startswith("DURABLE"):
            section = "durable"
        elif line.startswith("WORKING"):
            section = "working"
        elif line.startswith("ORPHAN") or line.startswith("SIGNALS"):
            section = ""
        elif line.startswith("IDS:"):
            ids = line[4:].split()
        elif section in ("durable", "working"):
            match = _SNAP_LINE.match(line)
            if match:
                rows.append({"id": match.group(1), "tier": section, "headline": match.group(2).strip()})
    allowed = set(ids)
    return [r for r in rows if r["id"] in allowed], ids


def _excerpts(ids: list, cwd: str) -> dict:
    """First part of each record body, read without touching the records (best effort)."""
    out: dict = {}
    try:
        mem = td.load_mem()
        if not mem.DB.exists():
            return out
        con = mem.get_con()
        try:
            for rid in ids:
                row = mem._visible_record(con, rid)
                if row is not None:
                    _meta, body = mem._row_to_meta(row)
                    out[rid] = " ".join(str(body).split())[:CANDIDATE_BODY_CHARS]
        finally:
            con.close()
    except BaseException:  # noqa: BLE001 - excerpts are a nicety, never a reason to fail
        return out
    return out


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[0-9A-Za-z가-힣_]{3,}", text.lower())}


def existing_records(cwd: str, conversation: str) -> tuple[list, list]:
    """(related active records of this project, duplicate-group signals), both read only."""
    try:
        snap = _mem_cli("curate-snapshot", cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return [], []
    if snap.returncode != 0:
        return [], []
    rows, ids = parse_snapshot(snap.stdout)
    wanted = _tokens(conversation)
    scored = sorted(rows, key=lambda r: -len(wanted & _tokens(r["headline"])))[:CANDIDATE_MAX]
    bodies = _excerpts([r["id"] for r in scored], cwd)
    for row in scored:
        row["excerpt"] = bodies.get(row["id"], "")
    groups: list = []
    try:
        life = _mem_cli("lifecycle", cwd=cwd)
        allowed = set(ids)
        for line in life.stdout.splitlines():
            if "[dup-flag]" in line:
                try:
                    group = ast.literal_eval(line.split("[dup-flag]", 1)[1].split("  (", 1)[0].strip())
                except (ValueError, SyntaxError):
                    continue
                group = [g for g in group if isinstance(g, str) and g in allowed]
                if len(group) > 1:
                    groups.append(group)
    except (OSError, subprocess.SubprocessError):
        groups = []
    return scored, groups[:DUPLICATE_SIGNAL_MAX]


def _pending_decisions(project_key: str) -> list:
    out = []
    for _path, payload in td.list_pending():
        try:
            if st.project_key_for(payload.get("cwd") or "") != project_key:
                continue
        except BaseException:  # noqa: BLE001
            continue
        out.append({"source": payload.get("source"), "question": payload.get("question"),
                    "answers": payload.get("answers"), "note": payload.get("note", "")})
    return out[:PENDING_DECISIONS_MAX]


class Bundle:
    def __init__(self, path: Path, digest: str, cursors: list, empty: bool):
        self.path, self.digest, self.cursors, self.empty = path, digest, cursors, empty


def _target_sessions(item: dict, seat) -> list:
    caller = {"harness": item["harness"], "sid": item["sid"], "role": "caller",
              "transcript": item.get("transcript") or ""}
    if not caller["transcript"]:
        located = tt.locate_transcript(caller["harness"], caller["sid"])
        caller["transcript"] = str(located) if located else ""
    targets = [caller]
    try:
        recent = tt.select_recent_sessions(seat, cwd=item["cwd"])
    except BaseException:  # noqa: BLE001
        recent = []
    for row in recent:
        if (row["harness"], row["sid"]) != (caller["harness"], caller["sid"]):
            targets.append({"harness": row["harness"], "sid": row["sid"], "role": "recent",
                            "transcript": row.get("transcript") or ""})
    return targets[:MAX_TARGETS]


def assemble(item: dict, batch: str, run_dir: Path) -> Bundle:
    seat = st.Seat(**item["seat"])
    project_key = st.project_key_for(item["cwd"])
    sessions, cursors, choices, skipped = [], [], [], []
    remaining = TOTAL_CHUNK_BYTES
    for target in _target_sessions(item, seat):
        if remaining <= 0:
            break
        if not target["transcript"]:
            skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": "no-record"})
            continue
        chunk = tt.read_pending(target["harness"], target["sid"], target["transcript"],
                                limit_bytes=min(SESSION_CHUNK_BYTES, remaining))
        if chunk.error:
            skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": chunk.error})
            continue
        if chunk.cursor_to == chunk.cursor_from and not chunk.text and not chunk.choices:
            continue
        remaining -= len(chunk.text.encode("utf-8"))
        sessions.append({"harness": target["harness"], "sid": target["sid"], "role": target["role"],
                         "cursor_from": chunk.cursor_from, "cursor_to": chunk.cursor_to, "eof": chunk.eof,
                         "rows": chunk.rows, "skipped_oversize": chunk.skipped_oversize,
                         "blocked": chunk.blocked, "text": chunk.text})
        cursors.append({"harness": target["harness"], "sid": target["sid"], "cursor": chunk.cursor_to,
                        "source": target["transcript"]})
        for choice in chunk.choices:
            choices.append({**choice, "session": target["sid"]})
    pending = _pending_decisions(project_key)
    conversation = "\n".join(s["text"] for s in sessions)
    records, groups = existing_records(item["cwd"], conversation) if (sessions or pending or choices) else ([], [])
    card = st.read_latest_card(seat)
    doc = {"schema_version": 1, "batch_id": batch, "created": st.iso_utc(_now()), "cwd": item["cwd"],
           "project_key": project_key, "seat": {"kind": seat.kind, "key": seat.key},
           "caller": {"harness": item["harness"], "sid": item["sid"]},
           "card": (card or {}).get("body", "")[:CARD_CONTEXT_CHARS],
           "sessions": sessions, "skipped_sessions": skipped, "user_choices": choices,
           "pending_decisions": pending, "existing_records": records, "duplicate_groups": groups,
           "limits": {"max_new_records": 10, "max_duplicate_groups": DUPLICATE_SIGNAL_MAX}}
    path = run_dir / "input_v1.json"
    st.atomic_write_json(path, doc)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    empty = not (sessions or choices or pending)
    return Bundle(path, digest, cursors, empty)


# ---------------------------------------------------------------------------
# Registered worker
# ---------------------------------------------------------------------------

def write_prompt(run_dir: Path, batch: str, bundle: Bundle) -> Path:
    actions = run_dir / "actions.json"
    text = (
        "Session-tidy memory worker. Follow the unit instructions (ops/session-tidy-memory).\n\n"
        f"batch_id: {batch}\n"
        f"input (read only): {bundle.path}\n"
        f"input_digest: {bundle.digest}\n"
        f"output (write exactly this one file): {actions}\n\n"
        "Read the input, write actions.json (JSON only), then stop. Do not call any memory write command.\n\n"
        "Final message: exactly the three handoff lines with `artifact: -`, then `verdict: PASS` (or FAIL) and\n"
        "`blocker: none` (or one line). The output file above is a private handoff to the runner, not a\n"
        "durable artifact, so its path does not go in the artifact field.\n")
    path = run_dir / "prompt.md"
    st.atomic_write(path, text.encode("utf-8"))
    return path


def _git_worktree(cwd: str) -> str:
    for candidate in (cwd, str(ROOT)):
        try:
            done = subprocess.run(["git", "-C", candidate, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip():
            return candidate
    raise RunnerFailure("no git worktree to register the worker under")


def launch_command(item: dict, prompt: Path, slug: str) -> list:
    injected = injected_executable("HEARTING_TIDY_WORKER_CMD")
    harness = WORKER_HARNESS.get(item["harness"], "claude")
    if injected:
        base = [str(injected)]
    else:
        base = [sys.executable, str(ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")]
    return base + [
        "--start", "--worktree", _git_worktree(item["cwd"]) if not injected else item["cwd"],
        "--slug", slug, "--capability", CAPABILITY, "--capability-mode", "default",
        "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "support",
        "--unit", UNIT, "--owner", CAPABILITY, "--assigned-contract", "session-tidy-memory",
        "--model-profile", MODEL_PROFILE, "--model-role", MODEL_ROLE, "--prompt-file", str(prompt)]


def parse_receipt(text: str) -> dict:
    fields: dict = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", " ")):
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def dispatch_worker(item: dict, batch: str, run_dir: Path, bundle: Bundle) -> dict:
    prompt = write_prompt(run_dir, batch, bundle)
    slug = os.environ.get("HEARTING_TIDY_SLUG") if test_root() else None
    slug = re.sub(r"[^a-z0-9-]", "-", (slug or f"session-{batch}").lower())[:64]
    command = launch_command(item, prompt, slug)
    deadline = time.monotonic() + tunable("GOVERNOR_WAIT")
    attempt = 0
    while True:
        try:
            done = subprocess.run(command, env=clean_env(), cwd=str(run_dir), capture_output=True,
                                  text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerFailure(f"worker launch failed: {exc}") from exc
        receipt = parse_receipt(done.stdout)
        _log(run_dir, f"launch rc={done.returncode} reason={receipt.get('reason', '-')} "
                      f"registered={receipt.get('registered', '-')} started={receipt.get('started', '-')}")
        if (done.returncode == 0 and receipt.get("registered") == "1" and receipt.get("started") == "1"
                and receipt.get("child_spawned") == "1" and receipt.get("attempt_id", "-") != "-"):
            return receipt
        reason = receipt.get("reason") or (done.stderr.strip().splitlines() or ["unknown"])[-1]
        if reason == "model-worker-governor-denied" or receipt.get("retryable") == "1":
            if time.monotonic() >= deadline:
                raise RunnerFailure("no free worker seat within the wait limit (governor)")
            try:
                hint = float(receipt.get("retry_after_seconds", ""))
            except ValueError:
                hint = 15.0 * (2 ** min(attempt, 4))
            time.sleep(min(max(hint, tunable("GOVERNOR_SLEEP_MIN")), tunable("GOVERNOR_SLEEP_MAX"),
                           max(0.0, deadline - time.monotonic())))
            attempt += 1
            continue
        raise RunnerFailure(f"worker was not started: {reason}")


def attempt_state(receipt: dict) -> str:
    """The worker's process state from the checked exact-identity classifier
    (``dispatch-registry.py attempt-state``): ``working``, ``dead``, ``done`` or ``unknown``."""
    injected = injected_executable("HEARTING_TIDY_STATE_CMD")
    base = [str(injected)] if injected else [sys.executable, str(ROOT / "utilities" / "dispatch-registry.py")]
    command = base + ["--jobs", receipt["job_registry"], "--attempt", receipt["attempt_id"],
                      "--pid", receipt.get("child_pid", ""), "--pid-start", receipt.get("child_pid_start", ""),
                      "attempt-state"]
    try:
        done = subprocess.run(command, env=clean_env(), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for line in done.stdout.splitlines():
        if line.startswith("state="):
            return line.partition("=")[2].strip() or "unknown"
    return "unknown"


def wait_worker(receipt: dict, run_dir: Path) -> None:
    """Wait until the registered worker's process is gone (checked exact-identity state, polled
    with a limit), then require its output file.  The model's stdout is never read.

    A route-free support worker has no terminal commit, so its registry row is not closed by
    the checked join (``settle_finished_attempt`` answers ``not-route-bound``); the process
    state is the end signal and ``actions.json`` is the result.
    """
    if not (receipt.get("job_registry") and receipt.get("child_pid") and receipt.get("child_pid_start")):
        raise RunnerFailure("the launch receipt carries no exact worker identity")
    deadline = time.monotonic() + tunable("WORKER_WAIT")
    while True:
        state = attempt_state(receipt)
        if state in ("dead", "done"):
            break
        if time.monotonic() >= deadline:
            raise RunnerFailure("worker did not finish within the wait limit")
        time.sleep(tunable("POLL"))
    _log(run_dir, f"worker process state={state}")
    if not (run_dir / "actions.json").is_file():
        raise RunnerFailure("worker finished without writing actions.json")


# ---------------------------------------------------------------------------
# actions.json check, apply, finish
# ---------------------------------------------------------------------------

def validate_actions(run_dir: Path, batch: str, bundle: Bundle) -> None:
    path = run_dir / "actions.json"
    try:
        if path.stat().st_size > ACTIONS_MAX_BYTES:
            raise RunnerFailure("actions.json is too large")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RunnerFailure(f"actions.json is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("actions"), list):
        raise RunnerFailure("actions.json must be an object with an actions list")
    if any(not isinstance(a, dict) for a in doc["actions"]):
        raise RunnerFailure("every action must be an object")
    # The runner owns the identity of the batch; the worker's copies are not trusted.
    doc["schema_version"], doc["batch_id"], doc["input_digest"] = 1, batch, bundle.digest
    st.atomic_write_json(path, doc)


def apply_actions(item: dict, run_dir: Path, bundle: Bundle) -> str:
    try:
        done = _mem_cli("tidy-apply", str(run_dir / "actions.json"), "--input", str(bundle.path),
                        "--cwd", item["cwd"], cwd=item["cwd"], timeout=tunable("APPLY_TIMEOUT"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerFailure(f"apply {'stopped' if any(applied_writes(run_dir)) else 'did not run'}: {exc}") from exc
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if ln.startswith("[tidy]")), "")
    _log(run_dir, f"apply rc={done.returncode} {summary or (done.stderr.strip().splitlines() or [''])[-1]}")
    if done.returncode != 0:
        detail = summary or (done.stderr.strip().splitlines() or ["apply failed"])[-1]
        raise RunnerFailure(detail.removeprefix("[tidy]").strip())
    return summary or "[tidy] 정리를 마쳤습니다."


def advance_watermarks(bundle: Bundle) -> None:
    for entry in bundle.cursors:
        tt.write_watermark(entry["harness"], entry["sid"], entry["cursor"], source=entry["source"])


def notify(item: dict, text: str) -> None:
    with contextlib.suppress(BaseException):
        st.write_notice(st.Seat(**item["seat"]), text, author_harness=item["harness"], author_sid=item["sid"])


def applied_writes(run_dir: Path) -> tuple:
    """``(finished, unconfirmed)`` writes of this batch, read from its undo journal.

    The applier records each step before taking it, so no journal means nothing was
    written: ``(0, 0)``.  A step left as ``intent`` may have landed before the journal
    could say so, and an unreadable journal is ``(0, -1)`` -- both unknown, never
    "untouched"; ``mem tidy-undo`` checks such steps against the store itself.
    """
    path = run_dir / "undo.json"
    if not os.path.lexists(path):
        return 0, 0
    try:
        ops = st.read_json(path)["ops"]
        return (sum(1 for op in ops if op.get("state") == "done"),
                sum(1 for op in ops if op.get("state") == "intent"))
    except Exception:  # noqa: BLE001 - an unreadable journal is "unknown", not "nothing"
        return 0, -1


def failure_line(reason: str, batch: str = "", applied: tuple = (0, 0)) -> str:
    reason = " ".join(str(reason).split(" — 되돌리기")[0].split())[:80]
    done, unconfirmed = applied
    if batch and (done or unconfirmed):
        # Something may have landed: say so and keep the undo command; never claim memory is untouched.
        landed = ("반영 여부를 확인하지 못했습니다" if unconfirmed < 0
                  else f"{done}건 반영" + (f", {unconfirmed}건은 반영 여부 불명" if unconfirmed else ""))
        return (f"[정리] 기억 정리가 중간에 멈췄습니다({landed}). 나머지는 다음 정리 때 이어서 처리됩니다. "
                f"(사유: {reason}) — 되돌리기: mem tidy-undo {batch}")
    return f"[정리] 기억 정리를 끝내지 못했습니다. 카드와 기존 기억은 그대로이고 다음 정리 때 이어서 처리됩니다. (사유: {reason})"


def process_item(item: dict) -> None:
    attempts = int(item.get("attempts", 0))
    batch = item["id"] if attempts == 0 else f"{item['id']}-r{attempts}"
    item["attempts"] = attempts + 1
    run_dir = st.ensure_dir(_runs_dir() / batch)
    set_status(item, "assembling", batch=batch)
    bundle = None
    try:
        bundle = assemble(item, batch, run_dir)
        if bundle.empty:
            set_status(item, "notified", result="nothing-new")
            notify(item, "[정리] 새로 정리할 대화가 없습니다.")
            return
        set_status(item, "dispatching")
        receipt = dispatch_worker(item, batch, run_dir, bundle)
        set_status(item, "waiting-worker", attempt_id=receipt["attempt_id"])
        wait_worker(receipt, run_dir)
        set_status(item, "validating")
        validate_actions(run_dir, batch, bundle)
        set_status(item, "applying")
        line = apply_actions(item, run_dir, bundle)
        advance_watermarks(bundle)
        set_status(item, "notified", result="applied")
        notify(item, line)
    except RunnerFailure as exc:
        _log(run_dir, f"failed: {exc}")
        applied = applied_writes(run_dir)
        set_status(item, "failed", error=str(exc), applied=list(applied))
        notify(item, failure_line(str(exc), batch, applied))
    except BaseException as exc:  # noqa: BLE001 - nothing may leave an entry half-done
        _log(run_dir, f"failed: internal {type(exc).__name__}: {exc}")
        applied = applied_writes(run_dir)
        set_status(item, "failed", error=f"internal {type(exc).__name__}", applied=list(applied))
        notify(item, failure_line(f"internal {type(exc).__name__}", batch, applied))
    finally:
        for name in ("input_v1.json", "prompt.md"):
            with contextlib.suppress(OSError):
                os.unlink(run_dir / name)


def prune_finished() -> None:
    cutoff = _now() - KEEP_FINISHED_DAYS * 86400
    for item in list_items():
        if item["status"] in UNFINISHED or float(item.get("updated", _now()) or 0) >= cutoff:
            continue
        for suffix in (".json", ".pid"):
            with contextlib.suppress(OSError):
                os.unlink(_queue_dir() / f"{item['id']}{suffix}")
    run_cutoff = _now() - KEEP_RUN_DAYS * 86400
    with contextlib.suppress(OSError):
        for path in _runs_dir().iterdir():
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < run_cutoff:
                shutil.rmtree(path, ignore_errors=True)


def run(qid: str) -> int:
    st.atomic_write(_queue_dir() / f"{qid}.pid", f"{os.getpid()}\n".encode("ascii"))
    fd = acquire_runner_lock(tunable("LOCK_WAIT"))
    if fd is None:
        return 0        # the entry stays queued; the next tidy continues from the queue
    try:
        for _ in range(MAX_ITEMS_PER_RUN):
            todo = [i for i in list_items() if i["status"] in UNFINISHED]
            if not todo:
                break
            process_item(todo[0])
        prune_finished()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return 0


# ---------------------------------------------------------------------------
# handoff: deliver the card to a peer session through peer-steward only
# ---------------------------------------------------------------------------

def handoff(seat: "st.Seat", target: str) -> tuple[str, int]:
    """(one report line, exit code); the verdict is peer-steward's own typed answer."""
    card = st.read_latest_card(seat)
    if not card or not st.sanitize_body(card.get("body", "")):
        return "prompted=false reason=no-card", 1
    path = st.state_root() / "handoff" / f"{new_id()}.md"
    st.atomic_write(path, (card["body"].rstrip() + "\n").encode("utf-8"))
    injected = injected_executable("HEARTING_TIDY_PEER_STEWARD")
    command = ([str(injected)] if injected else [sys.executable, str(ROOT / "utilities" / "peer-steward.py")])
    try:
        done = subprocess.run([*command, "prompt", target, "--body-file", str(path)],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"prompted=failed reason=peer-steward-unavailable detail={type(exc).__name__}", 1
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    lines = [ln.strip() for ln in done.stdout.splitlines() if ln.strip()]
    verdict = next((ln for ln in lines if ln.startswith("prompted=")), lines[-1] if lines else "prompted=unknown")
    return f"{verdict} exit={done.returncode}", done.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="session_tidy_runner.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="process the queue (started detached by enqueue)")
    runner.add_argument("--queue-id", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.queue_id)
    except BaseException:  # noqa: BLE001 - a detached runner has nobody to tell
        return 0


if __name__ == "__main__":
    sys.exit(main())
