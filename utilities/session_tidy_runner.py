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
``runs/<batch>/{input_v1.json,actions.json,runner.log,undo.json,result.json}``,
``runner.lock``.

The worker's own files are the one exception: its sandbox allows the artifact root the
checked wrapper launches it with, not the state folder, so ``input_v1.json`` (a copy of the
exact bytes in ``runs/<batch>``), ``prompt.md`` and the one ``actions.json`` it writes live in
``<artifact root>/.runtime/session-tidy/<batch>/``.  The runner reads that file back (no symlink,
size-capped), validates it, copies the checked result into ``runs/<batch>`` for ``mem tidy-apply``,
and removes the conversation copy when the batch ends (the rest goes with the finished entry).

Reading is tail first (``tidy_transcripts.read_pending``): the newest unread range's last whole
rows are the worker's input; only a successful apply removes that range from the unread ones, so a
long record's older front stays pending and a short result line says "일부만 읽음(범위)".  A finished
batch also refreshes the seat card's "참고할 기억" list (``session_tidy.update_memory_refs``).

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
import stat
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
RELATED_MAX = 20
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
    with contextlib.suppress(BaseException):
        card = st.read_latest_card(seat)
        if card:
            item["card_generation"] = int(card.get("generation", 0) or 0)   # the card this batch was queued under
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
    """The assembled input.  ``exchange`` (the worker's folder under the artifact root), ``allowed_ids``
    (what ``related_existing_ids`` may name), ``unread`` (ranges left unread) and ``related`` (the
    checked ids the worker named) are filled in later and default to "none"."""

    def __init__(self, path: Path, digest: str, cursors: list, empty: bool, unread: Optional[list] = None,
                 allowed_ids: Optional[set] = None):
        self.path, self.digest, self.cursors, self.empty = path, digest, cursors, empty
        self.unread = unread or []
        self.allowed_ids = allowed_ids or set()
        self.exchange: Optional[Path] = None
        self.related: list = []


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


MAX_SILENT_HOPS = 16


def _unread_entry(target: dict, chunk) -> dict:
    return {"harness": target["harness"], "sid": target["sid"], "role": target["role"],
            "coverage": tt.describe_coverage(chunk), "blocked": chunk.blocked, "remaining": tt.unread_total(chunk)}


def assemble(item: dict, batch: str, run_dir: Path) -> Bundle:
    seat = st.Seat(**item["seat"])
    project_key = st.project_key_for(item["cwd"])
    sessions, cursors, choices, skipped, unread = [], [], [], [], []
    remaining = TOTAL_CHUNK_BYTES
    for target in _target_sessions(item, seat):
        if remaining <= 0:
            break
        if not target["transcript"]:
            skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": "no-record"})
            continue
        for hop in range(MAX_SILENT_HOPS + 1):
            chunk = tt.read_pending(target["harness"], target["sid"], target["transcript"],
                                    limit_bytes=min(SESSION_CHUNK_BYTES, remaining))
            if chunk.error:
                skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": chunk.error})
                break
            if chunk.skipped_oversize:
                skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": "oversize-row"})
            if chunk.cursor_to == chunk.cursor_from:
                if chunk.pending_after:                 # blocked at an open question: nothing readable yet
                    unread.append(_unread_entry(target, chunk))
                break                                   # (or nothing unread at all)
            if not chunk.text and not chunk.choices:
                # Nothing to tidy in this range (tool output only): it counts as read, then look further back.
                tt.mark_applied(target["harness"], target["sid"], chunk)
                if chunk.pending_after and hop < MAX_SILENT_HOPS:
                    continue
                if chunk.pending_after:
                    unread.append(_unread_entry(target, chunk))
                break
            remaining -= len(chunk.text.encode("utf-8"))
            sessions.append({"harness": target["harness"], "sid": target["sid"], "role": target["role"],
                             "unit": chunk.unit, "cursor_from": chunk.cursor_from, "cursor_to": chunk.cursor_to,
                             "total": chunk.total, "eof": chunk.eof, "pending_after": chunk.pending_after,
                             "rows": chunk.rows, "skipped_oversize": chunk.skipped_oversize,
                             "blocked": chunk.blocked, "text": chunk.text})
            cursors.append({"harness": target["harness"], "sid": target["sid"], "cursor": chunk.cursor_to,
                            "source": target["transcript"], "chunk": chunk})
            for choice in chunk.choices:
                choices.append({**choice, "session": target["sid"]})
            if chunk.pending_after:
                unread.append(_unread_entry(target, chunk))
            break
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
    return Bundle(path, digest, cursors, empty, unread, {r["id"] for r in records})


def coverage_note(bundle: Bundle) -> str:
    """"일부만 읽음(범위)" when part of a record was left unread, else ""; never reads as "nothing to tidy"."""
    if not bundle.unread:
        return ""
    first = bundle.unread[0]
    more = f" 외 기록 {len(bundle.unread) - 1}개도 일부만 읽음" if len(bundle.unread) > 1 else ""
    why = "열린 질문 앞까지, " if first.get("blocked") else ""
    return f"일부만 읽음({why}{first['coverage']}){more} — 앞부분은 다음 정리에서 계속"


# ---------------------------------------------------------------------------
# Registered worker
# ---------------------------------------------------------------------------

def write_prompt(run_dir: Path, batch: str, bundle: Bundle) -> Path:
    where = bundle.exchange or run_dir          # the worker's own folder when there is one
    actions = where / "actions.json"
    text = (
        "Session-tidy memory worker. Follow the unit instructions (ops/session-tidy-memory).\n\n"
        f"batch_id: {batch}\n"
        f"input (read only): {where / 'input_v1.json' if bundle.exchange else bundle.path}\n"
        f"input_digest: {bundle.digest}\n"
        f"output (write exactly this one file): {actions}\n\n"
        "Read the input, write actions.json (JSON only), then stop. Do not call any memory write command.\n\n"
        "Final message: exactly the three handoff lines with `artifact: -`, then `verdict: PASS` (or FAIL) and\n"
        "`blocker: none` (or one line). The output file above is a private handoff to the runner, not a\n"
        "durable artifact, so its path does not go in the artifact field.\n")
    if bundle.exchange:
        path = bundle.exchange / "prompt.md"
        _write_private(path, text.encode("utf-8"))
    else:
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


def launch_worktree(item: dict) -> str:
    """The ``--worktree`` the checked wrapper registers the worker under (it derives its artifact root from it)."""
    return item["cwd"] if injected_executable("HEARTING_TIDY_WORKER_CMD") else _git_worktree(item["cwd"])


def artifact_root_for(worktree: str) -> Path:
    """The artifact root the wrapper launches that worktree's worker with: the same ``artifact-root.sh``
    on the same worktree, in the same cleaned environment.  Nothing else is ever substituted; a root
    that cannot be resolved ends the tidy with its one failure line."""
    try:
        done = subprocess.run([str(ROOT / "utilities" / "artifact-root.sh"), worktree], env=clean_env(),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerFailure(f"no artifact root for the worker's files: {type(exc).__name__}") from exc
    value = done.stdout.strip()
    if done.returncode != 0 or not value or not os.path.isabs(value):
        detail = (done.stderr or done.stdout or "unresolved").strip().splitlines()[-1][:80]
        raise RunnerFailure(f"no artifact root for the worker's files: {detail}")
    return Path(value)


EXCHANGE_PARTS = (".runtime", "session-tidy")


def _write_private(path: Path, data: bytes) -> None:
    """Atomic 0600 write of a file in the worker's folder (outside the state root, so not ``st.atomic_write``)."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prepare_exchange(item: dict, batch: str, bundle: Bundle) -> Path:
    """``<artifact root>/.runtime/session-tidy/<batch>/`` (0700) holding an exact copy of the input.

    The folder is where the worker may read and write; the state folder is not.  A symlink on the way
    is refused.  The root is created when the project has none yet (the wrapper's own launch needs it too).
    """
    root = artifact_root_for(launch_worktree(item))
    try:
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in (*EXCHANGE_PARTS, batch):
            current = current / part
            if current.is_symlink():
                raise RunnerFailure(f"the worker's folder is a symlink: {current}")
            current.mkdir(mode=0o700, exist_ok=True)
            if part != EXCHANGE_PARTS[0]:
                os.chmod(current, 0o700)
        _write_private(current / "input_v1.json", bundle.path.read_bytes())
    except OSError as exc:
        raise RunnerFailure(f"cannot prepare the worker's folder: {type(exc).__name__}") from exc
    bundle.exchange = current
    return current


def remove_exchange(path, only_conversation: bool = False) -> None:
    """Delete a batch's worker folder (or just the conversation copy and the prompt) if it is the real one."""
    if not path:
        return
    folder = Path(path)
    if folder.parent.name != EXCHANGE_PARTS[1] or folder.parent.parent.name != EXCHANGE_PARTS[0] \
            or folder.is_symlink() or not folder.is_dir():
        return
    if only_conversation:
        for name in ("input_v1.json", "prompt.md"):
            with contextlib.suppress(OSError):
                os.unlink(folder / name)
    else:
        shutil.rmtree(folder, ignore_errors=True)


def launch_command(item: dict, prompt: Path, slug: str) -> list:
    injected = injected_executable("HEARTING_TIDY_WORKER_CMD")
    harness = WORKER_HARNESS.get(item["harness"], "claude")
    if injected:
        base = [str(injected)]
    else:
        base = [sys.executable, str(ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")]
    return base + [
        "--start", "--worktree", launch_worktree(item),
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


def wait_worker(receipt: dict, run_dir: Path, actions: Optional[Path] = None) -> None:
    """Wait until the registered worker's process is gone (checked exact-identity state, polled
    with a limit), then require its output file.  The model's stdout is never read.

    A route-free support worker has no terminal commit, so its registry row is not closed by
    the checked join (``settle_finished_attempt`` answers ``not-route-bound``); the process
    state is the end signal and ``actions.json`` (``actions``, else the one in ``run_dir``) is the result.
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
    if not (actions or run_dir / "actions.json").is_file():
        raise RunnerFailure("worker finished without writing actions.json")


# ---------------------------------------------------------------------------
# actions.json check, apply, finish
# ---------------------------------------------------------------------------

def _read_worker_file(path: Path, limit: int) -> bytes:
    """The worker's output, read without following a symlink and only if it is a regular file of sane size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RunnerFailure(f"actions.json cannot be read: {type(exc).__name__}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RunnerFailure("actions.json is not a regular file")
        if info.st_size > limit:
            raise RunnerFailure("actions.json is too large")
        return handle.read(limit + 1)


def validate_actions(run_dir: Path, batch: str, bundle: Bundle) -> None:
    """Check the worker's ``actions.json`` (in its folder when there is one) and leave the checked
    result in ``run_dir`` for ``mem tidy-apply``; the worker's copy is never what gets applied."""
    source = (bundle.exchange / "actions.json") if bundle.exchange else run_dir / "actions.json"
    raw = _read_worker_file(source, ACTIONS_MAX_BYTES)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RunnerFailure(f"actions.json is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("actions"), list):
        raise RunnerFailure("actions.json must be an object with an actions list")
    if any(not isinstance(a, dict) for a in doc["actions"]):
        raise RunnerFailure("every action must be an object")
    # The runner owns the identity of the batch; the worker's copies are not trusted.
    doc["schema_version"], doc["batch_id"], doc["input_digest"] = 1, batch, bundle.digest
    # Optional: which existing records of this project the conversation relates to (ids only).
    named = doc.get("related_existing_ids")
    related: list = []
    for rid in named if isinstance(named, list) else []:
        if isinstance(rid, str) and rid in bundle.allowed_ids and rid not in related:
            related.append(rid)
    doc["related_existing_ids"] = related[:RELATED_MAX]
    bundle.related = doc["related_existing_ids"]
    st.atomic_write_json(run_dir / "actions.json", doc)


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
    """Take the ranges this batch covered off the unread ones (and nothing more)."""
    for entry in bundle.cursors:
        if entry.get("chunk") is not None:
            tt.mark_applied(entry["harness"], entry["sid"], entry["chunk"])
        else:
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


def journal_ids(run_dir: Path) -> list:
    """``[(id, kind)]`` of what this batch's undo journal says really landed, new and changed first."""
    doc = st.read_json(run_dir / "undo.json")
    ops = [op for op in (doc.get("ops") if isinstance(doc, dict) else None) or []
           if isinstance(op, dict) and op.get("state") == "done"]
    out = [(op["id"], "새") for op in ops if op.get("op") == "add" and op.get("id")]
    out += [(op["new_id"], "갱신") for op in ops if op.get("op") == "supersede" and op.get("new_id")]
    out += [(op["id"], "강화") for op in ops if op.get("op") == "reinforce" and op.get("id")]
    return out


def _current_headlines(ids: list) -> dict:
    """``{id: headline}`` of the active records among ``ids`` (read only; best effort, never raises)."""
    out: dict = {}
    if not ids:
        return out
    try:
        mem = td.load_mem()
        if not mem.DB.exists():
            return out
        con = mem.get_con()
        try:
            for rid in ids:
                row = con.execute("SELECT headline, status FROM records WHERE id=?", (rid,)).fetchone()
                if row and row[1] == "active":
                    out[rid] = " ".join(str(row[0] or "").split())[:120]
        finally:
            con.close()
    except BaseException:  # noqa: BLE001 - the list is a nicety, never a reason to fail the tidy
        return out
    return out


def memory_refs(run_dir: Path, bundle: Optional[Bundle]) -> tuple:
    """``(refs, more)``: records this batch touched (from the journal, so only what landed) first,
    then the existing records the worker named as related; at most 8 and 1,200 UTF-8 bytes."""
    entries = list(journal_ids(run_dir))
    seen = {rid for rid, _ in entries}
    entries += [(rid, "관련") for rid in (bundle.related if bundle else []) if rid not in seen]
    heads = _current_headlines([rid for rid, _ in entries])
    refs, used, total = [], 0, 0
    for rid, kind in entries:
        if rid not in heads:
            continue
        total += 1
        line = f"- {rid} [{kind}] {heads[rid]}"
        size = len(line.encode("utf-8")) + 1
        if len(refs) < st.MEMORY_REFS_MAX and used + size <= st.MEMORY_REFS_BYTES:
            refs.append({"id": rid, "kind": kind, "headline": heads[rid]})
            used += size
    return refs, total - len(refs)


def publish_memory(item: dict, batch: str, run_dir: Path, bundle: Optional[Bundle], status: str,
                   detail: str = "") -> None:
    """Refresh the seat card's "참고할 기억" list (never touches the card itself); best effort."""
    with contextlib.suppress(BaseException):
        refs, more = memory_refs(run_dir, bundle) if status != "failed" or journal_ids(run_dir) else ([], 0)
        st.update_memory_refs(st.Seat(**item["seat"]), batch=batch, status=status, refs=refs, more=more,
                              coverage=coverage_note(bundle) if bundle else "", detail=detail,
                              result_path=str(run_dir / "result.json"),
                              source_generation=item.get("card_generation"))


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


def _with_coverage(line: str, note: str) -> str:
    """Put the coverage note before the undo command so a cut never takes the undo away."""
    if not note:
        return line
    head, sep, undo = line.partition(" — 되돌리기")
    return f"{head} · {note}{sep}{undo}"


def process_item(item: dict) -> None:
    attempts = int(item.get("attempts", 0))
    batch = item["id"] if attempts == 0 else f"{item['id']}-r{attempts}"
    item["attempts"] = attempts + 1
    run_dir = st.ensure_dir(_runs_dir() / batch)
    set_status(item, "assembling", batch=batch)
    bundle = None
    finished = False
    try:
        bundle = assemble(item, batch, run_dir)
        if bundle.empty:
            # The notice goes first: a terminal status means "the notice was left".
            note = coverage_note(bundle)
            # Unread ranges left means the read part had nothing to tidy, never "nothing new".
            notify(item, f"[정리] 읽은 범위에는 정리할 대화가 없었습니다 · {note}" if note
                   else "[정리] 새로 정리할 대화가 없습니다.")
            set_status(item, "notified", result="nothing-in-range" if note else "nothing-new")
            finished = True
            return
        set_status(item, "dispatching", exchange=str(prepare_exchange(item, batch, bundle)))
        receipt = dispatch_worker(item, batch, run_dir, bundle)
        set_status(item, "waiting-worker", attempt_id=receipt["attempt_id"])
        wait_worker(receipt, run_dir, bundle.exchange / "actions.json")
        set_status(item, "validating")
        validate_actions(run_dir, batch, bundle)
        set_status(item, "applying")
        line = apply_actions(item, run_dir, bundle)
        result = st.read_json(run_dir / "result.json")
        over = int(result.get("discarded_over_budget") or 0) if isinstance(result, dict) else 0
        if over:
            # Proposals beyond the batch's ten were dropped: that conversation is read again next time.
            line = _with_coverage(line, "상한을 넘은 제안은 다음 정리에서 다시 봅니다")
        else:
            advance_watermarks(bundle)
            line = _with_coverage(line, coverage_note(bundle))
        publish_memory(item, batch, run_dir, bundle, "applied" if not over else "partial",
                       "상한 초과 제안 폐기" if over else "")
        notify(item, line)
        set_status(item, "notified", result="applied")
        finished = True
    except RunnerFailure as exc:
        _log(run_dir, f"failed: {exc}")
        applied = applied_writes(run_dir)
        publish_memory(item, batch, run_dir, bundle, "failed", str(exc)[:80])
        notify(item, failure_line(str(exc), batch, applied))
        set_status(item, "failed", error=str(exc), applied=list(applied))
    except BaseException as exc:  # noqa: BLE001 - nothing may leave an entry half-done
        _log(run_dir, f"failed: internal {type(exc).__name__}: {exc}")
        applied = applied_writes(run_dir)
        publish_memory(item, batch, run_dir, bundle, "failed", f"internal {type(exc).__name__}")
        notify(item, failure_line(f"internal {type(exc).__name__}", batch, applied))
        set_status(item, "failed", error=f"internal {type(exc).__name__}", applied=list(applied))
    finally:
        for name in ("input_v1.json", "prompt.md"):
            with contextlib.suppress(OSError):
                os.unlink(run_dir / name)
        # The conversation copy and the prompt go in every case; the rest of the worker's folder goes
        # with a clean finish (a failed batch keeps its actions.json until the entry is pruned).
        remove_exchange(item.get("exchange"), only_conversation=not finished)
        if finished:
            remove_exchange(item.get("exchange"))


def prune_finished() -> None:
    cutoff = _now() - KEEP_FINISHED_DAYS * 86400
    for item in list_items():
        if item["status"] in UNFINISHED or float(item.get("updated", _now()) or 0) >= cutoff:
            continue
        remove_exchange(item.get("exchange"))
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
