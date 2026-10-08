#!/usr/bin/env python3
"""session-tidy: seat state, session cards, session ledger, result notices, hook text.

One entry point for the parts of ``session-tidy`` that a session touches directly:

    session_tidy.py card [--harness H] [--session-id S] [--cwd D] [--file F | --text T]
        Write the calling session's card (body from stdin, a file or --text).
        Prints one line: ``card=<path> seat=<key>``.  Nothing is required.
    session_tidy.py hook --harness H --event start|prompt|compact --session-id S
                         [--source X] [--transcript P] [--cwd D] [--reread]
        Record the session in the seat ledger and print the injection text for a
        card / result notice this session is due, or nothing at all.  A hook never
        fails: any problem is silent and exits 0.
    session_tidy.py status [--json] [--cwd D]
        Print where the state lives and what is waiting.

    session_tidy.py enqueue [--harness H] [--session-id S] [--cwd D] [--no-clear | --no-continue]
        Queue the memory tidy and return at once (one line); the detached runner
        ``session_tidy_runner.py`` does the work and leaves a result notice.  Inside
        herdr it also books the window's auto-clear and the continue prompt after it
        (``session_tidy_clear.py``); ``--no-clear`` keeps the window and cancels a pending
        booking, ``--no-continue`` clears without typing anything after it.
    session_tidy.py handoff <target> [--harness H] [--session-id S] [--cwd D]
        Deliver this seat's card to a peer session through ``peer-steward.py prompt``
        and report its typed verdict (``prompted=...``) and exit code on one line.  The
        card is marked as handed off: this window is not continued from it.

State lives in ``${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/``
(directories 0700, files 0600, temp file + rename, symlinks refused):

    cards/<seat>.json|.md   latest card (canonical JSON + readable text)
    card-history/<seat>/    bounded older cards
    sessions/<seat>.jsonl   seat ledger: one line per hook/card event
    consumed/<seat>.json    who already received the latest card generation (+ the "참고할 기억" revision)
    reread/<harness>-<sid>  the injection a session just consumed (OpenCode re-emits)
    notices/<seat>.json     result lines waiting for the next start/prompt
    locks/<seat>.lock       flock for the read-modify-write above
    prompt-seq/<seat>.json  count of real prompts submitted at the seat (card/enqueue compare it)
    clear/<seat>.json       the one pending auto-clear reservation (``session_tidy_clear.py``)
    watermarks/  decisions/pending/  runs/  queue/  runner.lock   (other slices)

The seat is the herdr pane when ``HERDR_PANE_ID`` is set, else harness + project.
A Codex whose tools and hooks run in the shared app-server daemon has no pane variable; its
pane is then the one herdr ``agent list`` entry (``agent=codex``) whose session is exactly the
caller's thread id, else the one whose seat ledger already knows the thread (herdr's value can lag
a ``/clear``), else the one whose confirmed auto-clear named exactly this thread as its new session,
else -- for the first hook after an auto-clear -- the one window waiting for its successor.  No
match or several -> the project seat, quietly.
A worker marker is checked before the pane: a worker that inherited its
supervisor's pane id gets no card, ledger line or notice.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Iterator, Optional

SCHEMA = 1
HARNESSES = ("claude", "codex", "opencode")
CARD_BODY_MAX_BYTES = 8 * 1024
INJECTION_MAX_BYTES = 2400
MEMORY_REFS_MAX = 8                 # "참고할 기억" (card layer B): records listed
MEMORY_REFS_BYTES = 1200            # ... and the bytes their lines may take
CARD_MIN_WHEN_BUNDLED = 900         # the card keeps at least this much room when B rides along
NOTICE_MAX_BYTES = 500
CARD_HISTORY_KEEP = 20
LEDGER_FOLD_LINES = 500
LEDGER_KEEP_RAW = 100
HANDOVER_KEEP_ROWS = 40
PROMPT_LEDGER_THROTTLE_SEC = 20
COMPACT_DEDUPE_SEC = 60
AUTHOR_STALE_SEC = 10 * 60


class StateError(Exception):
    """The state folder is unsafe (a symlink) or unusable."""


# ---------------------------------------------------------------------------
# State folder primitives
# ---------------------------------------------------------------------------

def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hearting" / "session-tidy"


def _reject_symlink(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise StateError(f"refusing symlink: {path}")


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (0700) below the state root; refuse symlinks on the way."""
    root = state_root()
    path = Path(path)
    if path != root and root not in path.parents:
        raise StateError(f"outside the state root: {path}")
    chain = [root] + [p for p in reversed(path.parents) if root in p.parents] + ([path] if path != root else [])
    for part in chain:
        _reject_symlink(part)
        if not part.exists():
            part.mkdir(mode=0o700, parents=True, exist_ok=True)
        if part == root or root in part.parents:
            with contextlib.suppress(OSError):
                os.chmod(part, 0o700)
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    _reject_symlink(path)
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


def atomic_write_json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8"))


def read_bytes(path: Path) -> Optional[bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def read_json(path: Path):
    raw = read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


@contextlib.contextmanager
def seat_lock(seat_key: str) -> Iterator[None]:
    """Serialize hook consumption, ledger appends and card writes for one seat."""
    lock_dir = ensure_dir(state_root() / "locks")
    path = lock_dir / f"{seat_key}.lock"
    _reject_symlink(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def now_epoch() -> float:
    return time.time()


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _digest(*parts: str, size: int = 20) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:size]


# ---------------------------------------------------------------------------
# Worker check, harness / session identity, seat
# ---------------------------------------------------------------------------

def is_worker(env=None) -> bool:
    """Same markers as hooks/mem-recall-inject.sh; checked before any pane."""
    env = os.environ if env is None else env
    if env.get("AGENT_SESSION_ROLE") == "worker" or env.get("AGENT_DISPATCH_CHILD") == "1":
        return True
    if env.get("AGENT_DISPATCH_DEPTH"):
        return True
    if env.get("FLEET_TITLE_REFRESH") == "1" or env.get("MEM_DISTILL") == "1":
        return True
    return any(key.startswith("OPENCODE_DISPATCH") and value for key, value in env.items())


def session_from_env(harness: Optional[str] = None, env=None) -> tuple[Optional[str], Optional[str]]:
    """(harness, session id) from the harness-provided variables, or (harness, None).

    Read by `session_identity`; with several harnesses' ids inherited and none
    named, the first in its claude, codex, opencode order answers.
    """
    from session_identity import identity, session_ids
    sessions = session_ids(env)
    if harness:
        session_id = sessions.get(harness, (None, None))[0]
        return harness, session_id.strip() if session_id else None
    found = identity(env)
    if found.known and found.session_id:
        return found.harness, found.session_id.strip()
    for name, (session_id, _variable) in sessions.items():
        return name, session_id.strip()
    return None, None


_PROJECT_KEYS: dict[str, str] = {}
PROJECT_KEY_TTL_SEC = 6 * 3600


def _project_key_cache() -> Path:
    return state_root() / "project-keys.json"


def project_key_for(cwd) -> str:
    """The memory tool's project key (origin, common root, marker), never raising.

    ``mem.project_key`` runs three ``git`` calls (slow on a loaded NFS box), and a
    pane-less prompt hook needs it every turn, so the answer is cached per cwd for
    a few hours next to the rest of the state.
    """
    cwd = str(cwd or os.getcwd())
    if cwd in _PROJECT_KEYS:
        return _PROJECT_KEYS[cwd]
    now = now_epoch()
    cache = read_json(_project_key_cache())
    hit = cache.get(cwd) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and hit.get("key") and now - float(hit.get("at", 0)) < PROJECT_KEY_TTL_SEC:
        _PROJECT_KEYS[cwd] = str(hit["key"])
        return _PROJECT_KEYS[cwd]
    key = ""
    try:
        memory_dir = str(Path(__file__).resolve().parents[1] / "tools" / "memory")
        if memory_dir not in sys.path:
            sys.path.insert(0, memory_dir)
        import mem  # type: ignore

        key = str(mem.project_key(cwd, seed=False) or "")
    except BaseException:  # noqa: BLE001 - a hook never fails because of the key
        key = ""
    if not key:
        with contextlib.suppress(OSError):
            key = "cwd:" + str(Path(cwd).resolve())
        key = key or "cwd:" + cwd
    _PROJECT_KEYS[cwd] = key
    with contextlib.suppress(Exception):
        fresh = {c: v for c, v in (cache.items() if isinstance(cache, dict) else [])
                 if isinstance(v, dict) and now - float(v.get("at", 0)) < PROJECT_KEY_TTL_SEC}
        fresh[cwd] = {"key": key, "at": now}
        atomic_write_json(_project_key_cache(), dict(list(fresh.items())[-200:]))
    return key


@dataclasses.dataclass(frozen=True)
class Seat:
    kind: str            # "pane" | "project"
    key: str             # hash used in file names
    pane: str = ""
    harness: str = ""
    project_key: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "key": self.key, "pane": self.pane,
                "harness": self.harness, "project_key": self.project_key}


def seat_for_project(harness: str, project_key: str) -> Seat:
    return Seat("project", _digest("project", harness, project_key), "", harness, project_key)


HERDR_LOOKUP_TIMEOUT_SEC = 2.0      # a hook has a few seconds in total


def _herdr_executable() -> Optional[str]:
    try:
        import session_tidy_clear
        return session_tidy_clear.herdr_command()
    except BaseException:  # noqa: BLE001 - no herdr, no lookup
        return shutil.which("herdr")


def _herdr_codex_agents() -> Optional[list[dict]]:
    """The Codex entries of ``herdr agent list``; None when herdr cannot be asked (missing, slow,
    unreadable).  Read-only: nothing is typed or changed."""
    exe = _herdr_executable()
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "agent", "list"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=HERDR_LOOKUP_TIMEOUT_SEC)
        agents = (json.loads(done.stdout or "").get("result") or {}).get("agents")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    if not isinstance(agents, list):
        return None
    return [a for a in agents if isinstance(a, dict) and a.get("agent") == "codex" and a.get("pane_id")]


def _agent_session(agent: dict) -> str:
    session = agent.get("agent_session")
    return str(session.get("value") or "") if isinstance(session, dict) else ""


def _real(path) -> str:
    with contextlib.suppress(OSError, TypeError, ValueError):
        return os.path.realpath(str(path))
    return ""


def _pane_seat_of(pane: str) -> Seat:
    return Seat("pane", _digest("pane", pane), pane, "codex", "")


def ledger_precedes(seat: Seat, older: str, newer: str, harness: str = "codex") -> bool:
    """True when this seat's own ledger saw session ``older`` strictly before session ``newer``."""
    try:
        rows = session_summary(seat)
        a, b = rows.get((harness, older)), rows.get((harness, newer))
        return bool(a and b and float(a["first_seen"]) < float(b["first_seen"]))
    except Exception:  # noqa: BLE001
        return False


def _successor_pane(sid: str, agents: list[dict], cwd: str) -> list[str]:
    """The window whose auto-clear is under way, for the first hook of the session it started.

    herdr keeps showing the cleared session, and the shared daemon gives the TUI no rollout to
    read, so the new thread cannot be proven.  It is taken only when everything points at one
    window: exactly one Codex window of herdr works in this directory, its booking is still
    waiting for the successor, and herdr shows the booked session there or an older one of the seat."""
    here = [a for a in agents if _real(a.get("foreground_cwd") or a.get("cwd") or "") == _real(cwd)]
    if len(here) != 1:
        return []
    pane = str(here[0]["pane_id"])
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(_pane_seat_of(pane).key)
    except BaseException:  # noqa: BLE001
        return []
    ok = bool(booking) and booking.get("harness") == "codex" and booking.get("status") in ("reserved", "unverified") \
        and booking.get("sid") not in (sid, "") \
        and (_agent_session(here[0]) == booking.get("sid")
             or ledger_precedes(_pane_seat_of(pane), _agent_session(here[0]), str(booking.get("sid")))) \
        and _real(booking.get("cwd") or "") == _real(cwd) \
        and (booking.get("seat") or {}).get("pane") == pane
    return [pane] if ok else []


def _cleared_into(sid: str) -> list[str]:
    """The windows whose confirmed auto-clear read exactly ``sid`` off their own screen as the
    thread it started (``peer-steward.py clear``).  Read from the bookings alone: a thread id is
    unique, and herdr may be slow or gone just when that thread's first hook runs."""
    try:
        import session_tidy_clear
        keys = [path.stem for path in session_tidy_clear.clear_dir().glob("*.json")]
    except BaseException:  # noqa: BLE001
        return []
    panes = []
    for key in keys:
        try:
            booking = session_tidy_clear.read_reservation(key)
        except BaseException:  # noqa: BLE001
            continue
        pane = str(((booking or {}).get("seat") or {}).get("pane") or "")
        if booking and booking.get("harness") == "codex" and booking.get("status") == "cleared" \
                and booking.get("new_session") == sid and pane and _pane_seat_of(pane).key == key:
            panes.append(pane)
    return panes


def codex_pane_for_session(sid: str, cwd: str = "", source: str = "") -> str:
    """The herdr pane of a Codex session that has no ``HERDR_PANE_ID`` -- exactly one, else "".

    In order: the pane whose herdr session is this thread; the pane whose seat ledger already
    knows this thread (herdr's value can lag a ``/clear`` for good); the pane whose confirmed
    auto-clear started exactly this thread (no herdr needed); for the first hook after a clear
    (``source=clear``) the one window whose auto-clear is waiting for its successor."""
    if not sid or sid == "-":
        return ""
    agents = _herdr_codex_agents()
    panes: list[str] = []
    if agents:
        panes = [str(a["pane_id"]) for a in agents if _agent_session(a) == sid]
        if not panes:
            panes = [str(a["pane_id"]) for a in agents
                     if ("codex", sid) in session_summary(_pane_seat_of(str(a["pane_id"])))]
    if not panes:
        panes = _cleared_into(sid)
    if not panes and agents and source == "clear" and cwd:
        panes = _successor_pane(sid, agents, cwd)
    return panes[0] if len(set(panes)) == 1 else ""


def resolve_seat(harness: Optional[str], cwd=None, env=None, sid: Optional[str] = None,
                 source: str = "") -> Seat:
    env = os.environ if env is None else env
    pane = (env.get("HERDR_PANE_ID") or "").strip()
    if not pane and harness == "codex" and sid:
        pane = codex_pane_for_session(sid, str(cwd or ""), source)
    if pane:
        return Seat("pane", _digest("pane", pane), pane, harness or "", "")
    return seat_for_project(harness or "unknown", project_key_for(cwd))


# ---------------------------------------------------------------------------
# Seat ledger
# ---------------------------------------------------------------------------

def _ledger_path(seat: Seat) -> Path:
    return state_root() / "sessions" / f"{seat.key}.jsonl"


def _read_ledger_lines(seat: Seat) -> list[dict]:
    raw = read_bytes(_ledger_path(seat))
    entries = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("sid"):
            entries.append(item)
    return entries


def session_summary(seat: Seat) -> dict[tuple[str, str], dict]:
    """Fold the ledger: ``(harness, sid) -> first_seen, last_seen, epoch, transcript, cwd, ...``."""
    out: dict[tuple[str, str], dict] = {}
    for item in _read_ledger_lines(seat):
        if item.get("event") == "handover":     # a relation row (dispatch_seat_handover), not a session event
            continue
        key = (str(item.get("harness") or ""), str(item["sid"]))
        row = out.setdefault(key, {"harness": key[0], "sid": key[1], "first_seen": item.get("first_seen", item.get("ts", 0)),
                                   "last_seen": 0, "epoch": 0, "transcript": "", "cwd": "",
                                   "last_event": "", "last_event_at": 0, "last_compact_at": 0})
        ts = float(item.get("ts", 0) or 0)
        row["first_seen"] = min(float(row["first_seen"] or ts), float(item.get("first_seen", ts) or ts))
        if ts >= row["last_seen"]:
            row["last_seen"] = ts
            row["last_event"] = item.get("event", "")
            row["last_event_at"] = ts
        row["epoch"] = max(row["epoch"], int(item.get("epoch", 0) or 0))
        if item.get("transcript"):
            row["transcript"] = item["transcript"]
        if item.get("cwd"):
            row["cwd"] = item["cwd"]
        if item.get("event") == "compact" or item.get("source") == "compact":
            row["last_compact_at"] = max(row["last_compact_at"], ts)
    return out


def latest_session(seat: Seat, harness: Optional[str] = None) -> Optional[dict]:
    rows = [r for r in session_summary(seat).values() if not harness or r["harness"] == harness]
    return max(rows, key=lambda r: r["last_seen"]) if rows else None


def pane_session_aliases(harness: str, sid: str, pane: str, cwd: str) -> list[str]:
    """Display IDs observed at this exact pane, bounded to one harness/repository.

    Reuse the seat history; a same-cwd session elsewhere is not an alias. Readers
    can hold the previous ID while native publication is catching up, so the
    display join is bidirectional. Execution and notification identities stay exact.
    """
    if harness not in HARNESSES or not sid or not pane or not cwd:
        return []
    try:
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.gitinfo import resolve_gitdir
        repository = resolve_gitdir(cwd)[1]
        if not repository:
            return []
        rows = [row for row in session_summary(_pane_seat_of(pane)).values()
                if row["harness"] == harness and row.get("cwd")
                and resolve_gitdir(row["cwd"])[1] == repository]
        if not any(row["sid"] == sid for row in rows):
            return []
        return [row["sid"] for row in sorted(rows, key=lambda r: r["last_seen"], reverse=True)
                if row["sid"] != sid][:8]
    except (OSError, ValueError, TypeError, ImportError):
        return []


def session_start_source(harness: str, sid: str, pane: str) -> Optional[str]:
    """The recorded native start source, not a new or inferred lifecycle event."""
    if harness not in HARNESSES or not sid or not pane:
        return None
    for row in reversed(_read_ledger_lines(_pane_seat_of(pane))):
        if row.get("harness") == harness and row.get("sid") == sid and row.get("event") == "start":
            source = row.get("source")
            return source if source in {"startup", "resume", "clear", "compact", "fork"} else None
    return None


def record_event(seat: Seat, harness: str, sid: str, event: str, *, source: str = "",
                 transcript: str = "", cwd: str = "", now: Optional[float] = None,
                 bump_epoch: bool = False) -> dict:
    """Append one ledger line (caller holds the seat lock). Returns the session row."""
    now = now_epoch() if now is None else now
    summary = session_summary(seat).get((harness, sid))
    epoch = summary["epoch"] if summary else 0
    if event == "prompt" and summary and not transcript \
            and now - summary["last_seen"] < PROMPT_LEDGER_THROTTLE_SEC and not bump_epoch:
        return summary
    if bump_epoch:
        epoch += 1
    entry = {"ts": now, "harness": harness, "sid": sid, "event": event, "epoch": epoch}
    if source:
        entry["source"] = source
    if transcript:
        entry["transcript"] = transcript
    if cwd:
        entry["cwd"] = cwd
    if summary is None:
        entry["first_seen"] = now
    path = _ledger_path(seat)
    ensure_dir(path.parent)
    _reject_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _fold_ledger(seat)
    return session_summary(seat).get((harness, sid), entry)


def _fold_ledger(seat: Seat) -> None:
    """Keep the ledger small: old sessions become one summary line each."""
    lines = _read_ledger_lines(seat)
    if len(lines) <= LEDGER_FOLD_LINES:
        return
    # Handover rows (A -> B, with their bindings) are the seat's authority record: they stay raw.
    relations = [l for l in lines if l.get("event") == "handover"][-HANDOVER_KEEP_ROWS:]
    lines = [l for l in lines if l.get("event") != "handover"]
    recent = lines[-LEDGER_KEEP_RAW:]
    older = lines[:-LEDGER_KEEP_RAW]
    folded: dict[tuple[str, str], dict] = {}
    for item in older:
        key = (str(item.get("harness") or ""), str(item["sid"]))
        row = folded.setdefault(key, {"harness": key[0], "sid": key[1], "event": "summary", "epoch": 0,
                                      "first_seen": item.get("first_seen", item.get("ts", 0)), "ts": 0})
        row["first_seen"] = min(row["first_seen"], item.get("first_seen", item.get("ts", 0)))
        row["ts"] = max(row["ts"], item.get("ts", 0))
        row["epoch"] = max(row["epoch"], int(item.get("epoch", 0) or 0))
        for field in ("transcript", "cwd"):
            if item.get(field):
                row[field] = item[field]
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in list(folded.values()) + relations + recent)
    atomic_write(_ledger_path(seat), body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def card_json_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{seat.key}.json"


def card_text_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{seat.key}.md"


def sanitize_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    return _cut_bytes(text.strip(), CARD_BODY_MAX_BYTES)[0]


def _cut_bytes(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


def read_latest_card(seat: Seat) -> Optional[dict]:
    card = read_json(card_json_path(seat))
    if isinstance(card, dict) and card.get("schema") == SCHEMA and isinstance(card.get("body"), str):
        return card
    return None


def write_card(seat: Seat, harness: str, sid: str, body: str, *, cwd: str = "",
               now: Optional[float] = None, prompt_seq: Optional[int] = None) -> dict:
    """Write the seat's newest card (caller holds the seat lock)."""
    now = now_epoch() if now is None else now
    body = sanitize_body(body)
    summary = session_summary(seat).get((harness, sid))
    previous = read_latest_card(seat)
    generation = int(previous.get("generation", 0)) + 1 if previous else 1
    card = {
        "schema": SCHEMA,
        "seat": seat.as_dict(),
        "author": {"harness": harness, "sid": sid, "epoch": summary["epoch"] if summary else 0},
        "authored_at": iso_utc(now),
        "authored_at_epoch": now,
        "generation": generation,
        "cwd": cwd,
        "body": body,
    }
    if prompt_seq is not None:
        card["prompt_seq"] = int(prompt_seq)    # lets enqueue notice a prompt typed after the card
    if previous and isinstance(previous.get("memory_refs"), dict):
        card["memory_refs"] = previous["memory_refs"]   # layer B outlives the card: only its revision moves it
    if previous:
        history = ensure_dir(state_root() / "card-history" / seat.key)
        atomic_write_json(history / f"{int(previous.get('generation', 0)):08d}.json", previous)
        for old in sorted(history.glob("*.json"))[:-CARD_HISTORY_KEEP]:
            with contextlib.suppress(OSError):
                old.unlink()
    atomic_write_json(card_json_path(seat), card)
    atomic_write(card_text_path(seat), render_card_text(card).encode("utf-8"))
    return card


def mark_card_handed_off(seat: Seat) -> None:
    """The latest card went to a peer session: this seat's window is not continued from it
    (takes the seat lock; a newer card starts unmarked)."""
    with seat_lock(seat.key):
        card = read_latest_card(seat)
        if card and not card.get("handed_off"):
            card["handed_off"] = True
            atomic_write_json(card_json_path(seat), card)


def render_card_text(card: dict) -> str:
    """The readable card file: header, the card body (layer A), and the "참고할 기억" section (layer B)."""
    author = card.get("author") or {}
    header = (f"# session card {card.get('generation')} — {card.get('authored_at')} "
              f"({author.get('harness', '')} {str(author.get('sid', ''))[:8]})\n\n")
    text = header + str(card.get("body", "")) + "\n"
    refs = card.get("memory_refs")
    if isinstance(refs, dict) and refs.get("revision"):
        text += "\n" + build_memory_injection(refs, MEMORY_REFS_BYTES + 600) + "\n"
    return text


STATUS_LABEL = {"applied": "정돈 결과", "partial": "일부만 반영", "failed": "정돈을 끝내지 못함"}


def build_memory_injection(refs: dict, room: int) -> str:
    """The "참고할 기억" section within ``room`` UTF-8 bytes: ids and headlines only, never a record body.

    The header, the coverage/reason lines and the result path always survive; list lines are dropped
    from the end first and counted as "외 N건".
    """
    head = f"[참고할 기억] 묶음 {refs.get('batch', '')} — {STATUS_LABEL.get(refs.get('status'), '')}".rstrip(" —")
    tail = [str(refs[k]) for k in ("coverage",) if refs.get(k)]
    if refs.get("detail"):
        tail.append(f"사유: {refs['detail']}")
    items = [f"- {r.get('id', '')} [{r.get('kind', '')}] {r.get('headline', '')}" for r in refs.get("refs") or []
             if isinstance(r, dict)]
    hidden = int(refs.get("more") or 0)
    path = f"전체 결과: {refs['result_path']}" if refs.get("result_path") else ""
    fixed = [head] + tail + ([path] if path else [])
    room = max(0, room)
    used = sum(len(x.encode("utf-8")) + 1 for x in fixed)
    shown: list = []
    for line in items:
        size = len(line.encode("utf-8")) + 1
        if used + size + (24 if len(shown) + 1 < len(items) or hidden else 0) > room:
            break
        shown.append(line)
        used += size
    omitted = len(items) - len(shown) + hidden
    lines = [head] + shown + ([f"외 {omitted}건"] if omitted else []) + tail + ([path] if path else [])
    return _cut_bytes("\n".join(lines), room)[0] if room else ""


def update_memory_refs(seat: Seat, *, batch: str, status: str, refs: list, more: int = 0, coverage: str = "",
                       detail: str = "", result_path: str = "", source_generation: Optional[int] = None,
                       now: Optional[float] = None) -> Optional[dict]:
    """Set the latest card's layer B ("참고할 기억") for a finished tidy batch; takes the seat lock.

    Only ``memory_refs`` changes: the card's body, author, time and generation stay, so layer A is
    never handed out again because of this.  ``revision`` rises by one per batch result for the seat
    and is what the hooks hand out once.  The same batch with the same result changes nothing, and a
    batch that finishes after a newer card was written still lands here (marked with the generation
    it was queued under).  No card, no layer: ``None``.
    """
    now = now_epoch() if now is None else now
    with seat_lock(seat.key):
        card = read_latest_card(seat)
        if not card:
            return None
        old = card.get("memory_refs") if isinstance(card.get("memory_refs"), dict) else {}
        new = {"batch": batch, "status": status, "refs": refs, "more": int(more), "coverage": coverage,
               "detail": detail, "result_path": result_path}
        if old and all(old.get(k) == v for k, v in new.items()):
            return old
        new["revision"] = int(old.get("revision", 0) or 0) + 1
        new["source_generation"] = int(card.get("generation", 0) or 0) if source_generation is None \
            else int(source_generation)
        new["updated"] = iso_utc(now)
        card["memory_refs"] = new
        atomic_write_json(card_json_path(seat), card)
        atomic_write(card_text_path(seat), render_card_text(card).encode("utf-8"))
        return new


# ---------------------------------------------------------------------------
# Consumption bookkeeping and notices
# ---------------------------------------------------------------------------

def _consumed_path(seat: Seat) -> Path:
    return state_root() / "consumed" / f"{seat.key}.json"


def _write_consumed(seat: Seat, **fields) -> None:
    """Merge ``fields`` into the seat's consumption file (layer A and layer B receipts are independent)."""
    consumed = read_json(_consumed_path(seat))
    data = dict(consumed) if isinstance(consumed, dict) else {}
    data.update(fields)
    data["schema"] = SCHEMA
    atomic_write_json(_consumed_path(seat), data)


def _receipt(harness: str, sid: str, epoch: int) -> str:
    return f"{harness}:{sid}:{epoch}"


def _reread_path(harness: str, sid: str) -> Path:
    return state_root() / "reread" / f"{harness}-{_digest(sid, size=16)}.json"


def _notices_path(seat: Seat) -> Path:
    return state_root() / "notices" / f"{seat.key}.json"


def _prompt_seq_path(seat: Seat) -> Path:
    return state_root() / "prompt-seq" / f"{seat.key}.json"


def read_prompt_seq(seat: Seat) -> int:
    """How many real prompts were submitted at this seat (0 before the first one)."""
    data = read_json(_prompt_seq_path(seat))
    try:
        return max(0, int(data.get("seq", 0))) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return 0


def bump_prompt_seq(seat: Seat, harness: str, sid: str, now: float) -> int:
    """Count one real prompt (caller holds the seat lock).

    A file of its own, written for every prompt: the ledger throttles prompt lines and
    folds old ones, and neither may lose a request that decides whether the window is
    still safe to clear.
    """
    seq = read_prompt_seq(seat) + 1
    atomic_write_json(_prompt_seq_path(seat), {"schema": SCHEMA, "seq": seq, "at": now,
                                              "harness": harness, "sid": sid})
    return seq


def write_notice(seat: Seat, text: str, *, author_harness: str = "", author_sid: str = "",
                 now: Optional[float] = None) -> dict:
    """Leave one result line for the seat's next start/prompt (takes the seat lock)."""
    now = now_epoch() if now is None else now
    line = " ".join((text or "").split())
    line = _cut_bytes("".join(ch for ch in line if ch.isprintable()), NOTICE_MAX_BYTES)[0]
    item = {"id": secrets.token_hex(6), "text": line, "created": now,
            "author": {"harness": author_harness, "sid": author_sid}}
    with seat_lock(seat.key):
        data = read_json(_notices_path(seat))
        items = data.get("items", []) if isinstance(data, dict) else []
        items.append(item)
        atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": items[-20:]})
    return item


def pending_notices(seat: Seat, harness: str, sid: str, *, now: Optional[float] = None,
                    summary: Optional[dict] = None) -> list[dict]:
    """Notices this session should show: its own, or a departed author's."""
    now = now_epoch() if now is None else now
    data = read_json(_notices_path(seat))
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return []
    sessions = summary if summary is not None else session_summary(seat)
    me = sessions.get((harness, sid))
    out = []
    for item in items:
        author = item.get("author") or {}
        a_key = (author.get("harness") or "", author.get("sid") or "")
        if not a_key[1] or a_key == (harness, sid):
            out.append(item)
            continue
        theirs = sessions.get(a_key)
        if theirs is None:
            out.append(item)          # author unknown to the ledger: nobody else will show it
            continue
        replaced = bool(me) and me["first_seen"] > theirs["last_seen"]
        stale = now - theirs["last_seen"] > AUTHOR_STALE_SEC
        if replaced and (seat.kind == "pane" or stale):
            out.append(item)
    return out


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "방금"
    if seconds < 3600:
        return f"{seconds // 60}분 전"
    if seconds < 172800:
        return f"{seconds // 3600}시간 전"
    return f"{seconds // 86400}일 전"


def build_card_injection(card: dict, card_path: Path, now: float, cap: int) -> str:
    """Card text within ``cap`` UTF-8 bytes; header and the file path always survive."""
    author = card.get("author") or {}
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(card.get("authored_at_epoch") or now)))
    header = (f"[세션 카드] 같은 자리에서 이어받는 작업 카드 — 작성 {stamp} "
              f"({_ago(now - float(card.get('authored_at_epoch') or now))}, {author.get('harness', '')} "
              f"{str(author.get('sid', ''))[:8]})")
    footer = f"카드 전문: {card_path}"
    cut_note = "…(이하 생략)"
    body = card.get("body", "")
    fixed = len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
    room = cap - fixed
    if room <= 0:
        return _cut_bytes(f"{header}\n{footer}", cap)[0]
    if len(body.encode("utf-8")) <= room:
        return f"{header}\n{body}\n{footer}"
    room -= len(cut_note.encode("utf-8")) + 1
    trimmed = _cut_bytes(body, max(0, room))[0].rstrip()
    return f"{header}\n{trimmed}\n{cut_note}\n{footer}"


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def run_hook(harness: str, event: str, sid: str, *, source: str = "", transcript: str = "",
             cwd: str = "", reread: bool = False, env=None, now: Optional[float] = None,
             emit: Optional[Callable[[str], None]] = None) -> str:
    """Record the session, and print the card / notices it is due exactly once.

    Returns the text that was emitted ("" for none). The receipt is written only
    after ``emit`` returned, so a hook whose output never went out keeps the card
    for the next start or prompt.
    """
    env = os.environ if env is None else env
    if is_worker(env) or harness not in HARNESSES or not sid or event not in ("start", "prompt", "compact"):
        return ""
    now = now_epoch() if now is None else now
    seat = resolve_seat(harness, cwd or None, env, sid, source)
    with seat_lock(seat.key):
        if reread:
            saved = read_json(_reread_path(harness, sid))
            summary = session_summary(seat).get((harness, sid))
            text = str(saved.get("text") or "") if isinstance(saved, dict) and summary \
                and saved.get("epoch") == summary["epoch"] else ""
            if text and emit:
                emit(text)
            return text
        if event == "prompt":
            bump_prompt_seq(seat, harness, sid, now)
        prior = session_summary(seat).get((harness, sid))
        # SessionStart(source=compact) and a separate compact event describe the same
        # compaction; count it once.
        compacting = (event == "compact" or source == "compact") and not (
            prior and now - prior["last_compact_at"] < COMPACT_DEDUPE_SEC)
        row = record_event(seat, harness, sid, event, source=source, transcript=transcript,
                           cwd=cwd, now=now, bump_epoch=compacting)
        if event == "compact":
            return ""
        if event == "start" and seat.kind == "pane":
            with contextlib.suppress(BaseException):    # a hook never fails for the clear note
                import session_tidy_clear
                session_tidy_clear.note_start_locked(seat, harness, sid, now)
        # The one place a same-seat handover (A -> B) is recorded: B's confirmed start/first prompt.
        handed = None
        handover = None
        if seat.kind == "pane" and event in ("start", "prompt"):
            with contextlib.suppress(BaseException):    # a hook never fails for the handover
                import dispatch_seat_handover as handover
                handed = handover.record_locked(seat, harness, sid, event, source, now)
        epoch = int(row.get("epoch", 0) or 0)
        parts: list[str] = []
        notices = pending_notices(seat, harness, sid, now=now)
        notice_block = ""
        if notices:
            notice_block = "\n".join(f"[정리 결과] {n['text']}" for n in notices)
            notice_block = _cut_bytes(notice_block, NOTICE_MAX_BYTES)[0]
            parts.append(notice_block)
        card = read_latest_card(seat)
        card_due = False
        memory_due = False
        memory = None
        resume_text = ""
        if card:
            author = card.get("author") or {}
            same_session = (author.get("harness"), author.get("sid")) == (harness, sid)
            eligible = (not same_session) or epoch > int(author.get("epoch", 0) or 0)
            consumed = read_json(_consumed_path(seat))
            # One card generation is injected once for the whole seat: any receipt for it,
            # from any session, means the card has already been handed over.
            taken = isinstance(consumed, dict) and consumed.get("generation") == card.get("generation") \
                and bool(consumed.get("receipts"))
            card_due = eligible and not taken
            # Layer B ("참고할 기억") has its own revision and its own receipt: a session that already
            # took card generation N still gets a newer B once, and the writer of the card (who
            # could be cleared next) does not use it up before the new session can see it.
            memory = card.get("memory_refs") if isinstance(card.get("memory_refs"), dict) else None
            taken_rev = int(consumed.get("memory_revision", 0) or 0) if isinstance(consumed, dict) else 0
            memory_due = bool(memory) and eligible and int(memory.get("revision", 0) or 0) > taken_rev
            used = len(notice_block.encode("utf-8")) + (1 if notice_block else 0)
            # The verified route and the existing resume command, one line, with the card B receives.
            if handover is not None and (card_due or handed):
                resume_text = handover.resume_line(seat, harness, sid)
                used += len(resume_text.encode("utf-8")) + (1 if resume_text else 0)
            room = INJECTION_MAX_BYTES - used
            memory_text = ""
            if memory_due:
                wanted = build_memory_injection(memory, MEMORY_REFS_BYTES + 600)
                memory_text = build_memory_injection(
                    memory, min(len(wanted.encode("utf-8")), room - CARD_MIN_WHEN_BUNDLED - 1) if card_due else room)
                room -= len(memory_text.encode("utf-8")) + 1
            if resume_text:
                parts.append(resume_text)
            if card_due:
                parts.append(build_card_injection(card, card_text_path(seat), now, room))
            if memory_text:
                parts.append(memory_text)
        if not card and handed and handover is not None:
            resume_text = handover.resume_line(seat, harness, sid)
            if resume_text:
                parts.append(resume_text)
        text = "\n".join(parts)
        if not text:
            return ""
        (emit or (lambda t: None))(text)
        if card_due:
            consumed = read_json(_consumed_path(seat))
            receipts = list(consumed.get("receipts", [])) if isinstance(consumed, dict) \
                and consumed.get("generation") == card.get("generation") else []
            receipts.append(_receipt(harness, sid, epoch))
            _write_consumed(seat, generation=card["generation"], receipts=receipts[-200:])
        if memory_due:
            consumed = read_json(_consumed_path(seat))
            same = isinstance(consumed, dict) and consumed.get("memory_revision") == memory["revision"]
            receipts = list(consumed.get("memory_receipts", [])) if same else []
            receipts.append(_receipt(harness, sid, epoch))
            _write_consumed(seat, memory_revision=memory["revision"], memory_receipts=receipts[-200:])
        if notices:
            shown = {n["id"] for n in notices}
            data = read_json(_notices_path(seat))
            rest = [i for i in (data.get("items", []) if isinstance(data, dict) else []) if i.get("id") not in shown]
            atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": rest})
        # OpenCode re-emits this cache every turn: a later "참고할 기억" is added to what the session
        # already received instead of replacing it.
        earlier = read_json(_reread_path(harness, sid))
        kept = str(earlier.get("text") or "") if isinstance(earlier, dict) and earlier.get("epoch") == epoch else ""
        cached = _cut_bytes(kept + "\n" + text, 2 * INJECTION_MAX_BYTES)[0] if kept else text
        atomic_write_json(_reread_path(harness, sid), {"schema": SCHEMA, "epoch": epoch, "text": cached, "at": now})
        return text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # a hook must never exit non-zero because of its arguments
        raise ValueError(message)


def _emit_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _guess_seat_from_ledgers(cwd: str) -> tuple[Optional[Seat], Optional[dict]]:
    best: tuple[Optional[Seat], Optional[dict]] = (None, None)
    for name in HARNESSES:
        seat = seat_for_project(name, project_key_for(cwd))
        row = latest_session(seat, name)
        if row and (best[1] is None or row["last_seen"] > best[1]["last_seen"]):
            best = (seat, row)
    return best


def resolve_caller(harness, sid, cwd: str) -> Optional[tuple[Seat, str, str]]:
    """(seat, harness, session id) of the calling session; ids come from the arguments,
    the harness variables, then the seat ledger, and as a last resort a fresh ``unknown-`` id."""
    if not sid:
        harness, sid = session_from_env(harness)
    detected = harness or session_from_env(None)[0]
    seat = resolve_seat(detected, cwd, sid=sid)
    if not sid or not harness:
        ledger_row = latest_session(seat, harness)
        if ledger_row is None and seat.kind == "project" and not detected:
            seat, ledger_row = _guess_seat_from_ledgers(cwd)
        if ledger_row:
            harness = harness or ledger_row["harness"]
            sid = sid or ledger_row["sid"]
    if seat is None:
        return None
    return seat, harness or detected or "unknown", sid or f"unknown-{secrets.token_hex(4)}"


def cmd_card(args) -> int:
    if is_worker():
        print("card=none reason=worker")
        return 0
    cwd = args.cwd or os.getcwd()
    if args.text is not None:
        body = args.text
    elif args.file:
        body = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        body = sys.stdin.read()
    if not sanitize_body(body):
        print("card=none reason=empty-body")
        return 0
    caller = resolve_caller(args.harness, args.session_id, cwd)
    if caller is None:
        print("card=none reason=no-seat")
        return 0
    seat, harness, sid = caller
    with seat_lock(seat.key):
        record_event(seat, harness, sid, "card", cwd=cwd)
        write_card(seat, harness, sid, body, cwd=cwd, prompt_seq=read_prompt_seq(seat))
    print(f"card={card_text_path(seat)} seat={seat.key}")
    return 0


def cmd_enqueue(args) -> int:
    if is_worker():
        print("enqueue=none reason=worker")
        return 0
    cwd = args.cwd or os.getcwd()
    caller = resolve_caller(args.harness, args.session_id, cwd)
    if caller is None:
        print("enqueue=none reason=no-seat")
        return 0
    seat, harness, sid = caller
    import session_tidy_runner as runner
    transcript = (session_summary(seat).get((harness, sid)) or {}).get("transcript", "")
    import session_tidy_clear as clear
    with contextlib.suppress(BaseException):    # the tidy goes ahead even if the snapshot cannot be taken
        import dispatch_seat_handover as handover
        with seat_lock(seat.key):
            handover.write_snapshot_locked(seat, harness, sid)
    item = runner.enqueue_item(seat, harness, sid, cwd, transcript)
    try:
        booked = clear.schedule_for_enqueue(seat, harness, sid, cwd, opt_out=bool(args.no_clear),
                                            no_continue=bool(args.no_continue))
    except BaseException as exc:  # noqa: BLE001 - the tidy is queued; the window is left alone
        booked = f"clear=manual reason=internal-{type(exc).__name__} hint={clear.CLEAR_COMMAND.get(harness, '/clear')}"
    print(f"enqueue={item['id']} seat={seat.key} status=queued {booked}")
    return 0


def cmd_handoff(args) -> int:
    if is_worker():
        print("prompted=false reason=worker")
        return 1
    caller = resolve_caller(args.harness, args.session_id, args.cwd or os.getcwd())
    if caller is None:
        print("prompted=false reason=no-seat")
        return 1
    import session_tidy_runner as runner
    line, code = runner.handoff(caller[0], args.target)
    print(line)
    return code


def cmd_hook(args) -> int:
    try:
        sid = args.session_id or session_from_env(args.harness)[1] or ""
        run_hook(args.harness, args.event, sid, source=args.source or "", transcript=args.transcript or "",
                 cwd=args.cwd or os.getcwd(), reread=args.reread, emit=_emit_stdout)
    except BaseException:  # noqa: BLE001 - a hook is silent on every failure
        pass
    return 0


def _queue_counts(seat: Seat) -> dict:
    counts: dict = {}
    folder = state_root() / "queue"
    try:
        names = list(folder.iterdir())
    except OSError:
        return counts
    for path in names:
        item = read_json(path) if path.suffix == ".json" else None
        if isinstance(item, dict) and (item.get("seat") or {}).get("key") == seat.key:
            counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
    return counts


def cmd_status(args) -> int:
    harness, sid = session_from_env(None)
    seat = resolve_seat(harness, args.cwd or os.getcwd(), sid=sid)
    card = read_latest_card(seat)
    notices = read_json(_notices_path(seat))
    info = {
        "state_root": str(state_root()),
        "seat": seat.as_dict(),
        "worker": is_worker(),
        "card": {"generation": card["generation"], "authored_at": card["authored_at"],
                 "path": str(card_text_path(seat))} if card else None,
        "sessions": len(session_summary(seat)),
        "notices": len(notices.get("items", [])) if isinstance(notices, dict) else 0,
        "queue": _queue_counts(seat),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, sort_keys=True))
    else:
        print(f"state_root={info['state_root']}")
        print(f"seat={seat.key} kind={seat.kind}")
        print("card=" + (f"gen{card['generation']} {card['authored_at']}" if card else "none"))
        print(f"sessions={info['sessions']} notices={info['notices']} worker={int(info['worker'])}")
        print("queue=" + (" ".join(f"{k}={v}" for k, v in sorted(info["queue"].items())) or "empty"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="session_tidy.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    card = sub.add_parser("card", help="write the calling session's card")
    card.add_argument("--harness", choices=HARNESSES)
    card.add_argument("--session-id")
    card.add_argument("--cwd")
    card.add_argument("--file")
    card.add_argument("--text")
    card.set_defaults(func=cmd_card)
    hook = sub.add_parser("hook", help="record the session; print the card/notice it is due")
    hook.add_argument("--harness", required=True)
    hook.add_argument("--event", required=True)
    hook.add_argument("--session-id")
    hook.add_argument("--source")
    hook.add_argument("--transcript")
    hook.add_argument("--cwd")
    hook.add_argument("--reread", action="store_true")
    hook.set_defaults(func=cmd_hook)
    enqueue = sub.add_parser("enqueue", help="queue the memory tidy and return at once")
    enqueue.add_argument("--harness", choices=HARNESSES)
    enqueue.add_argument("--session-id")
    enqueue.add_argument("--cwd")
    enqueue.add_argument("--no-clear", action="store_true",
                         help="keep this window: no auto-clear (cancels a pending one)")
    enqueue.add_argument("--no-continue", action="store_true",
                         help="clear the window but type nothing after it (the user asked to stop after tidying)")
    enqueue.set_defaults(func=cmd_enqueue)
    handoff = sub.add_parser("handoff", help="deliver this seat's card to a peer session")
    handoff.add_argument("target")
    handoff.add_argument("--harness", choices=HARNESSES)
    handoff.add_argument("--session-id")
    handoff.add_argument("--cwd")
    handoff.set_defaults(func=cmd_handoff)
    status = sub.add_parser("status", help="show state location and what is waiting")
    status.add_argument("--json", action="store_true")
    status.add_argument("--cwd")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args, _unknown = parser.parse_known_args(argv)
    except ValueError:
        if argv and argv[0] == "hook":
            return 0
        parser.print_usage(sys.stderr)
        return 2
    except SystemExit as exc:
        return 0 if (argv and argv[0] == "hook") else int(exc.code or 0)
    try:
        return int(args.func(args) or 0)
    except StateError as exc:
        if args.command == "hook":
            return 0
        print(f"session-tidy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
